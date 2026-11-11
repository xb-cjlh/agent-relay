"""Native session titles remain separate from stable CLI names and identities."""
import tempfile
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
