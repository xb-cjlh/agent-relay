"""Session cursors, complete search, live windows and account boundaries."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from scripts.devtools import APIError, Client, Server
from test_relay import TOKEN


class SessionPaginationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server(('127.0.0.1', 0), self.root / 'data', TOKEN)
        self.client = Client('http://127.0.0.1:' + str(self.server.server_port), TOKEN)
        self.devices = []
        for name in ['work', 'home']:
            identity = self.client.request('POST', '/api/register', {
                'name': name, 'platform': 'fixture', 'roots': [],
                'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True}})
            device = Client(self.client.server, identity['token'])
            sessions = [{
                'kind': 'codex' if name == 'work' else 'claude',
                'session_id': name + '-' + str(i), 'name': name + '-project-' + str(i),
                'cwd': str(self.root / ('project-' + str(i))),
                'updated_at': 1000 + i // 3, 'busy': False,
                'messages': [{'id': name + '-message-' + str(i), 'role': 'user',
                              'content': 'literal %_ target' if i == 0 else 'fixture',
                              'created_at': 1000 + i // 3}]} for i in range(35)]
            device.request('POST', '/api/device/poll', {'sessions': sessions})
            self.devices.append((identity, device))
        self.all = sorted(self.client.request('GET', '/api/state')['agents'],
                          key=lambda a: (-a['last_activity'], a['id']))

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def get(self, path='/api/agents', **params):
        return self.client.request('GET', path + '?' + urlencode(params))

    def test_cursor_ties_counts_and_remembered_conversation(self):
        first = self.get('/api/state', view='web', selected=self.all[-1]['id'])
        self.assertEqual([a['id'] for a in first['agents']], [a['id'] for a in self.all[:30]])
        self.assertTrue(first['has_more'])
        self.assertEqual(first['selected_agent']['id'], self.all[-1]['id'])
        self.assertEqual([d['session_count'] for d in first['devices']], [35, 35])
        seen = list(first['agents'])
        page = first
        while page['has_more']:
            page = self.get(before=page['next_cursor'])
            self.assertLessEqual(len(page['agents']), 30)
            seen.extend(page['agents'])
        self.assertIsNone(page['next_cursor'])
        self.assertEqual([a['id'] for a in seen], [a['id'] for a in self.all])
        self.assertEqual(len(set(a['id'] for a in seen)), 70)

    def test_loaded_window_stays_live_and_web_omits_prompt_outputs(self):
        first = self.get()
        second = self.get(before=first['next_cursor'])
        last = second['agents'][-1]
        through = str(last['last_activity']) + '|' + last['id']
        older = self.all[-1]
        device = next(d for identity, d in self.devices if identity['id'] == older['device_id'])
        device.request('POST', '/api/device/poll', {'sessions': [{
            'kind': older['kind'], 'session_id': older['session_id'], 'cwd': older['cwd'],
            'name': older['name'], 'updated_at': 2000, 'busy': True}]})
        live = self.get('/api/state', view='web', through=through)
        self.assertEqual(len(live['agents']), 61)
        self.assertEqual(live['agents'][0]['id'], older['id'])
        self.assertTrue(live['agents'][0]['busy'])
        self.assertTrue(live['has_more'])
        job = self.client.request('POST', '/api/jobs', {
            'kind': 'prompt', 'agent': older['id'], 'prompt': 'fixture prompt'})
        self.assertEqual(self.get('/api/state', view='web')['jobs'], [])
        self.assertIn(job['id'], [j['id'] for j in self.get('/api/state')['jobs']])

    def test_search_filters_and_account_isolation(self):
        home = self.devices[1][0]['id']
        page = self.get(device=home, q='Claude Code')
        self.assertEqual(len(page['agents']), 30)
        self.assertTrue(all(a['device_id'] == home for a in page['agents']))
        matches = self.get(q='%_')
        self.assertEqual(len(matches['agents']), 2)
        self.assertFalse(matches['has_more'])
        self.assertEqual(len(self.get(q='home-project-0')['agents']), 1)
        other_token = 'sk-relay_' + 'p' * 43
        self.server.database.execute("INSERT INTO app_users(id,email,display_name,created_at) VALUES('other','other@test.invalid','Other',0)")
        self.server.database.execute("INSERT INTO agent_keys(id,user_id,name,token_hash,token_prefix,created_at,expires_at) VALUES('other-key','other','Other',?,?,0,4102444800)",
                                     (hashlib.sha256(other_token.encode()).hexdigest(), other_token[:17]))
        other = Client(self.client.server, other_token)
        self.assertEqual(other.request('GET', '/api/agents')['agents'], [])
        state = other.request('GET', '/api/state?' + urlencode({'view': 'web', 'selected': self.all[0]['id']}))
        self.assertEqual(state['devices'], [])
        self.assertIsNone(state['selected_agent'])
        grouped = other.request('GET', '/api/state?' + urlencode({'view': 'web', 'organization': 'directory', 'directory_device': home, 'cwd': self.all[0]['cwd']}))
        self.assertEqual(grouped['directories'], [])
        self.assertEqual(grouped['agents'], [])
        self.assertIsNone(grouped['active_directory'])
        for cursor in ['NaN|agt_a', 'Infinity|agt_a', '-1|agt_a', 'bad', '1|bad', '1|agt_a|extra']:
            with self.assertRaises(APIError) as error:
                self.get(before=cursor)
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(APIError):
            self.get(q='a' * 201)

    def test_work_directories_include_old_sessions_and_paginate_exact_paths(self):
        identity, device = self.devices[1]
        target = next(a for a in self.all if a['device_id'] == identity['id'] and a['cwd'].endswith('project-0'))
        cwd = target['cwd']
        sessions = [{'kind': 'codex' if i % 2 else 'claude', 'session_id': 'shared-' + str(i),
                     'name': 'shared-' + str(i), 'cwd': cwd, 'updated_at': 2000 + i, 'busy': False} for i in range(34)]
        sessions += [{'kind': 'claude', 'session_id': 'case-sensitive', 'name': 'case', 'cwd': cwd.replace('project-', 'Project-'), 'updated_at': 500, 'busy': False},
                     {'kind': 'codex', 'session_id': 'same-name', 'name': 'same basename', 'cwd': str(self.root / 'other' / 'project-0'), 'updated_at': 400, 'busy': False}]
        device.request('POST', '/api/device/poll', {'sessions': sessions})
        params = dict(view='web', organization='directory', directory_device=identity['id'], cwd=cwd)
        state = self.get('/api/state', **params)
        self.assertEqual(len(state['directories']), 72)
        self.assertEqual(sum(d['session_count'] for d in state['directories']), 106)
        self.assertEqual(state['active_directory']['cwd'], cwd)
        self.assertEqual(state['active_directory']['session_count'], 35)
        self.assertEqual(state['directories'][0], state['active_directory'])
        self.assertEqual(len(state['agents']), 30)
        self.assertEqual({a['kind'] for a in state['agents']}, {'codex', 'claude'})
        second = self.get(directory_device=identity['id'], cwd=cwd, before=state['next_cursor'])
        self.assertEqual(len(second['agents']), 5)
        self.assertFalse(second['has_more'])
        all_rows = state['agents'] + second['agents']
        self.assertEqual(len({a['id'] for a in all_rows}), 35)
        self.assertTrue(all(a['device_id'] == identity['id'] and a['cwd'] == cwd for a in all_rows))
        last = all_rows[-1]
        live = self.get('/api/state', through=str(last['last_activity']) + '|' + last['id'], **params)
        self.assertEqual(len(live['agents']), 35)
        self.assertFalse(live['has_more'])
        # Search reaches old groups and filters their counts across the whole account.
        matches = self.get('/api/state', view='web', organization='directory', q='%_')
        self.assertEqual(len(matches['directories']), 2)
        self.assertTrue(all(d['session_count'] == 1 for d in matches['directories']))
        scoped = self.get('/api/state', view='web', organization='directory', device=self.devices[0][0]['id'])
        self.assertEqual(len(scoped['directories']), 35)
        self.assertTrue(all(d['device_id'] == self.devices[0][0]['id'] for d in scoped['directories']))
        self.assertEqual(self.get(directory_device=identity['id'], cwd=cwd, device=self.devices[0][0]['id'])['agents'], [])
        for invalid in [{'cwd': cwd}, {'directory_device': identity['id'], 'cwd': '/' * 4097}]:
            with self.assertRaises(APIError) as error:
                self.get(**invalid)
            self.assertEqual(error.exception.status, 400)
