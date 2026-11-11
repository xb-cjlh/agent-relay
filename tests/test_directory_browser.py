import time
import unittest
from scripts.devtools import APIError,Client
import test_relay


class DirectoryBrowserCase(unittest.TestCase):
    setUp=test_relay.ServerCase.setUp
    tearDown=test_relay.ServerCase.tearDown

    def register(self,name='folders',enabled=True):
        identity=self.client.request('POST','/api/register',{'name':name,'platform':'test','roots':[str(self.root)],'capabilities':{'codex':True,'unlimited_tasks':True,'directory_browser':enabled}})
        return identity,Client(self.url,identity['token'])

    def test_recent_directories_follow_all_sessions_and_device_scope(self):
        identity,device=self.register()
        sessions=[{'kind':'codex','session_id':'session-'+str(i),'name':'session-'+str(i),'cwd':str(self.root/('project-'+str(i))),'busy':False,'updated_at':time.time()-i*10} for i in range(25)]
        device.request('POST','/api/device/poll',{'capacity':0,'sessions':sessions})
        recent=self.client.request('GET','/api/directories?device='+identity['id'])['directories']
        self.assertEqual(len(recent),20)
        self.assertEqual(recent[0]['cwd'],sessions[0]['cwd'])
        other,_=self.register('another-device')
        self.assertEqual(self.client.request('GET','/api/directories?device='+other['id'])['directories'],[])
        sessions[-1]['updated_at']=time.time()+10
        device.request('POST','/api/device/poll',{'capacity':0,'sessions':[sessions[-1]]})
        self.assertEqual(self.client.request('GET','/api/directories?device='+identity['id'])['directories'][0]['cwd'],sessions[-1]['cwd'])
        with self.assertRaises(APIError):device.request('GET','/api/directories?device='+identity['id'])

    def test_browse_uses_control_slot_and_returns_only_device_owned_result(self):
        identity,device=self.register()
        job=self.client.request('POST','/api/jobs',{'kind':'directory_list','device':identity['id'],'path':str(self.root)})
        claimed=device.request('POST','/api/device/poll',{'capacity':0,'steer_capacity':1})['jobs']
        self.assertEqual([j['id'] for j in claimed],[job['id']])
        self.assertNotIn('steer_only',claimed[0])
        result={'path':str(self.root),'parent':str(self.root.parent),'entries':['a','中文目录'],'next_cursor':''}
        completed=device.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','directory':result})
        import json
        self.assertEqual(json.loads(completed['output']),result)
        self.assertEqual(self.client.request('GET','/api/state?view=web')['jobs'],[])
        old,_=self.register('old',False)
        with self.assertRaises(APIError):self.client.request('POST','/api/jobs',{'kind':'directory_list','device':old['id'],'path':str(self.root)})
