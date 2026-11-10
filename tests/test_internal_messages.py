import hashlib
import unittest

from scripts.devtools import APIError
import test_relay as fixtures


class InternalMessagesCase(unittest.TestCase):
    setUp = fixtures.ServerCase.setUp
    tearDown = fixtures.ServerCase.tearDown
    register = fixtures.ServerCase.register

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
