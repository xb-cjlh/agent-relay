"""Expiring, revocable anonymous shares against isolated real Java/MySQL and OSS."""
import json
import time
import unittest
from urllib.request import urlopen
from urllib.parse import urlsplit, parse_qs
from scripts.devtools import Client
from auth_support import BrowserClient
from oss_support import upload_image, claims, put
import test_accounts as accounts
from test_relay import TOKEN


class SharesCase(unittest.TestCase):
    setUp = accounts.AccountsCase.setUp
    tearDown = accounts.AccountsCase.tearDown
    denied = accounts.AccountsCase.denied

    def fixture(self):
        self.client = Client(self.url, TOKEN)
        self.device = self.client.request('POST', '/api/register', {'name': 'private-device', 'platform': 'test', 'roots': [], 'capabilities': {'codex': True, 'image_input': True, 'file_input': True, 'unlimited_tasks': True}})
        self.agent = self.client.request('POST', '/api/agents', {'device': self.device['id'], 'name': 'Shared conversation', 'kind': 'codex', 'cwd': '/private/project'})
        self.dev = Client(self.url, self.device['token'])
        return '/api/agents/'+self.agent['id']+'/shares'

    def share(self, body=None):
        return self.client.request('POST', '/api/agents/'+self.agent['id']+'/shares', body or {})

    def test_token_is_hashed_scoped_readonly_revocable_and_owner_managed(self):
        path = self.fixture()
        self.server.database.execute('UPDATE agents SET title=? WHERE id=?',('名'*256,self.agent['id']))
        self.client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': self.agent['id'], 'prompt': 'private question'})
        other = BrowserClient(self.url); other.register(self.mail, 'other@example.com')
        self.denied(401, BrowserClient(self.url), 'POST', path, {})
        self.denied(404, other, 'POST', path, {})
        share = self.share({'expiresIn': 3600}); token = share['token']; public = Client(self.url, token)
        self.assertRegex(token, r'^shr-relay_[A-Za-z0-9_-]{43}$')
        self.assertEqual('/share#'+token, share['path'])
        row = self.server.database.execute('SELECT token_hash,snapshot,resources FROM conversation_shares WHERE id=?', (share['id'],))
        self.assertNotIn(token, row)
        view = public.request('GET', '/api/shared/conversation')
        self.assertEqual('名'*256, view['title'])
        self.assertEqual('private question', view['messages'][0]['content'])
        encoded = json.dumps(view)
        for secret in (self.agent['id'], self.device['id'], '/private/project', 'diagnostics', 'approvals', 'owner_user_id', 'session_id'):
            self.assertNotIn(secret, encoded)
        listing = self.client.request('GET', path)
        self.assertNotIn(token, json.dumps(listing)); self.assertNotIn('token_hash', json.dumps(listing))
        self.denied(404, other, 'GET', path)
        self.denied(404, other, 'POST', '/api/shares/'+share['id']+'/revoke', {})
        for private in ('/api/state', path, '/api/jobs/'+self.agent['id']):self.denied(401, public, 'GET', private)
        self.denied(401, public, 'POST', '/api/jobs', {'kind': 'prompt', 'agent': self.agent['id'], 'prompt': 'forbidden'})
        self.denied(405, public, 'POST', '/api/shared/conversation', {})
        self.denied(404, self.client, 'GET', '/api/shared/conversation')
        self.denied(404, Client(self.url, token[:-1] + ('A' if token[-1] != 'A' else 'B')), 'GET', '/api/shared/conversation')
        self.denied(404, BrowserClient(self.url), 'GET', '/api/shared/conversation?token='+token)
        self.client.request('POST', '/api/shares/'+share['id']+'/revoke', {})
        self.client.request('POST', '/api/shares/'+share['id']+'/revoke', {})
        self.denied(404, public, 'GET', '/api/shared/conversation')
        self.assertIn('{}\t{}', self.server.database.execute('SELECT snapshot,resources FROM conversation_shares WHERE id=?', (share['id'],)))

    def test_snapshot_contains_all_pages_excludes_internal_and_future_messages(self):
        self.fixture()
        messages = [{'id': 'message-'+str(i), 'role': 'user' if i%2 == 0 else 'assistant', 'content': 'message '+str(i), 'created_at': 1000+i} for i in range(230)]
        session = {'kind': 'codex', 'session_id': 'native-share', 'name': 'Shared conversation', 'cwd': '/private/project', 'updated_at': 1230, 'messages': messages + [{'id': 'hidden', 'role': 'assistant', 'content': 'internal runtime', 'internal': True, 'created_at': 999}]}
        for start in range(0,len(session['messages']),100):
            self.dev.request('POST', '/api/device/poll', {'sessions': [{**session,'messages':session['messages'][start:start+100]}]})
        agent = next(a for a in self.client.request('GET', '/api/state')['agents'] if a.get('session_id') == 'native-share')
        self.agent = agent
        share = self.share(); public = Client(self.url, share['token'])
        self.assertEqual(230, share['message_count'])
        collected = []; path = '/api/shared/conversation'
        while True:
            page = public.request('GET', path); collected = page['messages'] + collected
            if not page['has_more']:break
            path = '/api/shared/conversation?before='+str(page['next_before'])
        self.assertEqual([m['content'] for m in messages], [m['content'] for m in collected])
        self.denied(400, public, 'GET', '/api/shared/conversation?before=NaN')
        self.denied(400, public, 'GET', '/api/shared/conversation?before=10000')
        session['messages'].append({'id': 'future', 'role': 'user', 'content': 'future secret', 'created_at': 1231})
        self.dev.request('POST', '/api/device/poll', {'sessions': [{**session,'messages':session['messages'][-1:]}]})
        self.assertNotIn('future secret', json.dumps(public.request('GET', '/api/shared/conversation')))
        self.assertNotIn('internal runtime', json.dumps(collected))
        self.server.shutdown()
        from scripts.devtools import Server
        self.server = Server(('127.0.0.1', 0), self.root/'data', TOKEN, database=self.server.database, isolated=True, environment={**self.mail.environment(), **self.oss.environment()})
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)
        self.assertEqual(page['title'], Client(self.url, share['token']).request('GET', '/api/shared/conversation')['title'])

    def test_expiration_limits_cleanup_and_response_headers(self):
        path = self.fixture()
        for invalid in (0, 59, 2592001, True, '3600', None):self.denied(400, self.client, 'POST', path, {'expiresIn': invalid})
        share = self.share({'expiresIn': 60}); public = Client(self.url, share['token'])
        _, headers = BrowserClient(self.url).request('GET', '/api/shared/conversation', raw=True, headers={'Authorization':'Bearer '+share['token']})
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertEqual('no-referrer', headers['Referrer-Policy'])
        self.assertIn('noindex', headers['X-Robots-Tag'])
        self.server.database.execute('UPDATE conversation_shares SET expires_at=0 WHERE id=?', (share['id'],))
        self.denied(404, public, 'GET', '/api/shared/conversation')
        self.share()
        self.assertIn('{}', self.server.database.execute('SELECT snapshot FROM conversation_shares WHERE id=?', (share['id'],)))
        for path in ('/share', '/share/', '/share.js', '/share.css'):
            with urlopen(self.url+path) as r:
                self.assertEqual(200, r.status);self.assertEqual('no-store', r.headers['Cache-Control'])
                self.assertIn('noindex', r.headers['X-Robots-Tag'])

    def test_only_snapshot_attachments_get_short_lived_direct_oss_grants(self):
        self.fixture()
        image = upload_image(self.client, accounts.PNG)
        data = b'%PDF-share\x00'
        ticket = self.client.request('POST', '/api/attachments/uploads', claims(data, 'report.pdf', 'application/pdf'));put(ticket, data)
        file = self.client.request('POST', '/api/attachments/uploads/'+ticket['uploadId']+'/complete', {})
        job = self.client.request('POST', '/api/jobs', {'kind':'prompt','agent':self.agent['id'],'prompt':'look','images':[image['id']],'files':[file['id']]})
        self.dev.request('POST','/api/device/poll',{'capacity':1})
        self.dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','output':'answer','diagnostics':'private diagnostics'})
        outsider = upload_image(self.client, accounts.PNG, 'not-shared.png')
        share = self.share({'expiresIn':60});public = Client(self.url, share['token'])
        view = public.request('GET','/api/shared/conversation')
        self.assertEqual(image['id'],view['messages'][0]['images'][0]['id'])
        self.assertEqual(file['id'],view['messages'][0]['files'][0]['id'])
        self.assertNotIn('private diagnostics',json.dumps(view))
        count = len(self.oss.requests)
        imagegrant = public.request('GET','/api/shared/images/'+image['id']+'/url')
        filegrant = public.request('GET','/api/shared/attachments/'+file['id']+'/url')
        self.assertEqual(count,len(self.oss.requests))
        for grant in (imagegrant,filegrant):
            self.assertLessEqual(grant['expiresAt'],share['expires_at']*1000)
            self.assertLessEqual(int(parse_qs(urlsplit(grant['url']).query)['x-oss-expires'][0]),60)
        with urlopen(imagegrant['url']) as r:self.assertEqual(accounts.PNG,r.read())
        with urlopen(filegrant['url']) as r:self.assertEqual(data,r.read());self.assertTrue(r.headers['Content-Disposition'].startswith('attachment;'))
        for resource in ('images/'+outsider['id'], 'images/'+file['id'], 'attachments/'+image['id']):self.denied(404,public,'GET','/api/shared/'+resource+'/url')
        self.client.request('POST','/api/shares/'+share['id']+'/revoke',{})
        self.denied(404,public,'GET','/api/shared/images/'+image['id']+'/url')
        self.denied(404,public,'GET','/api/shared/attachments/'+file['id']+'/url')

    def test_snapshot_preserves_replies_interleaved_with_followup_and_limits_active_shares(self):
        path=self.fixture()
        first=self.client.request('POST','/api/jobs',{'kind':'prompt','agent':self.agent['id'],'prompt':'original question'})
        self.dev.request('POST','/api/device/poll',{'capacity':1})
        session={'kind':'codex','session_id':'ordered-share','name':'ordered','cwd':'/private/project','busy':True,'updated_at':time.time(),'messages':[]}
        self.dev.request('POST','/api/device/poll',{'active':[{'id':first['id'],'session_id':session['session_id'],'output':'first reply\n'}],'sessions':[session]})
        reply=next(m for m in self.client.request('GET','/api/agents/'+self.agent['id']+'/messages')['messages'] if m['role']=='assistant')
        self.client.request('POST','/api/jobs',{'kind':'prompt','agent':self.agent['id'],'prompt':'followup'})
        session['messages']=[{'id':'native-first','role':'assistant','content':'first reply','created_at':reply['created_at']}]
        self.dev.request('POST','/api/device/poll',{'active':[{'id':first['id'],'session_id':session['session_id'],'output':'first reply\n\nnext reply'}],'sessions':[session]})
        share=self.share()
        view=Client(self.url,share['token']).request('GET','/api/shared/conversation')
        self.assertEqual(['original question','first reply','followup','next reply'],[m['content'].strip() for m in view['messages']])
        for index in range(49):
            self.server.database.execute("INSERT INTO conversation_shares(id,owner_user_id,agent_id,token_hash,title,snapshot,resources,created_at,expires_at) SELECT ?,owner_user_id,agent_id,SHA2(?,256),title,snapshot,resources,created_at,expires_at FROM conversation_shares WHERE id=?",('shr_quota'+str(index),'fixture'+str(index),share['id']))
        self.denied(429,self.client,'POST',path,{})
        self.client.request('POST','/api/shares/'+share['id']+'/revoke',{})
        self.assertIn('token',self.share())
