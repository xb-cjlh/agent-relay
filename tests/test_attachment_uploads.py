"""Arbitrary attachments: real Java/MySQL with a separate private OSS fixture."""
import json
import unittest
from urllib.parse import urlsplit, unquote, parse_qs
from urllib.request import urlopen
from scripts.devtools import Client
from auth_support import BrowserClient
from oss_support import claims, put, upload_image
import test_accounts as accounts
PNG = accounts.PNG
from test_relay import TOKEN


class AttachmentUploadsCase(unittest.TestCase):
    setUp = accounts.AccountsCase.setUp
    tearDown = accounts.AccountsCase.tearDown
    denied = accounts.AccountsCase.denied

    def upload(self, data=b'\x00binary\xff', name='中文 report.pdf'):
        c = Client(self.url, TOKEN)
        ticket = c.request('POST', '/api/attachments/uploads', claims(data, name, 'text/html'))
        self.assertEqual('application/octet-stream', ticket['headers']['Content-Type'])
        put(ticket, data)
        file = c.request('POST', '/api/attachments/uploads/' + ticket['uploadId'] + '/complete', {})
        return file, ticket

    def device(self, name='fixture', files=True):
        return Client(self.url, TOKEN).request('POST', '/api/register', {'name': name, 'platform': 'fixture', 'roots': [], 'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True, 'file_input': files, 'image_input': True}})

    def agent(self, device, kind='codex'):
        return Client(self.url, TOKEN).request('POST', '/api/agents', {'device': device['id'], 'kind': kind, 'cwd': '/tmp', 'name': kind})

    def test_binary_empty_and_active_content_use_signed_direct_downloads(self):
        c = Client(self.url, TOKEN)
        for name, data in [('中文 report.pdf', b'%PDF-1.7\x00\xff'), ('empty.txt', b''), ('page.html', b'<script>alert(1)</script>'), ('bundle.zip', b'PK\x03\x04'), ('report.docx', b'PK\x03\x04document')]:
            file, ticket = self.upload(data, name)
            count = len(self.oss.requests)
            self.assertEqual(file, c.request('POST', '/api/attachments/uploads/' + ticket['uploadId'] + '/complete', {}))
            grant = c.request('GET', '/api/attachments/' + file['id'] + '/url')
            self.assertEqual(count, len(self.oss.requests))  # Neither signing nor replay reads file bytes.
            query = parse_qs(urlsplit(grant['url']).query)
            self.assertIn('attachment;', query['response-content-disposition'][0])
            self.assertNotIn('response-content-type', query)
            with urlopen(grant['url']) as response:
                self.assertEqual(data, response.read())
                self.assertEqual('application/octet-stream', response.headers['Content-Type'])
            self.denied(401, BrowserClient(self.url), 'GET', '/api/attachments/' + file['id'] + '/url')
        file, _ = self.upload(name='../../unsafe\\name\n.html')
        self.assertEqual('name_.html', file['name'])

    def test_owner_device_scope_mixed_messages_and_reuse(self):
        c = Client(self.url, TOKEN)
        other = BrowserClient(self.url); other.register(self.mail, 'other@example.com')
        file, ticket = self.upload()
        d = self.device(); alien = self.device('alien'); agent = self.agent(d)
        dev = Client(self.url, d['token']); wrong = Client(self.url, alien['token'])
        path = '/api/device/attachments/' + file['id'] + '/url'
        self.denied(404, dev, 'GET', path)
        self.denied(404, other, 'GET', '/api/attachments/' + file['id'] + '/url')
        self.denied(404, other, 'POST', '/api/attachments/uploads/' + ticket['uploadId'] + '/complete', {})
        image = upload_image(c, PNG)
        job = c.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'files': [file['id']], 'images': [image['id']]})
        self.assertEqual('请查看这些附件。', job['payload']['prompt'])
        self.assertEqual([file], job['payload']['files'])
        grant = dev.request('GET', path)
        with urlopen(grant['url']) as response:self.assertEqual(b'\x00binary\xff', response.read())
        self.denied(404, wrong, 'GET', path)
        self.denied(403, c, 'GET', path)
        self.denied(409, c, 'POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'files': [file['id']]})
        messages = c.request('GET', '/api/agents/' + agent['id'] + '/messages')['messages']
        self.assertEqual([file], messages[0]['files']); self.assertEqual([image], messages[0]['images'])

    def test_limits_confirmation_integrity_and_old_device(self):
        c = Client(self.url, TOKEN); d = self.device(files=False); agent = self.agent(d)
        self.denied(400, c, 'POST', '/api/attachments/uploads', {**claims(b'x'), 'size': 16*1024*1024+1})
        file, ticket = self.upload()
        self.denied(409, c, 'POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'files': [file['id']]})
        d = self.device('new'); agent = self.agent(d)
        self.denied(400, c, 'POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'files': [file['id']]*2})
        pending = c.request('POST', '/api/attachments/uploads', claims(b'pending'))
        complete = '/api/attachments/uploads/' + pending['uploadId'] + '/complete'
        self.denied(409, c, 'POST', complete, {})
        put(pending, b'pending')
        key = unquote(urlsplit(pending['url']).path)
        self.oss.objects[key]['headers']['x-oss-meta-sha256'] = '0'*64
        self.denied(409, c, 'POST', complete, {})
        self.server.database.execute('UPDATE attachment_uploads SET expires_at=0 WHERE id=?', (pending['uploadId'],))
        self.denied(410, c, 'POST', complete, {})
        # Reserve aggregate limits without allocating large fixture objects.
        files = [self.upload(name=str(i)+'.txt')[0] for i in range(3)]
        for f in files:self.server.database.execute('UPDATE attachments SET size=? WHERE id=?', (16*1024*1024, f['id']))
        self.denied(413, c, 'POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'files': [f['id'] for f in files]})
        for i in range(20):c.request('POST', '/api/attachments/uploads', claims(b'x'))
        self.denied(429, c, 'POST', '/api/attachments/uploads', claims(b'x'))

    def test_native_echo_matches_exact_file_manifest_and_mixed_images(self):
        c = Client(self.url, TOKEN); d = self.device(); dev = Client(self.url, d['token'])
        for kind in ('codex', 'claude'):
            agent = self.agent(d, kind); file, _ = self.upload(); image = upload_image(c, PNG)
            job = c.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'check files', 'files': [file['id']], 'images': [image['id']]})
            dev.request('POST', '/api/device/poll', {'capacity': 1})
            refs = [{'id': file['id'], 'name': file['name'], 'path': '/config/attachments/'+file['id']+'/'+file['name']}]
            echo = 'check files\n\n[Relay attachments]\n'+json.dumps(refs)+'\n[/Relay attachments]'
            if kind == 'codex':echo += '\n<image name=[Image #1] path="/config/images/'+image['id']+'.png">\n</image>'
            session = {'kind': kind, 'session_id': 'native-'+kind, 'name': kind, 'cwd': '/tmp', 'updated_at': 1001, 'messages': [{'id': kind+'-echo', 'role': 'user', 'content': echo, 'created_at': 1000}, {'id': kind+'-extra', 'role': 'user', 'content': echo+'\nextra instruction', 'created_at': 1000}]}
            self.server.database.execute('UPDATE jobs SET created_at=999,started_at=1000 WHERE id=?', (job['id'],))
            dev.request('POST', '/api/device/poll', {'active': [{'id': job['id'], 'session_id': session['session_id'], 'output': ''}], 'sessions': [session]})
            messages = c.request('GET', '/api/agents/'+agent['id']+'/messages')['messages']
            users = [m for m in messages if m['role'] == 'user']
            self.assertEqual(2, len(users), users)
            self.assertEqual(1, sum(m['content'] == 'check files' for m in users))
            self.assertTrue(any(m['content'].endswith('extra instruction') for m in users))
