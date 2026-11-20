import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from scripts.devtools import APIError, Client, Server, binary
from auth_support import BrowserClient, Mailbox
from push_support import PushFixture
from test_relay import TOKEN

class NotificationsCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.mail=Mailbox();self.push=PushFixture(self.root)
        self.server=Server(('127.0.0.1',0),self.root/'data',TOKEN,environment={**self.mail.environment(),**self.push.environment()})
        self.url='http://127.0.0.1:'+str(self.server.server_port);self.a=BrowserClient(self.url);self.a.register(self.mail,'alice@example.com')
        key=self.a.request('POST','/api/agent/keys',{'name':'test','expiresInDays':30})
        self.cli=Client(self.url,key['secret']);self.key=key['secret']
    def tearDown(self):self.server.close();self.mail.close();self.push.close();self.temp.cleanup()
    def denied(self,status,client,method,path,body=None):
        with self.assertRaises(APIError) as error:client.request(method,path,body)
        self.assertEqual(status,error.exception.status)
    def create(self,**kwargs):return self.cli.request('POST','/api/notifications',{'title':'Secret title','body':'Private body',**kwargs})
    def wait(self,condition):
        deadline=time.monotonic()+8
        while not condition():
            self.assertLess(time.monotonic(),deadline,'Push condition timed out');time.sleep(.1)
        self.assertFalse(self.push.errors)
    def sql(self,sql,args=()):return self.server.database.execute(sql,args)
    def test_manual_idempotency_paging_ownership_and_cli(self):
        body={'idempotencyKey':'same'};first=self.create(**body);self.assertFalse(first['duplicate']);self.assertEqual(0,first['push_queued'])
        self.assertEqual(first['id'],self.create(**body)['id']);self.assertTrue(self.create(**body)['duplicate'])
        self.denied(409,self.cli,'POST','/api/notifications',{'title':'changed','body':'Private body',**body})
        b=BrowserClient(self.url);b.register(self.mail,'bob@example.com')
        self.denied(404,b,'GET','/api/notifications/'+first['id']);self.denied(404,b,'POST','/api/notifications/'+first['id']+'/read',{})
        self.denied(404,b,'GET','/api/notifications?before='+first['id']);self.assertFalse(b.request('GET','/api/notifications')['notifications'])
        self.denied(401,Client(self.url,''),'GET','/api/notifications')
        for i in range(51):self.create(title=str(i))
        page=self.a.request('GET','/api/notifications');self.assertEqual(50,len(page['notifications']));self.assertTrue(page['has_more']);self.assertEqual(52,page['unread'])
        rest=self.a.request('GET','/api/notifications?before='+page['next_cursor']);self.assertEqual(2,len(rest['notifications']));self.assertFalse(rest['has_more'])
        self.a.request('POST','/api/notifications/'+first['id']+'/read',{});self.assertEqual(51,self.a.request('GET','/api/notifications')['unread'])
        env={**os.environ,'AM_SERVER':self.url,'AM_TOKEN':self.key,'HOME':str(self.root)}
        reply=subprocess.run([binary(),'notify','--title','From CLI','--body','-','--idempotency-key','cli'],input='正文\nnext',text=True,capture_output=True,env=env,timeout=20)
        self.assertEqual(0,reply.returncode,reply.stderr);self.assertEqual('正文\nnext',json.loads(reply.stdout)['body'])
        for _ in range(7):self.create()
        self.denied(429,self.cli,'POST','/api/notifications',{'title':'over','body':'limit'})
        self.assertEqual(first['id'],self.create(**body)['id'])
    def test_encrypted_delivery_retry_expiry_and_binding(self):
        config=self.a.request('GET','/api/push/config');self.assertEqual({'configured':True,'publicKey':self.push.public},config)
        self.denied(403,self.cli,'POST','/api/push/subscriptions',self.push.subscription())
        for endpoint in ['http://169.254.169.254/latest','https://example.com/push','https://web.push.apple.com:8443/push']:
            self.denied(400,self.a,'POST','/api/push/subscriptions',{**self.push.subscription(),'endpoint':endpoint})
        self.denied(400,self.a,'POST','/api/push/subscriptions',{**self.push.subscription(),'keys':{'p256dh':'AA','auth':'AA'}})
        sub=self.a.request('POST','/api/push/subscriptions',self.push.subscription());self.push.status=503
        n=self.create();self.assertEqual(1,n['push_queued']);self.wait(lambda:len(self.push.messages)>=1)
        self.wait(lambda:'HTTP_503' in self.sql('SELECT last_error FROM push_deliveries WHERE notification_id=?',(n['id'],)))
        self.assertEqual({'id':n['id'],'title':'Relay','body':'你有一条新的 Agent 通知'},self.push.messages[0])
        # Rebinding the same live subscription must preserve a queued retry.
        self.assertEqual(sub,self.a.request('POST','/api/push/subscriptions',self.push.subscription()))
        self.push.status=201;self.sql('UPDATE push_deliveries SET next_attempt=0 WHERE notification_id=?',(n['id'],))
        self.wait(lambda:'sent' in self.sql('SELECT status FROM push_deliveries WHERE notification_id=?',(n['id'],)))
        self.push.status=410;gone=self.create();self.wait(lambda:'failed' in self.sql('SELECT status FROM push_deliveries WHERE notification_id=?',(gone['id'],)))
        self.assertIn('0',self.sql('SELECT enabled FROM web_push_subscriptions WHERE id=?',(sub['id'],)))
        self.assertEqual(0,self.create()['push_queued'])
    def test_read_logout_and_cross_account_rebind_cancel_pending(self):
        self.a.request('POST','/api/push/subscriptions',self.push.subscription());self.push.status=503
        n=self.create();self.wait(lambda:len(self.push.messages)>0)
        self.a.request('POST','/api/notifications/'+n['id']+'/read',{});self.assertIn('cancelled',self.sql('SELECT status FROM push_deliveries WHERE notification_id=?',(n['id'],)))
        old=self.create();b=BrowserClient(self.url);b.register(self.mail,'bob@example.com')
        b.request('POST','/api/push/subscriptions',self.push.subscription());self.assertIn('cancelled',self.sql('SELECT status FROM push_deliveries WHERE notification_id=?',(old['id'],)))
        self.assertEqual(0,self.create()['push_queued'])
        # Revoking the old account cannot affect the new owner's subscription.
        self.a.request('POST','/api/push/subscriptions/revoke',{'endpoint':self.push.subscription()['endpoint']})
        self.assertEqual(1,b.request('POST','/api/notifications',{'title':'Bob','body':'Own'})['push_queued'])
        b.request('POST','/api/auth/logout',{});self.wait(lambda:'pending' not in self.sql('SELECT status FROM push_deliveries'))
    def test_retry_survives_restart_and_expired_session_stops_new_push(self):
        self.a.request('POST','/api/push/subscriptions',self.push.subscription());self.push.status=503
        n=self.create();self.wait(lambda:'HTTP_503' in self.sql('SELECT last_error FROM push_deliveries WHERE notification_id=?',(n['id'],)))
        database=self.server.database;port=self.server.server_port;self.server.shutdown();self.push.status=201
        database.execute('UPDATE push_deliveries SET next_attempt=0 WHERE notification_id=?',(n['id'],))
        self.server=Server(('127.0.0.1',port),self.root/'data',TOKEN,database=database,environment={**self.mail.environment(),**self.push.environment()})
        self.wait(lambda:'sent' in self.sql('SELECT status FROM push_deliveries WHERE notification_id=?',(n['id'],)))
        self.assertEqual(n['id'],self.a.request('GET','/api/notifications')['notifications'][0]['id'])
        self.sql('UPDATE auth_sessions SET expires_at=0')
        self.assertEqual(0,self.create()['push_queued'])
        self.denied(401,self.a,'POST','/api/push/subscriptions',self.push.subscription())

    def test_task_and_approval_notifications_are_idempotent(self):
        identity=self.cli.request('POST','/api/register',{'name':'test','platform':'fixture','capabilities':{'unlimited_tasks':True,'codex':True,'codex_permissions':True,'writable':True}})
        device=Client(self.url,identity['token']);agent=self.cli.request('POST','/api/agents',{'device':identity['id'],'kind':'codex','name':'fixture','cwd':str(self.root)})
        self.denied(403,device,'GET','/api/notifications');self.denied(404,self.cli,'POST','/api/notifications',{'title':'bad','body':'bad','agent':'unknown'})
        job=self.cli.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'fixture','permission_mode':'ask'})
        device.request('POST','/api/device/poll',{'capacity':1})
        approval={'id':'apr_'+'a'*48,'method':'item/commandExecution/requestApproval','details':{'command':'fixture'}}
        for _ in range(2):device.request('POST','/api/device/jobs/'+job['id']+'/approvals',approval)
        for _ in range(2):device.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','output':'done'})
        notifications=self.cli.request('GET','/api/notifications')['notifications'];self.assertEqual(2,len(notifications));self.assertEqual({'approval','task'},{n['source'] for n in notifications});self.assertTrue(all(n['agent_id']==agent['id'] for n in notifications))
