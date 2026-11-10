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

    def test_native_messages_during_running_jobs_are_only_hidden_when_covered(self):
        _, device = self.register()
        for kind in ['codex', 'claude']:
            agent = self.create_agent(name=kind + '-live', kind=kind)
            job = self.prompt(agent, 'relay question')
            device.request('POST', '/api/device/poll', {'capacity': 1})
            self.server.database.execute('UPDATE jobs SET started_at=1000 WHERE id=?', (job['id'],))
            session = {'kind': kind, 'session_id': kind + '-live-session', 'name': kind,
                       'cwd': str(self.root), 'busy': True, 'updated_at': 1005,
                       'messages': [
                           {'id': kind + '-echo-user', 'role': 'user', 'content': 'relay question', 'created_at': 1001},
                           {'id': kind + '-echo-answer', 'role': 'assistant', 'content': 'first streamed update', 'created_at': 1002},
                           {'id': kind + '-new-answer', 'role': 'assistant', 'content': 'new native reply', 'created_at': 1003},
                           {'id': kind + '-local-user', 'role': 'user', 'content': 'local followup', 'created_at': 1004},
                           {'id': kind + '-local-answer', 'role': 'assistant', 'content': 'streamed', 'created_at': 1005}]}
            device.request('POST', '/api/device/poll', {
                'active': [{'id': job['id'], 'session_id': session['session_id'], 'output': 'first streamed update\n'}],
                'sessions': [session]})
            path = '/api/agents/' + agent['id'] + '/messages'
            reply = self.client.request('GET', path)
            contents = [m['content'] for m in reply['messages']]
            self.assertIn('new native reply', contents)
            self.assertIn('local followup', contents)
            self.assertIn('streamed', contents)  # A substring of another message is not a copy.
            self.assertEqual(contents.count('relay question'), 1)
            self.assertNotIn('first streamed update', contents)
            native = next(m for m in reply['messages'] if m['content'] == 'new native reply')
            self.assertEqual(reply['active_jobs'], [{'id': job['id'], 'status': 'running'}])
            queued = self.prompt(agent, 'queued next question')
            device.request('POST', '/api/device/poll', {'active': [{
                'id': job['id'], 'output': 'first streamed update\nnew native reply\n'}]})
            caught_up = self.client.request('GET', path)
            self.assertIn(native['id'], caught_up['suppressed_message_ids'])
            self.assertNotIn(native['id'], [m['id'] for m in caught_up['messages']])
            self.assertIn('queued next question', [m['content'] for m in caught_up['messages']])
            device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {
                'status': 'succeeded', 'output': 'first streamed update\n'})
            completed = self.client.request('GET', path)
            self.assertIn('new native reply', [m['content'] for m in completed['messages']])
            self.client.request('POST', '/api/jobs/' + queued['id'] + '/cancel', {})
