import base64
import json
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from scripts.devtools import Server,Client,APIError
from auth_support import Mailbox,BrowserClient
from test_relay import TOKEN
from oss_support import OssFixture,upload_image,claims,put

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

class AccountsCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.mail=Mailbox()
        self.oss=OssFixture()
        self.server=Server(('127.0.0.1',0),self.root/'data',TOKEN,environment={**self.mail.environment(),**self.oss.environment()})
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.browser=BrowserClient(self.url)

    def tearDown(self):
        self.server.close();self.mail.close();self.oss.close();self.temp.cleanup()

    def denied(self, status, client, method, path, body=None):
        with self.assertRaises(APIError) as caught:client.request(method,path,body)
        self.assertEqual(status,caught.exception.status)

    def key(self, browser=None):
        return (browser or self.browser).request('POST','/api/agent/keys',{'name':'test','expiresInDays':30})

    def device(self, client, name='home'):
        return client.request('POST','/api/register',{'name':name,'platform':'test','capabilities':{'codex':True,'claude':True,'image_input':True},'roots':[]})

    def test_email_registration_cookie_login_logout_csrf_and_key_lifecycle(self):
        email='alice@example.com'
        self.denied(400,self.browser,'POST','/api/auth/register',{'email':email,'password':'password1','code':'123456'})
        self.browser.register(self.mail,email)
        cookies=list(self.browser.jar)
        self.assertEqual('relay_session',cookies[0].name);self.assertIn('HttpOnly',cookies[0]._rest)
        self.assertEqual('Lax',cookies[0]._rest['SameSite'])
        session=self.browser.request('GET','/api/auth/session');self.assertEqual(email,session['user']['email'])
        self.assertFalse(self.browser.request('GET','/api/state')['devices'])
        key=self.key();self.assertTrue(key['secret'].startswith('sk-relay_'));self.assertNotIn('permission',key)
        listed=self.browser.request('GET','/api/agent/keys');self.assertNotIn(key['secret'],json.dumps(listed));self.assertNotIn('token_hash',json.dumps(listed))
        cli=Client(self.url,key['secret']);device=self.device(cli)
        self.assertEqual(device['id'],cli.request('GET','/api/state')['devices'][0]['id'])
        self.denied(403,cli,'POST','/api/agent/keys',{'name':'forbidden'})
        self.browser.headers={'X-Relay-Request':''}
        self.denied(403,self.browser,'POST','/api/register',{'name':'csrf'})
        self.browser.headers={'Origin':'https://evil.invalid'}
        self.denied(403,self.browser,'POST','/api/agent/keys',{'name':'evil'})
        self.browser.headers={}
        self.browser.request('POST','/api/agent/keys/'+key['id']+'/revoke',{})
        self.denied(401,cli,'GET','/api/state')
        key=self.key();cli=Client(self.url,key['secret'])
        self.server.database.execute('UPDATE agent_keys SET expires_at=0 WHERE id=?',(key['id'],))
        self.denied(401,cli,'GET','/api/state')
        self.browser.request('POST','/api/auth/logout',{})
        self.denied(401,self.browser,'GET','/api/state')
        self.denied(401,self.browser,'POST','/api/auth/login',{'email':email,'password':'incorrect'})
        self.browser.request('POST','/api/auth/login',{'email':email.upper(),'password':'Relay-password-2026'})
        self.assertTrue(self.browser.request('GET','/api/auth/session')['authenticated'])

    def test_two_users_isolate_devices_sessions_jobs_files_images_and_keys(self):
        self.browser.register(self.mail,'alice@example.com');a=self.browser
        b=BrowserClient(self.url);b.register(self.mail,'bob@example.com')
        da=self.device(a);db=self.device(b)
        self.assertNotEqual(da['id'],db['id']) # Same device names allowed in separate accounts.
        aa=a.request('POST','/api/agents',{'device':da['id'],'name':'conversation','kind':'codex','cwd':'/tmp'})
        job=a.request('POST','/api/jobs',{'kind':'prompt','agent':aa['id'],'prompt':'private'})
        self.denied(404,b,'GET','/api/agents/'+aa['id']+'/messages')
        self.denied(404,b,'GET','/api/jobs/'+job['id'])
        self.denied(404,b,'POST','/api/jobs/'+job['id']+'/cancel',{})
        self.denied(404,b,'POST','/api/agents',{'device':da['id'],'name':'bad','kind':'codex','cwd':'/tmp'})
        self.denied(404,b,'POST','/api/devices/merge',{'target':db['id'],'sources':[da['id']]})
        self.assertFalse(b.request('GET','/api/state')['agents'])
        image=upload_image(a,PNG,'secret.png')
        self.denied(404,b,'GET','/api/images/'+image['id'])
        self.denied(404,b,'GET','/api/images/'+image['id']+'/url')
        self.denied(404,b,'POST','/api/jobs',{'kind':'prompt','agent':aa['id'],'prompt':'steal','images':[image['id']]})
        key=self.key(a);self.denied(404,b,'POST','/api/agent/keys/'+key['id']+'/revoke',{})
        f=a.request('POST','/api/jobs',{'kind':'file_read','device':da['id'],'path':'secret'})
        dev=Client(self.url,da['token']);dev.request('POST','/api/device/poll',{'capacity':4})
        result=dev.request('POST','/api/device/jobs/'+f['id']+'/complete',{'status':'succeeded','file':{'name':'secret','data':base64.b64encode(b'private').decode(),'sha256':hashlib.sha256(b'private').hexdigest()}})
        self.denied(404,b,'GET','/api/files/'+result['artifact_id'])

    def test_password_reset_consumes_code_revokes_sessions_and_keys(self):
        email='reset@example.com';self.browser.register(self.mail,email);key=self.key()
        other=BrowserClient(self.url);other.request('POST','/api/auth/login',{'email':email,'password':'Relay-password-2026'})
        self.browser.request('POST','/api/auth/email-code',{'email':email,'purpose':'reset'})
        code=self.mail.code(email)
        reset={'email':email,'password':'New-password-2026','code':code}
        self.browser.request('POST','/api/auth/reset-password',reset)
        self.denied(401,other,'GET','/api/state');self.denied(401,Client(self.url,key['secret']),'GET','/api/state')
        self.denied(400,self.browser,'POST','/api/auth/reset-password',reset)
        self.browser.request('POST','/api/auth/login',{'email':email,'password':'New-password-2026'})

    def test_image_validation_binding_and_device_download_scope(self):
        self.browser.register(self.mail,'image@example.com')
        device=self.device(self.browser);other=self.device(self.browser,'other')
        agent=self.browser.request('POST','/api/agents',{'device':device['id'],'name':'image','kind':'codex','cwd':'/tmp'})
        self.denied(400,self.browser,'POST','/api/images/uploads',claims(b'<svg/>','fake.svg','image/svg+xml'))
        image=upload_image(self.browser,PNG,'picture.png')
        self.assertEqual('image/png',image['mime'])
        job=self.browser.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'images':[image['id']]})
        self.assertEqual(image['id'],job['payload']['images'][0]['id'])
        self.denied(409,self.browser,'POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'images':[image['id']]})
        self.assertNotIn(base64.b64encode(PNG).decode(),json.dumps(self.browser.request('GET','/api/state')))
        dev=Client(self.url,device['token']);another=Client(self.url,other['token'])
        data,headers=dev.request('GET','/api/device/images/'+image['id'],raw=True);self.assertEqual(PNG,data)
        self.denied(404,another,'GET','/api/device/images/'+image['id'])
        self.denied(403,dev,'GET','/api/images/'+image['id'])
        self.denied(403,dev,'GET','/api/images/'+image['id']+'/url')
