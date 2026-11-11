"""Shared file cache and signed transfers against real Java/MySQL and an OSS origin."""
import base64
import hashlib
import os
from unittest.mock import patch
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit, unquote
from urllib.request import urlopen
from scripts.devtools import Client, download
from oss_support import claims, put
from auth_support import BrowserClient
import test_accounts as accounts
from test_relay import TOKEN


class OssFilesCase(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ,RELAY_FILES_CLEANUP_INITIAL_MS="1000",RELAY_FILES_CLEANUP_INTERVAL_MS="1000"):
            accounts.AccountsCase.setUp(self)
    tearDown=accounts.AccountsCase.tearDown
    denied=accounts.AccountsCase.denied

    def prepare(self):
        self.client=Client(self.url,TOKEN)
        self.identity=accounts.AccountsCase.device(self,self.client)
        self.device=Client(self.url,self.identity['token'])
        self.body={'kind':'file_read','device':self.identity['id'],'path':'/docs/report.md'}

    def job(self):
        job=self.client.request('POST','/api/jobs',self.body)
        self.device.request('POST','/api/device/poll',{'capacity':4})
        return job

    def finish(self,job,file):
        return self.device.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':file})

    def cached(self):
        return self.client.request('GET','/api/files/cache?'+urlencode({'device':self.identity['id'],'path':self.body['path']}))['file']

    def test_legacy_files_share_cloud_cache_offline_and_conditional_checks_extend_expiry(self):
        self.prepare(); data=b'# cached original\n'
        job=self.job();result=self.finish(job,{'name':'report.md','data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()})
        artifact=result['artifact_id'];cached=self.cached()
        self.assertEqual(artifact,cached['id'])
        self.assertGreater(cached['expires_at']-time.time(),29*86400)
        self.assertFalse(list(self.server.directory.joinpath('files').iterdir()))
        # Device goes offline; a fresh browser/account client can still fetch OSS.
        self.server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?',(self.identity['id'],))
        other_browser=Client(self.url,TOKEN)
        self.assertEqual(artifact,other_browser.request('GET','/api/files/cache?'+urlencode({'device':self.identity['id'],'path':self.body['path']}))['file']['id'])
        before=len(self.oss.requests)
        grant=other_browser.request('GET','/api/files/'+artifact+'/url')
        self.assertEqual(before,len(self.oss.requests))
        self.assertEqual('oss',grant['storage'])
        query=parse_qs(urlsplit(grant['url']).query)
        self.assertLessEqual(int(query['x-oss-expires'][0]),300)
        self.assertIn('attachment',query['response-content-disposition'][0])
        with urlopen(grant['url']) as response:self.assertEqual(data,response.read())
        output=self.root/'download.md';download(self.client,artifact,output)
        self.assertEqual(data,output.read_bytes())
        self.assertEqual(data,self.client.request('GET','/api/files/'+artifact,raw=True)[0]) # Old CLI compatibility.
        self.server.database.execute('UPDATE file_objects SET expires_at=? WHERE artifact_id=?',(time.time()+100,artifact))
        before=len([r for r in self.oss.requests if r['method']=='PUT'])
        conditional=self.job()
        self.assertEqual(hashlib.sha256(data).hexdigest(),conditional['payload']['if_sha256'])
        unchanged=self.finish(conditional,{'unchanged':True,'sha256':conditional['payload']['if_sha256']})
        self.assertEqual(artifact,unchanged['artifact_id'])
        self.assertEqual(before,len([r for r in self.oss.requests if r['method']=='PUT']))
        self.assertGreater(self.cached()['expires_at']-time.time(),29*86400)
        for method,path,body in [('GET','/api/files/'+artifact+'/url',None),('GET','/api/files/cache?'+urlencode({'device':self.identity['id'],'path':self.body['path']}),None)]:
            self.denied(401,BrowserClient(self.url),method,path,body)
        self.browser.register(self.mail,'other-cache@example.com')
        self.denied(404,self.browser,'GET','/api/files/'+artifact+'/url')
        self.denied(404,self.browser,'GET','/api/files/cache?'+urlencode({'device':self.identity['id'],'path':self.body['path']}))
        self.server.database.execute('UPDATE file_objects SET expires_at=0 WHERE artifact_id=?',(artifact,))
        self.assertIsNone(self.cached());self.denied(404,self.client,'GET','/api/files/'+artifact+'/url')
        end=time.monotonic()+5
        while self.oss.objects and time.monotonic()<end:time.sleep(.1)
        self.assertFalse(self.oss.objects,'expired cloud objects were not cleaned')

    def test_direct_upload_scope_claims_replay_and_missing_objects(self):
        self.prepare();data=b'original bytes';job=self.job();path='/api/device/jobs/'+job['id']+'/file-upload'
        body=claims(data,'report.md','application/octet-stream')
        self.denied(403,self.client,'POST',path,body)
        unrelated=accounts.AccountsCase.device(self,self.client,'other')
        self.denied(404,Client(self.url,unrelated['token']),'POST',path,body)
        ticket=self.device.request('POST',path,body)
        self.assertEqual(ticket['upload_id'],self.device.request('POST',path,body)['upload_id'])
        self.denied(409,self.device,'POST',path,{**body,'name':'changed.md'})
        self.denied(409,self.device,'POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'upload_id':ticket['upload_id']}})
        put(ticket,data)
        key=unquote(urlsplit(ticket['url']).path);item=self.oss.objects[key]
        item['headers']['x-oss-meta-sha256']='0'*64
        self.denied(409,self.device,'POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'upload_id':ticket['upload_id']}})
        item['headers']['x-oss-meta-sha256']=body['sha256']
        result=self.finish(job,{'upload_id':ticket['upload_id']})
        self.assertEqual(result,self.finish(job,{'upload_id':ticket['upload_id']}))
        self.assertFalse(any(r['method']=='GET' for r in self.oss.requests)) # Completion only uses HEAD.
        self.assertEqual(result['artifact_id'],self.cached()['id'])
        self.oss.objects.pop(key)
        self.assertIsNone(self.cached())
        retry=self.job();self.assertNotIn('if_sha256',retry['payload'])
        expired=self.device.request('POST','/api/device/jobs/'+retry['id']+'/file-upload',body)
        self.server.database.execute('UPDATE file_uploads SET expires_at=0 WHERE id=?',(expired['upload_id'],))
        self.denied(410,self.device,'POST','/api/device/jobs/'+retry['id']+'/complete',{'status':'succeeded','file':{'upload_id':expired['upload_id']}})
