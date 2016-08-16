import unittest

import mock

import aasemble_host_agent


class ContainerTests(unittest.TestCase):
    def test_from_dict_constructor(self):
        d = {'name': 'thename',
             'image': 'aasemble/hostagent:sometag',
             'command': ['foo', 'bar', 'baz'],
             'host_network': True,
             'docker_access': True,
             'privileged': False,
             'binds': {'/home/soren/theapp': '/srv'}}
        container = aasemble_host_agent.Container.from_dict(d)
        self.assertEqual(container.name, 'thename')
        self.assertEqual(container.image, 'aasemble/hostagent:sometag')
        self.assertEqual(container.command, ['foo', 'bar', 'baz'])
        self.assertEqual(container.docker_access, True)
        self.assertEqual(container.host_network, True)
        self.assertEqual(container.privileged, False)
        self.assertEqual(container.binds, {'/home/soren/theapp': '/srv'})

    def test_create_and_start(self):
        container = aasemble_host_agent.Container(name='thename', image='aasemble/hostagent:sometag')

        class FakeDockerClient(object):
            def create_host_config(selff, **kwargs):
                return kwargs

            def create_container(selff, **kwargs):
                self.assertEqual(kwargs, {'environment': {},
                                          'image': 'aasemble/hostagent:sometag',
                                          'host_config': {'publish_all_ports': True,
                                                          'restart_policy': {'MaximumRetryCount': 0,
                                                                             'Name': 'always'},
                                                          'privileged': False},
                                          'name': 'thename'})
                return {'Id': 'containerId1234'}

            def start(selff, container):
                self.assertEqual(container, 'containerId1234')

        dc = FakeDockerClient()
        container.create(dc)
        self.assertEqual(container.container_id, 'containerId1234')

        container.start(dc)

    def test_create_simple(self):
        container = aasemble_host_agent.Container(name='thename', image='aasemble/hostagent:sometag')
        self._test_create(container, {'environment': {},
                                      'image': 'aasemble/hostagent:sometag',
                                      'host_config': {'publish_all_ports': True,
                                                      'restart_policy': {'MaximumRetryCount': 0,
                                                                         'Name': 'always'},
                                                      'privileged': False},
                                      'name': 'thename'})

    def test_create_with_docker_access(self):
        container = aasemble_host_agent.Container(name='thename', image='aasemble/hostagent:sometag',
                                                  docker_access=True)
        self._test_create(container, {'environment': {},
                                      'image': 'aasemble/hostagent:sometag',
                                      'host_config': {'publish_all_ports': True,
                                                      'restart_policy': {'MaximumRetryCount': 0,
                                                                         'Name': 'always'},
                                                      'privileged': False,
                                                      'binds': {'/var/run/docker.sock': {'bind': '/var/run/docker.sock',
                                                                                         'mode': 'rw'}}},
                                      'name': 'thename',
                                      'volumes': ['/var/run/docker.sock']})

    def test_create_with_docker_access_and_host_network(self):
        container = aasemble_host_agent.Container(name='thename', image='aasemble/hostagent:sometag',
                                                  docker_access=True, host_network=True)
        self._test_create(container, {'environment': {},
                                      'image': 'aasemble/hostagent:sometag',
                                      'host_config': {'publish_all_ports': True,
                                                      'restart_policy': {'MaximumRetryCount': 0,
                                                                         'Name': 'always'},
                                                      'privileged': False,
                                                      'network_mode': 'host',
                                                      'binds': {'/var/run/docker.sock': {'bind': '/var/run/docker.sock',
                                                                                         'mode': 'rw'}}},
                                      'name': 'thename',
                                      'volumes': ['/var/run/docker.sock']})

    def test_create_with_binds(self):
        container = aasemble_host_agent.Container(name='thename', image='aasemble/hostagent:sometag',
                                                  docker_access=True, host_network=True,
                                                  binds={'/home/soren/src/svc': '/srv'})
        self._test_create(container, {'environment': {},
                                      'image': 'aasemble/hostagent:sometag',
                                      'host_config': {'publish_all_ports': True,
                                                      'restart_policy': {'MaximumRetryCount': 0,
                                                                         'Name': 'always'},
                                                      'privileged': False,
                                                      'network_mode': 'host',
                                                      'binds': {'/var/run/docker.sock': {'bind': '/var/run/docker.sock',
                                                                                         'mode': 'rw'},
                                                                '/home/soren/src/svc': {'bind': '/srv'}}},
                                      'name': 'thename',
                                      'volumes': ['/var/run/docker.sock', '/srv']})

    def test_create_with_old_container(self):
        container = aasemble_host_agent.Container(name='thename', image='aasemble/hostagent:sometag')
        self._test_create(container, {'environment': {},
                                      'image': 'aasemble/hostagent:sometag',
                                      'host_config': {'publish_all_ports': True,
                                                      'privileged': False,
                                                      'restart_policy': {'MaximumRetryCount': 0,
                                                                         'Name': 'always'},
                                                      'volumes_from': 'oldie'},
                                      'name': 'thename'}, old_container='oldie')

    def _test_create(self, container, expected_create_container_kwargs, old_container=None):
        class FakeDockerClient(object):
            def create_host_config(selff, **kwargs):
                return kwargs

            def create_container(selff, **kwargs):
                self.assertEqual(kwargs, expected_create_container_kwargs)
                return {'Id': 'containerId1234'}

            def start(selff, container):
                self.assertEqual(container, 'containerId1234')

        dc = FakeDockerClient()
        container.create(dc, old_container=old_container)
        self.assertEqual(container.container_id, 'containerId1234')

        container.start(dc)


