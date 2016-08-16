import json
import logging
import os
import re
import socket
import string
import subprocess
import sys
import time

import consulate

import docker
import docker.errors

import requests
import requests.exceptions

LOG = logging.getLogger('aaSemble-host-agent')

RUN_INTERVAL = 10
JSON_SYNC_LOCK_PATH = 'lock/json-sync'


def setup_logging():  # pragma: nocover
    LOG.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(fmt)

    LOG.addHandler(sh)


class UnsetValue(object):
    pass


class Task(object):
    def __init__(self, agent, command=None, container=None, set_after=None, unless_set=None, only_if_set=None, lock=None):
        if (not command) and (not container):
            raise Exception('Must set either command or container')

        if command and container:
            raise Exception('Must set only one of command or container')

        self.agent = agent
        self.command = command
        self.container = container
        self.set_after = set_after or {}
        self.unless_set = unless_set
        self.only_if_set = only_if_set
        self.lock = lock

    def execute(self):
        if self.command:
            proc = subprocess.Popen(self.command, shell=True, stdout=subprocess.PIPE)
            stdout, _stderr = proc.communicate()
            rv = proc.returncode == 0
        else:
            self.container.create(self.agent.docker)
            self.container.start(self.agent.docker)
            rv = self.container.wait(self.agent.docker) == 0
            stdout = self.agent.docker.logs(container=self.container.container_id, stdout=True)

        if rv:
            for k, v in self.set_after.items():
                self.agent.consul.kv.set(string.Template(k).substitute(hostname=socket.gethostname()),
                                         string.Template(v).substitute(stdout=stdout))
        return rv

    def handle(self):
        if self.unless_set:
            if self.agent.consul.kv.get(self.unless_set, UnsetValue) is not UnsetValue:
                return

        if self.only_if_set:
            if self.agent.consul.kv.get(self.only_if_set, UnsetValue) is UnsetValue:
                return

        if self.lock:
            self.agent.attempt_to_acquire_lock(self.lock)
            if not self.agent.lock_held(self.lock):
                return
            self.execute()
            self.agent.release_lock(self.lock)
        else:
            self.execute()


