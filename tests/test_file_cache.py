import base64
import hashlib
import unittest

from scripts.devtools import APIError
import test_relay as fixtures


class FileCacheCase(unittest.TestCase):
    setUp = fixtures.ServerCase.setUp
    tearDown = fixtures.ServerCase.tearDown
    register = fixtures.ServerCase.register

    def complete(self, device, job, content=b'original', **extra):
        device.request('POST', '/api/device/poll', {'capacity': 4})
        file = {'name': 'file.txt', 'data': base64.b64encode(content).decode(), 'sha256': hashlib.sha256(content).hexdigest(), **extra}
        return device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'file': file})

    def test_unchanged_and_legacy_checks_reuse_artifact_but_changed_bytes_replace_it(self):
        identity, device = self.register()
        body = {'kind': 'file_read', 'device': identity['id'], 'path': '/file.txt'}
        original = self.complete(device, self.client.request('POST', '/api/jobs', body))
        body['cached_artifact'] = original['artifact_id']
        conditional = self.client.request('POST', '/api/jobs', body)
        self.assertEqual(conditional['payload']['if_sha256'], hashlib.sha256(b'original').hexdigest())
        device.request('POST', '/api/device/poll', {'capacity': 4})
        reply = {'status': 'succeeded', 'file': {'unchanged': True, 'sha256': conditional['payload']['if_sha256']}}
        result = device.request('POST', '/api/device/jobs/' + conditional['id'] + '/complete', reply)
        retry = device.request('POST', '/api/device/jobs/' + conditional['id'] + '/complete', reply)
        self.assertEqual(result['artifact_id'], original['artifact_id'])
        self.assertEqual(result['artifact_id'], retry['artifact_id'])
        legacy = self.complete(device, self.client.request('POST', '/api/jobs', body))
        self.assertEqual(legacy['artifact_id'], original['artifact_id'])
        self.assertEqual(len(self.client.request('GET', '/api/state')['artifacts']), 1)
        changed = self.complete(device, self.client.request('POST', '/api/jobs', body), b'changed!')
        self.assertNotEqual(changed['artifact_id'], original['artifact_id'])
        self.assertEqual(self.client.request('GET', '/api/files/' + changed['artifact_id'], raw=True)[0], b'changed!')
        self.assertEqual(len(self.client.request('GET', '/api/state')['artifacts']), 2)

    def test_cache_scope_hash_and_conditional_contract_are_enforced(self):
        identity, device = self.register()
        other, _ = self.register('other')
        body = {'kind': 'file_read', 'device': identity['id'], 'path': '/file.txt'}
        original = self.complete(device, self.client.request('POST', '/api/jobs', body))
        cached = {**body, 'cached_artifact': original['artifact_id']}
        for fields, status in [({'device': other['id']}, 404), ({'path': '/other.txt'}, 400), ({'cached_artifact': 'file_absent'}, 404)]:
            with self.assertRaises(APIError) as caught:
                self.client.request('POST', '/api/jobs', {**cached, **fields})
            self.assertEqual(caught.exception.status, status)
        for request in [body, cached]:
            job = self.client.request('POST', '/api/jobs', request)
            device.request('POST', '/api/device/poll', {'capacity': 4})
            with self.assertRaises(APIError):
                device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'file': {'unchanged': True, 'sha256': '0' * 64}})
        self.assertEqual(len(self.client.request('GET', '/api/state')['artifacts']), 1)