class FakeConsul(object):
    def __init__(self, data):
        self.data = data or {}
        self.kv = self

    def get(self, key, default=None):
        return self.data.get(key, default)


class FakeAgent(object):
    def __init__(self, consul_data=None, allowed_locks=None):
        self.consul = FakeConsul(data=consul_data)
        self.allowed_locks = allowed_locks or set()
        self.held_locks = set()

    def attempt_to_acquire_lock(self, lockpath):
        if lockpath in self.allowed_locks:
            self.held_locks.add(lockpath)

    def lock_held(self, lockpath):
        return lockpath in self.held_locks

    def release_lock(self, lockpath):
        if lockpath in self.held_locks:
            self.held_locks.remove(lockpath)
        else:
            raise Exception('We do not hold that lock. What should we do?')


class TaskTests(unittest.TestCase):
    def test_init_neither_command_nor_container(self):
        self.assertRaises(Exception, aasemble_host_agent.Task, FakeAgent())

    def test_init_both_command_and_container(self):
        self.assertRaises(Exception, aasemble_host_agent.Task, FakeAgent(),
                          command='foobar', container={'fakecontainer'})

    def test_init(self):
        aasemble_host_agent.Task(FakeAgent(), command='foobar')

    @mock.patch('aasemble_host_agent.Task.execute')
    def test_handle_unless_set_not_set(self, execute):
        aasemble_host_agent.Task(FakeAgent(), command='foobar', unless_set='someflag').handle()
        execute.assert_called_with()

    @mock.patch('aasemble_host_agent.Task.execute')
    def test_handle_unless_set_is_set(self, execute):
        aasemble_host_agent.Task(FakeAgent(consul_data={'someflag': 'somevalue'}), command='foobar', unless_set='someflag').handle()
        execute.assert_not_called()

    @mock.patch('aasemble_host_agent.Task.execute')
    def test_handle_only_if_set_not_set(self, execute):
        aasemble_host_agent.Task(FakeAgent(), command='foobar', only_if_set='someflag').handle()
        execute.assert_not_called()

    @mock.patch('aasemble_host_agent.Task.execute')
    def test_handle_only_if_set_is_set(self, execute):
        aasemble_host_agent.Task(FakeAgent(consul_data={'someflag': 'somevalue'}), command='foobar', only_if_set='someflag').handle()
        execute.assert_called_with()

    @mock.patch('aasemble_host_agent.Task.execute')
    def test_lock_not_available(self, execute):
        aasemble_host_agent.Task(FakeAgent(), command='foobar', lock='somelockpath').handle()
        execute.assert_not_called()

    @mock.patch('aasemble_host_agent.Task.execute')
    def test_lock_available(self, execute):
        agent = FakeAgent(allowed_locks=['somelockpath'])
        aasemble_host_agent.Task(agent, command='foobar', lock='somelockpath').handle()
        execute.assert_called_with()
        self.assertFalse(agent.held_locks, 'Locks still held on exit')
