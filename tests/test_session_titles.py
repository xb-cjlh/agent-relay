"""Native session titles remain separate from stable CLI names and identities."""
import tempfile
import hashlib
import unittest
from pathlib import Path
from urllib.parse import urlencode
from scripts.devtools import APIError, Client, Server
from test_relay import TOKEN


class SessionTitlesCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server(('127.0.0.1',0),self.root/'data',TOKEN)
        self.client = Client('http://127.0.0.1:'+str(self.server.server_port),TOKEN)
        self.identity = self.client.request('POST','/api/register',{'name':'titles','platform':'test','roots':[],
            'capabilities':{'codex':True,'claude':True,'unlimited_tasks':True}})
        self.device = Client(self.client.server,self.identity['token'])

    def tearDown(self):
        self.server.close();self.temp.cleanup()

    def sync(self,kind='codex',sid='title-session',**fields):
        return self.device.request('POST','/api/device/poll',{'sessions':[{
            'kind':kind,'session_id':sid,'name':kind+' generated session name','cwd':str(self.root),'updated_at':1000,'busy':False,**fields}]})

    def agents(self,**params):
        return self.client.request('GET','/api/agents?'+urlencode(params))['agents']

    def test_titles_update_without_renaming_merging_or_reordering_sessions(self):
        self.sync(title='修复登录页面')
        self.sync('claude',title='修复登录页面')
        self.assertEqual(len(self.agents(q='修复登录')),2)
        original=next(a for a in self.agents() if a['kind']=='codex')
        self.sync(title='新增图片上传')
        updated=next(a for a in self.agents() if a['kind']=='codex')
        self.assertEqual(updated['id'],original['id'])
        self.assertEqual(updated['name'],original['name'])
        self.assertEqual(updated['last_activity'],original['last_activity'])
        self.assertEqual(updated['title'],'新增图片上传')
        self.assertEqual(self.client.request('GET','/api/agents/'+updated['id']+'/messages')['agent']['title'],'新增图片上传')
        grouped=self.client.request('GET','/api/state?'+urlencode({'view':'web','organization':'directory','q':'图片上传'}))
        self.assertEqual(grouped['directories'][0]['session_count'],1)
        self.assertEqual(grouped['agents'][0]['id'],updated['id'])
        # Polling older daemons does not erase titles supplied by an upgraded daemon.
        self.sync()
        self.assertEqual(self.agents(q='图片上传')[0]['title'],'新增图片上传')
        self.sync(title='')
        self.assertEqual(self.agents(q='图片上传'),[])
        self.assertEqual(len(self.agents()),2)
        self.assertEqual(self.agents(q='修复登录')[0]['kind'],'claude')

    def test_relay_created_sessions_receive_the_original_client_title(self):
        agent=self.client.request('POST','/api/agents',{'device':self.identity['id'],'kind':'codex','name':'stable-cli-name','cwd':str(self.root)})
        job=self.client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'fixture'})
        self.device.request('POST','/api/device/poll',{'capacity':1})
        self.device.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','session_id':'relay-session','output':'fixture'})
        self.sync(sid='relay-session',title='Agent 为新会话生成的名字')
        matches=self.agents(q='生成的名字')
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]['id'],agent['id'])
        self.assertEqual(matches[0]['name'],'stable-cli-name')
        self.assertEqual(matches[0]['origin'],'relay')

    def test_title_validation_rolls_back_and_empty_title_is_valid(self):
        self.sync(title='original')
        for invalid in (None,1,{},'名'*257,'bad\0title'):
            with self.assertRaises(APIError) as error:self.sync(title=invalid)
            self.assertEqual(error.exception.status,400)
            self.assertEqual(self.agents()[0]['title'],'original')
        self.sync(title='名'*256)
        self.assertEqual(self.agents()[0]['title'],'名'*256)
        self.sync(title=' \n ')
        self.assertEqual(self.agents()[0]['title'],'')

    def test_custom_title_favorite_survive_sync_and_restart(self):
        for kind in ('codex','claude'):
            self.sync(kind,title='原生标题')
        original=self.agents()
        for agent in original:
            reply=self.client.request('POST','/api/agents/'+agent['id']+'/settings',{'custom_title':'  自定义\n 名字 '+agent['kind'],'favorite':True})
            self.assertTrue(reply['favorite'])
            self.sync(agent['kind'],title='后续自动标题')
        page=self.client.request('GET','/api/state?view=web&organization=favorites')
        self.assertEqual(len(page['agents']),2)
        for agent in page['agents']:
            old=next(a for a in original if a['id']==agent['id'])
            self.assertEqual(agent['custom_title'],'自定义 名字 '+agent['kind'])
            self.assertEqual(agent['title'],'后续自动标题')
            self.assertEqual((agent['name'],agent['last_activity']),(old['name'],old['last_activity']))
        self.assertEqual(len(self.agents(q='自定义 名字')),2)
        database=self.server.database
        self.server.shutdown()
        self.server=Server(('127.0.0.1',0),self.root/'data',TOKEN,database=database)
        self.client=Client('http://127.0.0.1:'+str(self.server.server_port),TOKEN)
        self.assertEqual(len(self.client.request('GET','/api/agents?organization=favorites')['agents']),2)
        agent=self.agents()[0]
        self.client.request('POST','/api/agents/'+agent['id']+'/settings',{'custom_title':'','favorite':False})
        self.assertEqual(self.agents(q='自定义 名字 '+agent['kind']),[])
        self.assertEqual(len(self.client.request('GET','/api/agents?organization=favorites')['agents']),1)
        self.assertEqual(self.client.request('GET','/api/state')['jobs'],[])

    def test_settings_validate_atomically_and_reject_other_accounts_and_device_tokens(self):
        self.sync(title='native')
        agent=self.agents()[0];path='/api/agents/'+agent['id']+'/settings'
        for invalid in ({},{'title':'wrong field'},{'custom_title':None},{'custom_title':'a'*257},
                        {'custom_title':'bad\0title'},{'favorite':1},{'favorite':'true'},
                        {'custom_title':'must roll back','favorite':None}):
            with self.assertRaises(APIError) as error:self.client.request('POST',path,invalid)
            self.assertEqual(error.exception.status,400)
            unchanged=self.agents()[0]
            self.assertEqual(unchanged['custom_title'],'')
            self.assertFalse(unchanged['favorite'])
        with self.assertRaises(APIError) as error:self.device.request('POST',path,{'favorite':True})
        self.assertEqual(error.exception.status,403)
        other_token='sk-relay_'+'p'*43
        self.server.database.execute("INSERT INTO app_users(id,email,display_name,created_at) VALUES('other','other@test.invalid','Other',0)")
        self.server.database.execute("INSERT INTO agent_keys(id,user_id,name,token_hash,token_prefix,created_at,expires_at) VALUES('other-key','other','Other',?,?,0,4102444800)",
            (hashlib.sha256(other_token.encode()).hexdigest(),other_token[:17]))
        other=Client(self.client.server,other_token)
        with self.assertRaises(APIError) as error:other.request('POST',path,{'favorite':True})
        self.assertEqual(error.exception.status,404)
        self.assertEqual(other.request('GET','/api/agents?organization=favorites')['agents'],[])
