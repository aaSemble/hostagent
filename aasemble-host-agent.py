import json
import logging
import re
import subprocess
import sys
import time

import consulate

import docker

import requests
import requests.exceptions


def setup_logging():
    LOG.setLevel(logging.INFO)

    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    LOG.addHandler(sh)


LOG = logging.getLogger('aaSemble-host-agent')
setup_logging()


class Container(object):
    def __init__(self, name, image, docker_access=False, host_network=False, cmd=None, environment=None):
        self.name = name
        self.image = image
        self.docker_access = docker_access
        self.host_network = host_network
        self.cmd = cmd
        self.environment = environment or {}

    def create(self, dc, old_container=None):
        host_config_kwargs = {'restart_policy': {"MaximumRetryCount": 0,
                                                 "Name": "always"},
                              'publish_all_ports': True}
        kwargs = {'image': self.image,
                  'name': self.name,
                  'environment': self.environment}

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
        self.container_id = dc.create_container(**kwargs)['Id']

    def start(self, dc):
        LOG.info('Starting %s', self)
        dc.start(container=self.container_id)

    def __repr__(self):
        return '<Container name="%s" image="%s" cmd=%r docker_access=%r host_network=%r>' % (self.name, self.image, self.cmd, self.docker_access, self.host_network)

    def pull(self, dc):
        if ':' in self.image:
            repository, tag = self.image.split(':')
        elif '@' in self.image:
            repository, tag = self.image.split('@')
        else:
            repository, tag = self.image, None

        dc.pull(repository=repository, tag=tag)

def get_local_ip():
    proc = subprocess.Popen('ip -o route get 8.8.8.8', shell=True, stdout=subprocess.PIPE)
    stdout, _stderr = proc.communicate()
    return re.match('.* src ([^ ]*) .*', stdout).groups()[0]


def get_required_containers(cc):
#    containers = {'consulagent': Container(image='consul:latest', name='consulagent', host_network=True, docker_access=True, cmd=["agent", "-advertise=%LOCAL_IP%"]),
    containers = {'consulagent': Container(image='consul:latest', name='consulagent', host_network=True, docker_access=True, cmd=["agent", "-dev", "-advertise=%LOCAL_IP%"]),
                  'registrator': Container(image='gliderlabs/registrator', name='registrator',
                                           host_network=True, docker_access=True,
                                           environment={'DOCKER_HOST': 'unix:///var/run/docker.sock'},
                                           cmd=["consul://localhost:8500"])}

    try:
        js = cc.kv.get('aaSemble/json')
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


def kill_container(dc, cid, timeout=5):
    timeout = time.time() + timeout
    dc.kill(container=cid, signal=2)

    while time.time() > timeout:
        if dc.inspect_container(container=cid).get('State', {}).get('Running', 'False') == 'False':
            break
        time.sleep(0.2)

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


def main(argv=sys.argv[1:]):
    dc = docker.Client()
    cc = consulate.Consul()

    required_containers = get_required_containers(cc)
    for c in dc.containers(all=True):
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
                    dc.start(container=cid)
                    del required_containers[name]
                    continue

            LOG.info('%s found, but not using the right image. Pulling new image.', name)

            # First pull the new image, so we're ready to go
            required_containers[name].pull(dc)

            LOG.info('Renaming %s to %s.old', name, name)
            dc.rename(name, '%s.old' % (name,))

            LOG.info('Creating new %s.', name)
            required_containers[name].create(dc, old_container='%s.old' % (name,))

            LOG.info('Killing old %s.', name)
            kill_container(dc, cid)

            LOG.info('Launching new %s.', name)
            required_containers[name].start(dc)
            del required_containers[name]

    for rc in required_containers:
        LOG.info('Launching %s as it was not found at all.', rc)
        required_containers[rc].create(dc)
        required_containers[rc].start(dc)

if __name__ == '__main__':
    while True:
        try:
            main()
        except:
            pass
        time.sleep(10)