class Container(object):
    restart_policy = {"MaximumRetryCount": 0, "Name": "always"}

    def __init__(self, name, image, docker_access=False, host_network=False, command=None, environment=None, binds=None, privileged=False):
        self.name = name
        self.image = image
        self.docker_access = docker_access
        self.host_network = host_network
        self.command = command
        self.environment = environment or {}
        self.binds = binds or {}
        self.privileged = privileged

    @classmethod
    def get_name_from_dict(cls, info_dict):
        return info_dict['name']

    @classmethod
    def from_dict(cls, info_dict):
        return cls(name=cls.get_name_from_dict(info_dict),
                   image=info_dict['image'],
                   docker_access=info_dict.get('docker_access', False),
                   host_network=info_dict.get('host_network', False),
                   command=info_dict.get('command', None),
                   environment=info_dict.get('environment', None),
                   privileged=info_dict.get('privileged', False),
                   binds=info_dict.get('binds', None))

    def create(self, dc, old_container=None):
        host_config_kwargs = {'restart_policy': self.restart_policy, 'publish_all_ports': True,
                              'privileged': self.privileged}
        kwargs = {'image': self.image, 'name': self.name, 'environment': self.environment}

        if self.docker_access:
            kwargs['volumes'] = ['/var/run/docker.sock']
            host_config_kwargs['binds'] = {'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}}

        if self.host_network:
            host_config_kwargs['network_mode'] = 'host'

        if old_container:
            host_config_kwargs['volumes_from'] = old_container

        if self.binds:
            if 'binds' not in host_config_kwargs:
                host_config_kwargs['binds'] = {}

            if 'volumes' not in kwargs:
                kwargs['volumes'] = []

            for src, mntpnt in self.binds.items():
                kwargs['volumes'] += [mntpnt]
                host_config_kwargs['binds'][src] = {'bind': mntpnt}

        local_ip = get_local_ip()

        if isinstance(self.command, (list, tuple)):
            kwargs['command'] = [s.replace('%LOCAL_IP%', local_ip) for s in self.command]
        elif self.command:
            kwargs['command'] = self.command.replace('%LOCAL_IP%', local_ip)

        kwargs['host_config'] = dc.create_host_config(**host_config_kwargs)

        LOG.info('Creating %s', self)
        LOG.info('kwargs: %r' % (kwargs,))
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
            LOG.info(e, exc_info=True)
            raise

    def wait(self, dc):
        return dc.wait(container=self.container_id)

    def __repr__(self):
        return '<Container name="%s" image="%s" command=%r docker_access=%r host_network=%r>' % (self.name, self.image, self.command, self.docker_access, self.host_network)

    def pull(self, dc):
        if ':' in self.image:
            repository, tag = self.image.split(':')
        elif '@' in self.image:
            repository, tag = self.image.split('@')
        else:
            repository, tag = self.image, None

        LOG.info('Pulling repository=%r tag=%r', repository, tag)
        dc.pull(repository=repository, tag=tag)


class TaskContainer(Container):
    restart_policy = {}

    @classmethod
    def get_name_from_dict(cls, info_dict):
        return info_dict.get('name', None)


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
        self._session = None
        self._consul = None
        self._docker = None
        self._session_refreshed = 0

    def lock_held(self, lock):
        try:
            LOG.info(self.consul.kv.get_record(lock))
            LOG.info(self.session)
            rv = self.consul.kv.get_record(lock)['Session'] == self.session
        except:
            LOG.info('oh, false', exc_info=True)
            rv = False

        LOG.info('Holding lock "%s": %r', lock, rv)
        return rv

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
            self._session = self.consul.session.create(ttl='%ds' % (2 * RUN_INTERVAL,))
        else:
            if self._session_refreshed + RUN_INTERVAL < time.time():
                LOG.info('Renewing session %s', self._session)
                try:
                    self.consul.session.renew(self._session)
                except:
                    LOG.info('Session renewal failed. Creating a new one')
                    self._session = self.consul.session.create(ttl='%ds' % (2 * RUN_INTERVAL,))
        return self._session

    def attempt_to_acquire_lock(self, lock):
        try:
            print(self.consul.kv.acquire_lock(lock, self.session))
        except Exception:
            LOG.info('Failure during attempt to acquire lock "%s".', lock, exc_info=True)

    def release_lock(self, lock):
        self.consul.kv.release_lock(lock, self.session)

    def update_json_from_web_service(self):
        LOG.info('Updating JSON from web service')

        try:
            url = os.environ['CLUSTER']
            LOG.debug('URL: %s', url)

            data = requests.get(url).json()['json']
            self.consul.kv['aaSemble/json'] = data
        except:
            LOG.info('Failed to update JSON from web service. No big deal, carrying on.', exc_info=True)

    def is_cluster_joined(self):
        rv = False
        try:
            LOG.debug('Checking if we have joined a cluster')
            rv = bool(self.consul.catalog.nodes())
            LOG.info('Cluster joined: %r', rv)
        except:
            LOG.debug('Failure fetching node list.', exc_info=True)
        return rv

    def join_known_nodes_until_joined(self):
        for node in requests.get(os.environ['CLUSTER'] + 'nodes/').json()['results']:
            self.consul.agent.join(node['internal_ip'])
            if self.is_cluster_joined():
                break

    def consul_agent_cmd(self):
        cmd = ['agent']
        if os.environ.get('CONSUL_SERVERS', False):
            cmd += ['-bootstrap-expect=' + os.environ['CONSUL_SERVERS']]
        if os.environ.get('CONSUL_SERVER', '0') == '1':
            cmd += ['-server']
        return cmd

    def get_required_containers(self):
        containers = {'consulagent': Container(image='consul:latest', name='consulagent', host_network=True, docker_access=True,
                                               environment={'CONSUL_BIND_INTERFACE': os.environ.get('CONSUL_BIND_INTERFACE', 'eth0')}, command=self.consul_agent_cmd(),
                                               binds={'/etc/consul.d': '/consul/config'}),
                      'aasemble-host-agent': Container(image=os.environ.get('AASEMBLE_HOST_AGENT', '') or 'aasemble/hostagent', name='aasemble-host-agent', host_network=True, docker_access=True,
                                                       environment={'CONSUL_BIND_INTERFACE': os.environ.get('CONSUL_BIND_INTERFACE', '') or 'eth0'}),
                      'registrator': Container(image='gliderlabs/registrator', name='registrator',
                                               host_network=True, docker_access=True,
                                               environment={'DOCKER_HOST': 'unix:///var/run/docker.sock'},
                                               command=["-deregister=on-success", "-internal", "consul://localhost:8500"])}

        try:
            js = self.consul.kv.get('aaSemble/json')
        except requests.exceptions.ConnectionError:
            return containers

        if not js:
            return containers

        data = json.loads(js)

        if 'containers' in data:
            for c in data['containers']:
                if not c['nodes']:
                    continue

                if re.match(c['nodes'], socket.gethostname()):
                    containers[c['name']] = Container.from_dict(c)
        return containers

    def register_with_web_service(self):
        cluster = os.environ.get('CLUSTER')
        LOG.info('Registering with web service as part of cluster: %r', cluster)
        requests.post('%snodes/' % (cluster,), data={'cluster': cluster,
                                                     'internal_ip': get_local_ip()})

    def main(self, argv=sys.argv[1:]):
        try:
            if not self.lock_held(JSON_SYNC_LOCK_PATH):
                self.attempt_to_acquire_lock(JSON_SYNC_LOCK_PATH)
            else:
                LOG.info('We hold the lock.')

            if self.lock_held(JSON_SYNC_LOCK_PATH):
                self.update_json_from_web_service()

            if not self.is_cluster_joined():
                self.register_with_web_service()
                self.join_known_nodes_until_joined()

        except:
            LOG.info('Unexpected failure.', exc_info=True)

        self.apply_container_config()
        self.run_tasks()

    def run_tasks(self):
        try:
            js = self.consul.kv.get('aaSemble/json')
        except requests.exceptions.ConnectionError:
            return

        if not js:
            return

        data = json.loads(js)
        for task in data.get('tasks', []):
            if re.match(task['nodes'], socket.gethostname()):
                self.handle_task(task)

    def handle_task(self, task_dict):
        if 'container' in task_dict:
            container = TaskContainer.from_dict(task_dict['container'])
        else:
            container = None

        task = Task(agent=self, command=task_dict.get('command', None),
                    container=container,
                    set_after=task_dict.get('set_after', None),
                    only_if_set=task_dict.get('only_if_set', None),
                    unless_set=task_dict.get('unless_set', None),
                    lock=task_dict.get('lock', None))
        task.handle()

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


def run():  # pragma: nocover
    agent = AasembleHostAgent()
    setup_logging()
    while True:
        try:
            agent.main()
        except:
            LOG.info('main() failed somehow.', exc_info=True)
        time.sleep(RUN_INTERVAL)

if __name__ == '__main__':  # pragma: nocover
    run()
