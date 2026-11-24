"""Preview leases are scoped, deduplicated and expire without persisted subscriptions."""
import base64,hashlib,json,time,unittest
from scripts.devtools import Client,APIError
import test_relay as fixtures

class FileWatchesCase(unittest.TestCase):
    setUp=fixtures.ServerCase.setUp
    tearDown=fixtures.ServerCase.tearDown
    def prepare(self):
        identity=self.client.request('POST','/api/register',{'name':'watch','platform':'test','roots':[],'capabilities':{'file_watch':True,'conditional_files':True,'unlimited_tasks':True,'codex':True}})
        self.device=Client(self.url,identity['token']);self.identity=identity
        self.body={'id':'fw_'+'a'*32,'device':identity['id'],'path':'/notes/report.md'}
        return self.client.request('POST','/api/files/watch',self.body)
    def test_watch_deduplication_immediate_claim_metadata_and_last_reader_stop(self):
        initial=self.prepare();self.assertIsNone(initial['file'])
        second={**self.body,'id':'fw_'+'b'*32};self.client.request('POST','/api/files/watch',second)
        watches=self.device.request('POST','/api/device/poll',{})['file_watches']
        self.assertEqual(len(watches),1);self.assertEqual(watches[0]['path'],self.body['path'])
        self.assertLessEqual(watches[0]['ttl'],45)
        job=self.device.request('POST','/api/device/files/refresh',{'path':self.body['path']})['job']
        self.assertEqual(job['status'],'running');self.assertTrue(job['payload']['file_watch'])
        self.assertEqual(job['id'],self.device.request('POST','/api/device/files/refresh',{'path':self.body['path']})['job']['id'])
        data=b'# changed'
        result=self.device.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'name':'report.md','data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}})
        view=self.client.request('GET','/api/state?view=web&file_watch='+self.body['id'])['file_watch']
        self.assertEqual(view['file']['id'],result['artifact_id']);self.assertEqual(view['file']['size'],len(data))
        self.assertNotIn('object_key',json.dumps(view));self.assertNotIn('payload',json.dumps(view))
        conditional=self.device.request('POST','/api/device/files/refresh',{'path':self.body['path']})['job']
        self.assertEqual(conditional['payload']['if_sha256'],hashlib.sha256(data).hexdigest())
        self.client.request('POST','/api/files/watch',{**self.body,'stop':True})
        self.assertEqual(len(self.device.request('POST','/api/device/poll',{})['file_watches']),1)
        self.client.request('POST','/api/files/watch',{**second,'stop':True})
        self.assertEqual(self.device.request('POST','/api/device/poll',{})['file_watches'],[])
        with self.assertRaises(APIError) as e:self.device.request('POST','/api/device/files/refresh',{'path':self.body['path']})
        self.assertEqual(e.exception.status,410)
    def test_scoping_restart_and_ordinary_read_deduplication(self):
        self.prepare()
        other=self.client.request('POST','/api/register',{'name':'other','platform':'test','roots':[],'capabilities':{'file_watch':True}})
        alien=Client(self.url,other['token'])
        with self.assertRaises(APIError) as e:alien.request('POST','/api/device/files/refresh',{'path':self.body['path']})
        self.assertEqual(e.exception.status,410)
        with self.assertRaises(APIError) as e:self.device.request('POST','/api/files/watch',self.body)
        self.assertEqual(e.exception.status,403)
        token='sk-relay_'+'q'*43
        self.server.database.execute("INSERT INTO app_users(id,email,display_name,created_at) VALUES('watch-other','watch-other@test.invalid','Other',0)")
        self.server.database.execute("INSERT INTO agent_keys(id,user_id,name,token_hash,token_prefix,created_at,expires_at) VALUES('watch-other-key','watch-other','Other',?,?,0,4102444800)",(hashlib.sha256(token.encode()).hexdigest(),token[:17]))
        foreign=Client(self.url,token)
        self.assertIsNone(foreign.request('GET','/api/state?view=web&file_watch='+self.body['id'])['file_watch'])
        with self.assertRaises(APIError) as e:foreign.request('POST','/api/files/watch',self.body)
        self.assertEqual(e.exception.status,404)
        job=self.client.request('POST','/api/jobs',{'kind':'file_read','device':self.identity['id'],'path':self.body['path']})
        self.assertIsNone(self.device.request('POST','/api/device/files/refresh',{'path':self.body['path']})['job'])
        self.assertEqual(len(self.client.request('GET','/api/state')['jobs']),1)
        # A file read can use control capacity while model capacity is exhausted.
        claimed=self.device.request('POST','/api/device/poll',{'capacity':0,'steer_capacity':1})['jobs']
        self.assertEqual([j['id'] for j in claimed],[job['id']])
        from scripts.devtools import Server
        database=self.server.database;self.server.shutdown()
        self.server=Server(('127.0.0.1',0),self.root/'data',fixtures.TOKEN,database=database)
        self.client=Client('http://127.0.0.1:'+str(self.server.server_port),fixtures.TOKEN)
        self.assertIsNone(self.client.request('GET','/api/state?view=web&file_watch='+self.body['id'])['file_watch'])
        self.assertTrue(self.client.request('POST','/api/files/watch',self.body)['supported'])
