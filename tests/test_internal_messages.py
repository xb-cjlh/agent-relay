import hashlib
import unittest

from scripts.devtools import APIError
import test_relay as fixtures


class InternalMessagesCase(unittest.TestCase):
    setUp = fixtures.ServerCase.setUp
    tearDown = fixtures.ServerCase.tearDown
    register = fixtures.ServerCase.register

    def test_legacy_codex_runtime_context_is_hidden_without_hiding_real_messages(self):
        identity, device = self.register()
        agents = '# AGENTS.md instructions for /Users/fixture\n\n<INSTRUCTIONS>\nProject rules\n</INSTRUCTIONS>'
        environment = '<environment_context>\n<cwd>/Users/fixture</cwd>\n</environment_context>'
        context = agents + '\n' + environment
        session = {'kind': 'codex', 'session_id': 'legacy-context', 'name': 'legacy-context',
                   'cwd': str(self.root), 'updated_at': 1010, 'busy': False,
                   'messages': [
                       {'id': 'normal', 'role': 'user', 'content': 'upgrade relay', 'created_at': 1000},
                       {'id': 'quoted', 'role': 'user', 'content': 'Explain this:\n' + context, 'created_at': 1001},
                       {'id': 'explicit-user', 'role': 'user', 'content': context, 'internal': False, 'created_at': 1002},
                       {'id': 'assistant-example', 'role': 'assistant', 'content': context, 'created_at': 1003},
                       {'id': 'mixed-user', 'role': 'user', 'content': context + '\nPlease review it', 'created_at': 1004},
                       {'id': 'incomplete', 'role': 'user', 'content': '# AGENTS.md instructions for /tmp\n<INSTRUCTIONS>text', 'created_at': 1005},
                       {'id': 'runtime', 'role': 'user', 'content': context, 'created_at': 1006},
                       {'id': 'environment', 'role': 'user', 'content': environment, 'created_at': 1007}]}
        device.request('POST', '/api/device/poll', {'sessions': [session]})
        state = self.client.request('GET', '/api/state?view=web')
        agent = state['agents'][0]
        self.assertEqual(agent['preview'], session['messages'][5]['content'][:140])
        path = '/api/agents/' + agent['id'] + '/messages'
        reply = self.client.request('GET', path)
        self.assertEqual(len(reply['messages']), 6)
        suppressed = {hashlib.sha256((identity['id'] + ':' + source).encode()).hexdigest() for source in ['runtime', 'environment']}
        self.assertEqual(set(reply['suppressed_message_ids']), suppressed)
        self.assertTrue(any(m['role'] == 'assistant' and m['content'] == context for m in reply['messages']))
        self.assertTrue(any(m['role'] == 'user' and m['content'] == context for m in reply['messages']))
        # The same structure in Claude is not automatically a Codex context item.
        session.update(kind='claude', session_id='claude-context', name='claude-example', messages=[{**session['messages'][6], 'id': 'claude-runtime'}])
        device.request('POST', '/api/device/poll', {'sessions': [session]})
        agent = next(a for a in self.client.request('GET', '/api/state')['agents'] if a['kind'] == 'claude')
        self.assertEqual(self.client.request('GET', '/api/agents/' + agent['id'] + '/messages')['messages'][0]['content'], context)

    def test_internal_classification_retracts_history_cache_and_preview(self):
        identity, device = self.register()
        for kind in ['codex', 'claude']:
            text = '## Task and authorization\n\nSame heading can be public.'
            session = {'kind': kind, 'session_id': kind + '-summary', 'name': kind,
                       'cwd': str(self.root), 'updated_at': 1002, 'busy': False,
                       'messages': [
                           {'id': kind + '-public', 'role': 'assistant', 'content': 'normal final answer', 'created_at': 1000},
                           {'id': kind + '-internal', 'role': 'assistant', 'content': text, 'created_at': 1001}]}
            device.request('POST', '/api/device/poll', {'sessions': [session]})
            agent = next(a for a in self.client.request('GET', '/api/state')['agents'] if a['session_id'] == session['session_id'])
            path = '/api/agents/' + agent['id'] + '/messages'
            internal_id = hashlib.sha256((identity['id'] + ':' + kind + '-internal').encode()).hexdigest()
            self.assertIn(internal_id, [m['id'] for m in self.client.request('GET', path)['messages']])
            legacy = session['messages'][1]
            session['messages'] = [{**legacy, 'internal': True, 'content': ''}]
            device.request('POST', '/api/device/poll', {'sessions': [session]})
            reply = self.client.request('GET', path)
            self.assertEqual([m['content'] for m in reply['messages']], ['normal final answer'])
            self.assertIn(internal_id, reply['suppressed_message_ids'])
            older = self.client.request('GET', path + '?before=1005')
            self.assertIn(internal_id, older['suppressed_message_ids'])
            state = self.client.request('GET', '/api/state?view=web')
            self.assertEqual(next(a for a in state['agents'] if a['id'] == agent['id'])['preview'], 'normal final answer')
            self.assertEqual(self.server.database.execute('SELECT CHAR_LENGTH(content),internal FROM messages WHERE id=?', (internal_id,)).splitlines()[-1], '0\t1')
            # Replays from a legacy daemon cannot restore an already classified item.
            session['messages'] = [legacy]
            device.request('POST', '/api/device/poll', {'sessions': [session]})
            self.assertNotIn(internal_id, [m['id'] for m in self.client.request('GET', path)['messages']])
            # Filter by metadata, never by English headings or the message body.
            session['messages'] = [{'id': kind + '-public-heading', 'role': 'assistant', 'content': text, 'created_at': 1003}]
            device.request('POST', '/api/device/poll', {'sessions': [session]})
            self.assertIn(text, [m['content'] for m in self.client.request('GET', path)['messages']])
            session['messages'] = [{'id': 'invalid', 'role': 'assistant', 'content': 'text', 'internal': 'true'}]
            with self.assertRaises(APIError) as error:
                device.request('POST', '/api/device/poll', {'sessions': [session]})
            self.assertEqual(error.exception.status, 400)
