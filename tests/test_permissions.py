import concurrent.futures
import hashlib
import time
import unittest

from scripts.devtools import APIError, Client
import test_relay as relay_tests


class PermissionsCase(unittest.TestCase):
    setUp = relay_tests.ServerCase.setUp
    tearDown = relay_tests.ServerCase.tearDown
    register = relay_tests.ServerCase.register
    create_agent = relay_tests.ServerCase.create_agent

    def denied(self, status, client, method, path, body=None):
        with self.assertRaises(APIError) as error:
            client.request(method, path, body)
        self.assertEqual(status, error.exception.status)

    def setup_job(self):
        _, device = self.register()
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True, 'claude': True, 'writable': True, 'codex_permissions': True}})
        agent = self.create_agent()
        job = self.client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture', 'permission_mode': 'ask'})
        device.request('POST', '/api/device/poll', {'capacity': 1})
        return device, agent, job

    def test_modes_require_upgraded_daemon_and_local_write_authorization(self):
        _, device = self.register()
        agent = self.create_agent()
        body = {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture'}
        self.denied(409, self.client, 'POST', '/api/jobs', {**body, 'permission_mode': 'full'})
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True, 'claude': True, 'writable': False, 'codex_permissions': True}})
        self.denied(403, self.client, 'POST', '/api/jobs', {**body, 'permission_mode': 'ask'})
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True, 'claude': True, 'writable': True, 'codex_permissions': True}})
        for mode in ('ask', 'full'):
            result = self.client.request('POST', '/api/jobs', {**body, 'permission_mode': mode})
            self.assertEqual(result['payload']['permission_mode'], mode)
        self.denied(400, self.client, 'POST', '/api/jobs', {**body, 'permission_mode': 'typo'})
        claude = self.create_agent(name='claude', kind='claude')
        self.denied(409, self.client, 'POST', '/api/jobs', {**body, 'agent': claude['id'], 'permission_mode': 'full'})
        device.request('POST', '/api/device/poll', {'capabilities': {'codex': True, 'claude': True, 'writable': True, 'codex_permissions': True, 'claude_permissions': True}})
        for mode in ('ask', 'full'):
            result = self.client.request('POST', '/api/jobs', {**body, 'agent': claude['id'], 'permission_mode': mode})
            self.assertEqual(result['payload']['permission_mode'], mode)

    def test_claude_uses_same_approval_boundary_with_native_tool_details(self):
        _, device = self.register()
        device.request('POST', '/api/device/poll', {'capabilities': {'claude': True, 'writable': True, 'claude_permissions': True}})
        agent = self.create_agent(name='claude', kind='claude')
        job = self.client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'fixture', 'permission_mode': 'ask'})
        device.request('POST', '/api/device/poll', {'capacity': 1})
        path = '/api/device/jobs/' + job['id'] + '/approvals'
        aid = 'apr_' + 'e' * 48
        request = {'id': aid, 'method': 'claude/tool/requestApproval', 'details': {'tool_name': 'Bash', 'input': {'command': 'fixture'}}}
        device.request('POST', path, request)
        self.denied(400, device, 'POST', path, {**request, 'method': 'item/fileChange/requestApproval'})
        pending = self.client.request('GET', '/api/agents/' + agent['id'] + '/messages')['approvals']
        self.assertEqual(pending[0]['details']['input']['command'], 'fixture')
        self.client.request('POST', '/api/jobs/' + job['id'] + '/approvals/' + aid, {'decision': 'accept'})
        self.assertEqual(device.request('GET', path + '/' + aid)['decision'], 'accept')

    def test_approval_is_isolated_idempotent_and_expired_with_its_job(self):
        device, agent, job = self.setup_job()
        _, other_device = self.register('other')
        # A separate account is created directly in the isolated fixture database.
        key = 'sk-relay_' + 'e' * 43
        self.server.database.execute("INSERT INTO app_users(id,display_name,created_at) VALUES('other','Other',0)")
        self.server.database.execute("INSERT INTO agent_keys(id,user_id,name,token_hash,token_prefix,created_at,expires_at) VALUES('other-key','other','Fixture',?,'fixture',0,4102444800)", (hashlib.sha256(key.encode()).hexdigest(),))
        other = Client(self.url, key)
        base = '/api/device/jobs/' + job['id'] + '/approvals'
        aid = 'apr_' + 'a' * 48
        request = {'id': aid, 'method': 'item/commandExecution/requestApproval', 'details': {'command': 'echo <fixture>', 'cwd': str(self.root)}}
        self.denied(404, other_device, 'POST', base, request)
        created = device.request('POST', base, request)
        self.assertEqual(created['decision'], 'pending')
        self.assertEqual(created['id'], device.request('POST', base, request)['id'])
        conversation = '/api/agents/' + agent['id'] + '/messages'
        self.assertEqual(self.client.request('GET', conversation)['approvals'][0]['details']['command'], 'echo <fixture>')
        answer = '/api/jobs/' + job['id'] + '/approvals/' + aid
        self.denied(404, other, 'POST', answer, {'decision': 'accept'})
        self.denied(403, device, 'POST', answer, {'decision': 'accept'})
        self.denied(404, other_device, 'GET', base + '/' + aid)
        self.denied(400, self.client, 'POST', answer, {'decision': 'acceptForSession'})
        # First answer wins, including concurrent browsers with opposite choices.
        def choose(decision):
            try:
                return self.client.request('POST', answer, {'decision': decision})['decision']
            except APIError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(choose, ['accept', 'decline']))
        self.assertIn(409, results)
        decision = next(result for result in results if result != 409)
        self.assertEqual(device.request('GET', base + '/' + aid)['decision'], decision)
        self.assertEqual(choose(decision), decision)
        self.assertEqual(self.client.request('GET', conversation)['approvals'], [])
        pending = {**request, 'id': 'apr_' + 'b' * 48}
        device.request('POST', base, pending)
        self.client.request('POST', '/api/jobs/' + job['id'] + '/cancel', {})
        self.assertEqual(self.client.request('GET', conversation)['approvals'], [])
        self.denied(409, self.client, 'POST', answer.replace(aid, pending['id']), {'decision': 'accept'})
        self.assertEqual(device.request('GET', base + '/' + pending['id'])['decision'], 'expired')

    def test_resolved_requests_and_lost_device_lease_cannot_be_approved(self):
        device, _, job = self.setup_job()
        base = '/api/device/jobs/' + job['id'] + '/approvals'
        for index in ('c', 'd'):
            aid = 'apr_' + index * 48
            device.request('POST', base, {'id': aid, 'method': 'item/permissions/requestApproval', 'details': {'permissions': {'network': {'enabled': True}}}})
            if index == 'c':
                device.request('POST', base + '/' + aid + '/expire', {})
            else:
                self.server.database.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() - 1, job['id']))
            self.denied(409, self.client, 'POST', '/api/jobs/' + job['id'] + '/approvals/' + aid, {'decision': 'accept'})
