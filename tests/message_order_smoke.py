"""In-flight instructions interleave with native/streaming replies, including reload."""
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server
from test_relay import TOKEN
from playwright.sync_api import expect,sync_playwright


def main():
 with tempfile.TemporaryDirectory() as temp:
  root=Path(temp);server=Server(('127.0.0.1',0),root/'data',TOKEN);client=Client('http://127.0.0.1:'+str(server.server_port),TOKEN)
  try:
   identity=client.request('POST','/api/register',{'name':'order','platform':'test','roots':[], 'capabilities':{'codex':True,'claude':True,'unlimited_tasks':True,'codex_steering':True}})
   device=Client(client.server,identity['token'])
   with sync_playwright() as playwright:
    for name,engine in [('Chromium',playwright.chromium),('WebKit',playwright.webkit)]:
     chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
     browser=engine.launch(**({'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}))
     context=browser.new_context(viewport={'width':1400,'height':900} if name=='Chromium' else {'width':390,'height':844},**({'is_mobile':True,'has_touch':True} if name=='WebKit' else {}));page=context.new_page();errors=[]
     page.on('pageerror',lambda error:errors.append(str(error)))
     page.goto(client.server);server.browser_login(page)
     for kind in ['codex','claude']:
      agent=client.request('POST','/api/agents',{'device':identity['id'],'kind':kind,'name':name+'-'+kind,'cwd':str(root)})
      first=client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'先合并 dev'})
      started=next(job['started_at'] for job in device.request('POST','/api/device/poll',{'capacity':4})['jobs'] if job['id']==first['id'])
      session={'kind':kind,'session_id':name+'-'+kind,'name':kind,'cwd':str(root),'busy':True,'updated_at':time.time(),'messages':[]}
      device.request('POST','/api/device/poll',{'active':[{'id':first['id'],'session_id':session['session_id'],'output':''}],'sessions':[session]})
      followup=client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'并部署 dev'})
      # The agent's first answer starts after the follow-up, before native sync.
      device.request('POST','/api/device/poll',{'active':[{'id':first['id'],'output':'会合并并部署。'}]})
      page.evaluate('refresh()');page.evaluate('id=>selectAgent(id)',agent['id'])
      def order(expected):
       page.evaluate('loadConversation()')
       expect(page.locator('#messages > .message').filter(has_text='会合并并部署。')).to_have_count(1,timeout=10000)
       actual=page.locator('#messages .message-body').all_text_contents()
       assert [text.strip() for text in actual]==expected,(name,kind,actual)
      order(['先合并 dev','并部署 dev','会合并并部署。'])
      # A genuine earlier native update stays before the follow-up. Later
      # answers from that SAME running task appear after it, in their own rows.
      stamp=followup['created_at']
      session['messages']=[{'id':name+kind+'-before','role':'assistant','content':'已检查当前分支。','created_at':(started+stamp)/2}, {'id':name+kind+'-after','role':'assistant','content':'会合并并部署。','created_at':stamp+.001}]
      output='已检查当前分支。\n会合并并部署。\n'
      device.request('POST','/api/device/poll',{'active':[{'id':first['id'],'output':output}],'sessions':[session]})
      order(['先合并 dev','已检查当前分支。','并部署 dev','会合并并部署。'])
      more=client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'再检查线上版本'})
      device.request('POST','/api/device/poll',{'active':[{'id':first['id'],'output':output+'正在检查线'}]})
      order(['先合并 dev','已检查当前分支。','并部署 dev','会合并并部署。','再检查线上版本','正在检查线'])
      device.request('POST','/api/device/poll',{'active':[{'id':first['id'],'output':output+'正在检查线上版本。'}]})
      order(['先合并 dev','已检查当前分支。','并部署 dev','会合并并部署。','再检查线上版本','正在检查线上版本。'])
      # Execution status and logs belong to the final visual row only.
      expect(page.locator('#messages .message-state')).to_have_count(3) # two queued users, one active reply
      expect(page.locator('#messages > .assistant').first.locator('.message-state')).to_have_count(0)
      page.reload();order(['先合并 dev','已检查当前分支。','并部署 dev','会合并并部署。','再检查线上版本','正在检查线上版本。'])
      # Match native echoes to the single canonical Relay user message.
      session['messages']=[{'id':name+kind+'-tail','role':'assistant','content':'正在检查线上版本。','created_at':more['created_at']+.001}]
      if kind=='codex':
       claimed=device.request('POST','/api/device/poll',{'capacity':0,'steer_capacity':1,'active':[{'id':first['id'],'session_id':session['session_id'],'output':output+'正在检查线上版本。','steerable':True}]})['jobs']
       assert any(job['id']==followup['id'] for job in claimed)
       device.request('POST','/api/device/jobs/'+followup['id']+'/complete',{'status':'succeeded','steered':True,'output':''})
       session['messages'].append({'id':name+kind+'-echo','role':'user','content':'并部署 dev','created_at':stamp})
      device.request('POST','/api/device/poll',{'sessions':[session]})
      device.request('POST','/api/device/jobs/'+first['id']+'/complete',{'status':'succeeded','output':output+'正在检查线上版本。'})
      order(['先合并 dev','已检查当前分支。','并部署 dev','会合并并部署。','再检查线上版本','正在检查线上版本。'])
      for job in [followup,more]:client.request('POST','/api/jobs/'+job['id']+'/cancel',{})
     assert not errors,errors
     print(name+': Codex/Claude in-flight instruction order, delayed native sync, partial replies, status placement, reload and duplicate suppression passed',flush=True)
     context.close();browser.close()
  finally:server.close()


if __name__=='__main__':main()
