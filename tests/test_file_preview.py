"""Private standalone preview metadata/grants and isolated HTML response policy."""
import unittest
from urllib.request import urlopen
from scripts.devtools import Client, Server
from auth_support import BrowserClient
from oss_support import claims, put, upload_image
import test_file_shares as shares
import test_accounts as accounts
from test_relay import TOKEN


class FilePreviewCase(unittest.TestCase):
    setUp=accounts.AccountsCase.setUp
    tearDown=accounts.AccountsCase.tearDown
    denied=accounts.AccountsCase.denied
    prepare=shares.FileSharesCase.prepare
    artifact=shares.FileSharesCase.artifact

    def test_private_artifact_attachment_image_metadata_owner_and_direct_download(self):
        self.prepare();data=b'<h1>preview</h1>';file=self.artifact(data,'report.html')
        ticket=self.client.request('POST','/api/attachments/uploads',claims(data,'uploaded.htm','text/html'));put(ticket,data)
        attachment=self.client.request('POST','/api/attachments/uploads/'+ticket['uploadId']+'/complete',{})
        image=upload_image(self.client,accounts.PNG)
        other=BrowserClient(self.url);other.register(self.mail,'preview-other@example.com')
        for id,expected in ((file,data),(attachment['id'],data),(image['id'],accounts.PNG)):
            path='/api/file-preview/'+id
            self.denied(401,BrowserClient(self.url),'GET',path)
            self.denied(403,self.dev,'GET',path)
            self.denied(404,other,'GET',path)
            grant=self.client.request('GET',path)
            self.assertEqual(id,grant['id']);self.assertEqual(len(expected),grant['size'])
            self.assertEqual(64,len(grant['sha256']));self.assertEqual('oss',grant['storage'])
            for private in ('object_key','owner_user_id','job_id','device_id','/private/'):
                self.assertNotIn(private,grant)
            with urlopen(grant['url']) as response:
                self.assertEqual(expected,response.read());self.assertIn('attachment;',response.headers['Content-Disposition'])
        self.assertFalse(self.client.request('GET','/api/file-shares?file='+file)['shares'])
        self.server.database.execute('UPDATE file_objects SET expires_at=0 WHERE artifact_id=?',(file,))
        self.denied(404,self.client,'GET','/api/file-preview/'+file)

    def test_unconfigured_oss_keeps_authenticated_local_preview_working(self):
        self.server.shutdown()
        self.server=Server(('127.0.0.1',0),self.root/'data',TOKEN,database=self.server.database,isolated=True,environment=self.mail.environment())
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.prepare();file=self.artifact(b'local preview','local.htm')
        grant=self.client.request('GET','/api/file-preview/'+file)
        self.assertEqual('local',grant['storage']);self.assertEqual('/api/files/'+file,grant['url'])
        self.assertEqual(b'local preview',self.client.request('GET',grant['url'],raw=True)[0])
        self.denied(401,BrowserClient(self.url),'GET',grant['url'])

    def test_public_shell_and_html_sandbox_headers_do_not_relax_application_policy(self):
        for path in ('/file','/file/','/html-preview.html','/html-renderer.js'):
            with urlopen(self.url+path) as response:
                self.assertEqual('no-store',response.headers['Cache-Control'])
                self.assertEqual('no-referrer',response.headers['Referrer-Policy'])
                self.assertTrue(response.read())
                if path.startswith('/file'):self.assertIn('noindex',response.headers['X-Robots-Tag'])
                if path=='/html-preview.html':
                    policy=response.headers['Content-Security-Policy']
                    for rule in ("sandbox allow-scripts","default-src 'none'","connect-src 'none'","frame-src 'none'","form-action 'none'","style-src 'unsafe-inline'"):
                        self.assertIn(rule,policy)
                    self.assertNotIn('allow-same-origin',policy)
                    self.assertNotIn('https:;',policy)
                    self.assertIn('camera=()',response.headers['Permissions-Policy'])
        with urlopen(self.url) as response:
            self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
            self.assertNotIn('unsafe-inline',response.headers['Content-Security-Policy'])
