import atexit
import json
import logging
import os
import re
import subprocess
import sys
import time

import consulate

import docker
import docker.errors

import requests
import requests.exceptions

LOG = logging.getLogger('aaSemble-host-agent')


def setup_logging():
    LOG.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(fmt)

    LOG.addHandler(sh)


class Container(object):
    def __init__(self, name, image, docker_access=False, host_network=False, cmd=None, environment=None):
        self.name = name
        self.image = image
        self.docker_access = docker_access
        self.host_network = host_network
        self.cmd = cmd
        self.environment = environment or {}

    def create(self, dc, old_container=None):
        host_config_kwargs = {'restart_policy': {"MaximumRetryCount": 0, "Name": "always"}, 'publish_all_ports': True}
        kwargs = {'image': self.image, 'name': self.name, 'environment': self.environment}

        if self.docker_access:
            kwargs['volumes'] = ['/var/run/docker.sock']
            host_config_kwargs['binds'] = {'/var/run/docker.sock': { 'bind': '/var/run/docker.sock', 'mode': 'rw'}}

        if self.host_network:
            host_config_kwargs['network_mode'] = 'host'

        if old_container:
            host_config_kwargs['volumes_from'] = old_container

        local_ip = get_local_ip()

        if isinstance(self.cmd, (list, tuple)):
            kwargs['command'] = [s.replace('%LOCAL_IP%', local_ip) for s in self.cmd]
        elif self.cmd:
            kwargs['command'] = self.cmd.replace('%LOCAL_IP%', local_ip)

        kwargs['host_config'] = dc.create_host_config(**host_config_kwargs)

        LOG.info('Creating %s', self)
        try:
            container = dc.create_container(**kwargs)
        except docker.errors.NotFound:
            LOG.info('Image not found.')
            self.pull(dc)
            LOG.info('Creating %s', self)
            container = dc.create_container(**kwargs)
        self.container_id = container['Id']

    def start(self, dc):
        try:
            LOG.info('Starting %s', self)
            LOG.info(dc.start(container=self.container_id))
        except Exception as e:
            LOG.info(type(e))
            LOG.info(e)

    def __repr__(self):
        return '<Container name="%s" image="%s" cmd=%r docker_access=%r host_network=%r>' % (self.name, self.image, self.cmd, self.docker_access, self.host_network)

    def pull(self, dc):
        if ':' in self.image:
            repository, tag = self.image.split(':')
        elif '@' in self.image:
            repository, tag = self.image.split('@')
        else:
            repository, tag = self.image, None

        LOG.info('Pulling repository=%r tag=%r', repository, tag)
        dc.pull(repository=repository, tag=tag)

def get_local_ip():
    proc = subprocess.Popen('ip -o route get 8.8.8.8', shell=True, stdout=subprocess.PIPE)
    stdout, _stderr = proc.communicate()
    return re.match('.* src ([^ ]*) .*', stdout).groups()[0]


def stop_container(dc, cid, timeout=5):
    dc.stop(container=cid)

def delete_container(dc, cid):
    dc.remove_container(container=cid, force=True)


def is_running(info):
    if 'State' not in info:
        return False
    if info['State'] == 'running':
        return True
    if 'Running' not in info['State']:
        return False
    if info['State']['Running'] == 'True':
        return True
    return False


