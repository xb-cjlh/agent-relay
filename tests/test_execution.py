import unittest

from scripts.devtools import APIError
import test_relay as fixtures


class ExecutionCase(unittest.TestCase):
    setUp = fixtures.ServerCase.setUp
    tearDown = fixtures.ServerCase.tearDown
    register = fixtures.ServerCase.register
    create_agent = fixtures.ServerCase.create_agent
    prompt = fixtures.ServerCase.prompt

    def test_default_has_no_execution_deadline_and_explicit_limits_are_retained(self):
        self.register()
        agent = self.create_agent()
        self.assertEqual(0, self.prompt(agent)['payload']['timeout'])
        body = {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture'}
        for timeout in (0, 1, 1800):
            result = self.client.request('POST', '/api/jobs', {**body, 'timeout': timeout})
            self.assertEqual(timeout, result['payload']['timeout'])
        for invalid in (-1, 1801, True, None):
            with self.assertRaises(APIError) as error:
                self.client.request('POST', '/api/jobs', {**body, 'timeout': invalid})
            self.assertEqual(400, error.exception.status)

    def test_old_daemons_cannot_silently_apply_their_five_minute_default(self):
        _, device = self.register()
        agent = self.create_agent()
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True}})
        with self.assertRaises(APIError) as error:
            self.prompt(agent)
        self.assertEqual(409, error.exception.status)
        self.assertIn('0.6.1', str(error.exception))
        # Explicit positive timeouts remain usable on older daemons.
        positive = self.client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture', 'timeout': 600})
        self.assertEqual(600, positive['payload']['timeout'])
        self.client.request('POST', '/api/jobs/' + positive['id'] + '/cancel', {})
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True, 'unlimited_tasks': True}})
        unlimited = self.prompt(agent)
        # A daemon downgrade after queuing cannot execute with a different limit.
        claimed = device.request('POST', '/api/device/poll', {'capacity': 1, 'capabilities': {'codex': True}})
        self.assertEqual([], claimed['jobs'])
        result = self.client.request('GET', '/api/jobs/' + unlimited['id'])
        self.assertEqual('failed', result['status'])
        self.assertIn('本次指令未执行', result['error'])

    def test_diagnostics_are_separate_during_streaming_and_after_completion(self):
        _, device = self.register()
        agent = self.create_agent()
        job = self.prompt(agent)
        device.request('POST', '/api/device/poll', {'capacity': 1})
        diagnostic = 'OutputTextDelta without active item <fixture>'
        progress = {'id': job['id'], 'output': 'Assistant answer', 'diagnostics': diagnostic}
        device.request('POST', '/api/device/poll', {'active': [progress]})
        path = '/api/agents/' + agent['id'] + '/messages'
        reply = self.client.request('GET', path)['messages'][-1]
        self.assertEqual('Assistant answer', reply['content'])
        self.assertEqual(diagnostic, reply['diagnostics'])
        device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {**progress, 'status': 'succeeded'})
        reply = self.client.request('GET', path)['messages'][-1]
        self.assertEqual('succeeded', reply['status'])
        self.assertEqual('Assistant answer', reply['content'])
        self.assertEqual(diagnostic, reply['diagnostics'])
