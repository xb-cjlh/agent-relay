"""Standalone immutable file shares: real isolated MySQL and local OSS."""
import base64
import hashlib
import json
import time
import unittest
from urllib.request import urlopen
from urllib.parse import urlsplit, parse_qs
from scripts.devtools import Client, Server
from auth_support import BrowserClient
from oss_support import claims, put, upload_image
import test_accounts as accounts
from test_relay import TOKEN


class FileSharesCase(unittest.TestCase):
    setUp = accounts.AccountsCase.setUp
    tearDown = accounts.AccountsCase.tearDown
    denied = accounts.AccountsCase.denied

    def prepare(self):
        self.client=Client(self.url,TOKEN)
        self.device=self.client.request('POST','/api/register',{'name':'private-files','platform':'test','roots':[],'capabilities':{'codex':True,'image_input':True,'file_input':True,'unlimited_tasks':True}})
        self.dev=Client(self.url,self.device['token'])

    def artifact(self,data=b'# original\n',name='报告.md'):
        job=self.client.request('POST','/api/jobs',{'kind':'file_read','device':self.device['id'],'path':'/private/'+name})
        self.dev.request('POST','/api/device/poll',{'capacity':4})
        result=self.dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'name':name,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}})
        return result['artifact_id']

    def share(self,file,duration=604800):
        return self.client.request('POST','/api/file-shares',{'file':file,'expiresIn':duration})

    def test_file_only_scope_owner_control_and_revocation(self):
        self.prepare();data=b'# original\n';file=self.artifact(data)
        other=BrowserClient(self.url);other.register(self.mail,'other-file@example.com')
        self.denied(401,BrowserClient(self.url),'POST','/api/file-shares',{'file':file})
        self.denied(404,other,'POST','/api/file-shares',{'file':file})
        self.denied(404,other,'GET','/api/file-shares?file='+file)
        self.denied(403,self.dev,'POST','/api/file-shares',{'file':file})
        share=self.share(file,60);public=Client(self.url,share['token'])
        view=public.request('GET','/api/shared/content')
        self.assertEqual('file',view['type']);self.assertEqual(file,view['file']['id'])
        self.assertEqual('报告.md',view['file']['name'])
        self.assertNotIn('/private',json.dumps(view));self.assertNotIn('object_key',json.dumps(view))
        self.assertNotIn(share['token'],self.server.database.execute('SELECT token_hash,snapshot,resources FROM conversation_shares WHERE id=?',(share['id'],)))
        listing=self.client.request('GET','/api/file-shares?file='+file)
        self.assertEqual(share['id'],listing['shares'][0]['id']);self.assertNotIn('token',json.dumps(listing))
        for path in ('conversation','images/img_unknown/url','attachments/att_unknown/url'):
            self.denied(404,public,'GET','/api/shared/'+path)
        self.denied(401,public,'GET','/api/files/'+file+'/url')
        self.denied(404,self.client,'GET','/api/shared/content')
        grant=public.request('GET','/api/shared/file/url')
        self.assertLessEqual(grant['expiresAt'],share['expires_at']*1000)
        self.assertLessEqual(int(parse_qs(urlsplit(grant['url']).query)['x-oss-expires'][0]),60)
        with urlopen(grant['url']) as response:self.assertEqual(data,response.read())
        self.denied(404,other,'POST','/api/shares/'+share['id']+'/revoke',{})
        self.client.request('POST','/api/shares/'+share['id']+'/revoke',{})
        self.denied(404,public,'GET','/api/shared/content');self.denied(404,public,'GET','/api/shared/file/url')
        self.assertEqual([],self.client.request('GET','/api/file-shares?file='+file)['shares'])

    def test_immutable_version_retention_offline_and_restart(self):
        self.prepare();data=b'first version';first=self.artifact(data)
        self.server.database.execute('UPDATE file_objects SET expires_at=? WHERE artifact_id=?',(time.time()+60,first))
        share=self.share(first,2592000)
        retention=float(self.server.database.execute('SELECT expires_at FROM file_objects WHERE artifact_id=?',(first,)).strip().splitlines()[-1])
        self.assertGreaterEqual(retention,share['expires_at'])
        newer=self.artifact(b'second version');self.assertNotEqual(first,newer)
        self.server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?',(self.device['id'],))
        self.server.shutdown()
        self.server=Server(('127.0.0.1',0),self.root/'data',TOKEN,database=self.server.database,isolated=True,environment={**self.mail.environment(),**self.oss.environment()})
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        public=Client(self.url,share['token'])
        self.assertEqual(first,public.request('GET','/api/shared/content')['file']['id'])
        with urlopen(public.request('GET','/api/shared/file/url')['url']) as response:self.assertEqual(data,response.read())

    def test_sent_attachments_images_and_conversation_share_scope(self):
        self.prepare()
        agent=self.client.request('POST','/api/agents',{'device':self.device['id'],'kind':'codex','cwd':'/tmp','name':'parent private conversation'})
        image=upload_image(self.client,accounts.PNG)
        ticket=self.client.request('POST','/api/attachments/uploads',claims(b'%PDF\x00','report.pdf','application/pdf'));put(ticket,b'%PDF\x00')
        file=self.client.request('POST','/api/attachments/uploads/'+ticket['uploadId']+'/complete',{})
        for item in (image,file):self.denied(409,self.client,'POST','/api/file-shares',{'file':item['id']})
        self.client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'private text','files':[file['id']],'images':[image['id']]})
        for item,expected in ((image,accounts.PNG),(file,b'%PDF\x00')):
            share=self.share(item['id']);public=Client(self.url,share['token'])
            self.assertNotIn('private text',json.dumps(public.request('GET','/api/shared/content')))
            with urlopen(public.request('GET','/api/shared/file/url')['url']) as response:self.assertEqual(expected,response.read())
        conversation=self.client.request('POST','/api/agents/'+agent['id']+'/shares',{})
        self.denied(404,Client(self.url,conversation['token']),'GET','/api/shared/file/url')

    def test_invalid_expired_missing_integrity_and_combined_quota(self):
        self.prepare();file=self.artifact(b'')
        for invalid in (59,2592001,True,None,'3600'):
            self.denied(400,self.client,'POST','/api/file-shares',{'file':file,'expiresIn':invalid})
        self.denied(400,self.client,'POST','/api/file-shares',{'file':'../elsewhere'})
        share=self.share(file)
        self.server.database.execute('UPDATE conversation_shares SET expires_at=0 WHERE id=?',(share['id'],))
        self.denied(404,Client(self.url,share['token']),'GET','/api/shared/file/url')
        self.server.database.execute('UPDATE file_objects SET expires_at=0 WHERE artifact_id=?',(file,))
        self.denied(404,self.client,'POST','/api/file-shares',{'file':file})
        file=self.artifact(b'check integrity','check.txt')
        obj=next(obj for obj in self.oss.objects.values() if obj['data']==b'check integrity')
        obj['headers']['x-oss-meta-sha256']='0'*64
        self.denied(409,self.client,'POST','/api/file-shares',{'file':file})
        obj['headers']['x-oss-meta-sha256']=hashlib.sha256(b'check integrity').hexdigest()
        share=self.share(file)
        for index in range(49):
            self.server.database.execute("INSERT INTO conversation_shares(id,owner_user_id,token_hash,title,snapshot,resources,created_at,expires_at,share_type,resource_id) SELECT ?,owner_user_id,SHA2(?,256),title,snapshot,resources,created_at,expires_at,share_type,resource_id FROM conversation_shares WHERE id=?",('shr_filequota'+str(index),'fixture'+str(index),share['id']))
        self.denied(429,self.client,'POST','/api/file-shares',{'file':file})
        agent=self.client.request('POST','/api/agents',{'device':self.device['id'],'kind':'codex','cwd':'/tmp','name':'quota'})
        self.denied(429,self.client,'POST','/api/agents/'+agent['id']+'/shares',{})