class AasembleHostAgent(object):
    def __init__(self):
        self.lock_held = False
        self._session = None
        self._consul = None
        self._docker = None

    @property
    def consul(self):
        if not self._consul:
            self._consul = consulate.Consul()
        return self._consul

    @property
    def docker(self):
        if not self._docker:
            self._docker = docker.Client()
        return self._docker

    @property
    def session(self):
        if not self._session:
            self._session = self.consul.session.create()
        return self._session


    def attempt_to_acquire_lock(self):
        try:
            if self.consul.kv.acquire_lock('json-sync', self.session):
                self.lock_held = True
        except Exception as e:
            print(e)

    def update_json_from_web_service(self):
        LOG.info('Updating JSON from web service')
        data = requests.get(os.environ['CLUSTER']).json()['json']
        self.consul.kv['aaSemble/json'] = data

    def is_cluster_joined(self):
        rv = False
        try:
            LOG.debug('Checking if we have joined a cluster')
            rv = bool(self.consul.catalog.nodes())
            LOG.info('Cluster joined: %r', rv)
        except Exception as e:
            print(e)
        return rv

    def join_known_nodes_until_joined(self):
        for node in requests.get(os.environ['CLUSTER'] + 'nodes/').json()['results']:
            self.consul.agent.join(node['internal_ip'])
            if self.is_cluster_joined():
                break

    def consul_agent_cmd(self):
        cmd = ['agent']
        if 'CONSUL_SERVERS' in os.environ:
            cmd += ['-bootstrap-expect=' + os.environ['CONSUL_SERVERS']]
        if os.environ.get('CONSUL_SERVER', '0') == '1':
            cmd += ['-server']
        return cmd

    def get_required_containers(self):
        containers = {'consulagent': Container(image='consul:latest', name='consulagent', host_network=True, docker_access=True,
                                               environment={'CONSUL_BIND_INTERFACE': os.environ.get('CONSUL_BIND_INTERFACE', 'eth0')}, cmd=self.consul_agent_cmd()),
                      'aasemble-host-agent': Container(image=os.environ.get('AASEMBLE_HOST_AGENT', 'aasemble/hostagent:latest'), name='aasemble-host-agent', host_network=True, docker_access=True,
                                               environment={'CONSUL_BIND_INTERFACE': os.environ.get('CONSUL_BIND_INTERFACE', 'eth0')}),
                      'registrator': Container(image='gliderlabs/registrator', name='registrator',
                                               host_network=True, docker_access=True,
                                               environment={'DOCKER_HOST': 'unix:///var/run/docker.sock'},
                                               cmd=["-deregister=on-success", "-internal", "consul://localhost:8500"])}

        try:
            js = self.consul.kv.get('aaSemble/json')
        except requests.exceptions.ConnectionError as e:
            return containers

        if not js:
            return containers

        data = json.loads(js)

        if 'containers' in data:
            for c in data['containers']:
                containers[c['name']] = Container(name=c['name'],
                                                  image=c['image'],
                                                  docker_access=c.get('docker_access', False),
                                                  host_network=c.get('host_network', False))

        return containers


    def register_with_web_service(self):
        cluster = os.environ.get('CLUSTER')
        LOG.info('Registering with web service as part of cluster: %r', cluster)
        requests.post('%snodes/' % (cluster,), data={'cluster': cluster,
                                                     'internal_ip': get_local_ip()})

    def main(self, argv=sys.argv[1:]):
        if not self.lock_held:
            LOG.info('Lock not held. Will attempt to acquire it.')
            self.attempt_to_acquire_lock()

        LOG.info('Lock held: %r.', self.lock_held)

        if self.lock_held:
            self.update_json_from_web_service()

        try:
            if not self.is_cluster_joined():
                self.register_with_web_service()
                self.join_known_nodes_until_joined()
        except Exception as e:
            LOG.info(e)

        self.apply_container_config()

    def apply_container_config(self):
        required_containers = self.get_required_containers()

        for c in self.docker.containers(all=True):
            name = c['Names'][0].lstrip('/')
            if name in required_containers:
                cid = c['Id']

                if c['Image'] == required_containers[name].image:
                    if is_running(c):
                        LOG.info('%s found and is running. Not doing anything.', name)
                        del required_containers[name]
                        continue
                    else:
                        LOG.info('%s found, but is not running. Starting it.', name)
                        self.docker.start(container=cid)
                        del required_containers[name]
                        continue

                LOG.info('%s found, but not using the right image. Pulling new image.', name)

                # First pull the new image, so we're ready to go
                required_containers[name].pull(self.docker)

                LOG.info('Renaming %s to %s.old', name, name)
                self.docker.rename(name, '%s.old' % (name,))

                LOG.info('Creating new %s.', name)
                required_containers[name].create(self.docker, old_container='%s.old' % (name,))

                LOG.info('Killing old %s.', name)
                stop_container(self.docker, cid)

                LOG.info('Launching new %s.', name)
                required_containers[name].start(self.docker)

                delete_container(self.docker, cid)

                del required_containers[name]

        for rc in required_containers:
            LOG.info('Launching %s as it was not found at all.', rc)
            required_containers[rc].create(self.docker)
            required_containers[rc].start(self.docker)

    def clean(self):
        if self._session:
            LOG.info("Destroying session")
            self.consul.session.destroy(self.session)

if __name__ == '__main__':
    agent = AasembleHostAgent()
    atexit.register(agent.clean)
    setup_logging()
    while True:
        try:
            agent.main()
        except Exception as e:
            LOG.info(e)
        time.sleep(10)
