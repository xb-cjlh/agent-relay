"""Persist stream observation times until native message timestamps arrive."""
import unittest
from scripts.devtools import Client
import test_relay


class MessageOrderCase(unittest.TestCase):
    setUp = test_relay.ServerCase.setUp
    tearDown = test_relay.ServerCase.tearDown
    register = test_relay.ServerCase.register
    create_agent = test_relay.ServerCase.create_agent
    prompt = test_relay.ServerCase.prompt

    def test_pending_reply_after_followup_keeps_time_across_growth_and_completion(self):
        _, device = self.register()
        agent = self.create_agent()
        original = self.prompt(agent, 'original question')
        claimed = device.request('POST', '/api/device/poll', {'capacity':1})['jobs'][0]
        session = {'kind':'codex','session_id':'ordered-stream','name':'stream','cwd':str(self.root),'busy':True,'updated_at':claimed['started_at'],'messages':[]}
        def progress(output, native=None):
            body = {'active':[{'id':original['id'],'session_id':session['session_id'],'output':output}]}
            if native is not None:
                session['messages'] = native
                body['sessions'] = [session]
            device.request('POST','/api/device/poll',body)
        def message():
            return next(m for m in self.client.request('GET','/api/agents/'+agent['id']+'/messages')['messages'] if m['id']==original['id']+':assistant')
        progress('first reply\n',[])
        first = message()['created_at']
        followup = self.prompt(agent,'additional input')
        progress('first reply\n')
        self.assertEqual(message()['created_at'],first)
        native = [{'id':'native-first','role':'assistant','content':'first reply','created_at':first}]
        progress('first reply\n\nnext repl',native)
        pending = message()['segments'][-1]['created_at']
        self.assertGreater(pending,followup['created_at'])
        progress('first reply\n\nnext reply complete')
        self.assertEqual(message()['segments'][-1]['created_at'],pending)
        device.request('POST','/api/device/jobs/'+original['id']+'/complete',{'status':'succeeded','output':'first reply\n\nnext reply complete'})
        self.assertEqual(message()['segments'][-1]['created_at'],pending)
        self.assertEqual(message()['segments'][0]['created_at'],first)

    def test_first_output_is_after_new_input_even_without_native_history(self):
        _, device = self.register()
        agent = self.create_agent()
        job = self.prompt(agent)
        device.request('POST','/api/device/poll',{'capacity':1})
        followup = self.prompt(agent,'followup before any reply')
        device.request('POST','/api/device/poll',{'active':[{'id':job['id'],'output':'first live reply'}]})
        messages = self.client.request('GET','/api/agents/'+agent['id']+'/messages')['messages']
        reply = next(m for m in messages if m['id']==job['id']+':assistant')
        self.assertGreater(reply['created_at'],followup['created_at'])
        self.assertEqual([m['content'] for m in messages if m['content']],['hello','followup before any reply','first live reply'])
        foreign = self.client.request('POST','/api/register',{'name':'other-device','platform':'test','roots':[],'capabilities':{}})
        Client(self.url,foreign['token']).request('POST','/api/device/poll',{'active':[{'id':job['id'],'output':'unowned update'}]})
        self.assertEqual(self.client.request('GET','/api/jobs/'+job['id'])['output'],'first live reply')
