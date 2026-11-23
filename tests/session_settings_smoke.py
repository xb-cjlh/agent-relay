"""Manual names and server-persisted favorites across desktop/mobile sessions."""
import json,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server
from test_relay import TOKEN
from playwright.sync_api import expect,sync_playwright


def main():
 with tempfile.TemporaryDirectory() as temporary:
  root=Path(temporary);server=Server(('127.0.0.1',0),root/'data',TOKEN);base='http://127.0.0.1:'+str(server.server_port);client=Client(base,TOKEN)
  try:
   devices=[]
   for name in ('work','home'):
    identity=client.request('POST','/api/register',{'name':name,'platform':'test','roots':[],'capabilities':{'codex':True,'claude':True,'unlimited_tasks':True}})
    device=Client(base,identity['token']);devices.append((identity,device))
    device.request('POST','/api/device/poll',{'sessions':[{'kind':'codex' if i%2 else 'claude','session_id':name+'-'+str(i),'name':name+'-stable-'+str(i),'title':'Agent 标题 '+str(i),'cwd':'/projects/'+name,'updated_at':1000+i,'busy':False} for i in range(36)]})
   with sync_playwright() as p:
    for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
     # Keep each browser's initial state independent.
     server.database.execute("DELETE FROM agents WHERE cwd='/projects/new-directory'")
     server.database.execute("UPDATE agents SET custom_title='',favorite=FALSE")
     chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
     browser=engine.launch(**({'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}))
     context=browser.new_context(viewport={'width':1400,'height':900} if name=='Chromium' else {'width':390,'height':844},**({'is_mobile':True,'has_touch':True} if name=='WebKit' else {}))
     page=context.new_page();errors=[];jobs=[]
     page.on('pageerror',lambda error:errors.append(str(error)))
     page.on('request',lambda request:jobs.append(request.url) if request.method=='POST' and request.url==base+'/api/jobs' else None)
     page.add_init_script('window.setInterval=()=>0')
     page.goto(base);server.browser_login(page);expect(page.locator('#workspace')).to_be_visible()
     expect(page.locator('[data-organization="directory"]')).to_have_attribute('aria-pressed','true')
     expect(page.locator('[data-organization="time"]')).to_have_count(0)
     agent=page.evaluate('state.agents.find(a=>a.id===selected)');id=agent['id'];path='/api/agents/'+id+'/settings'
     page.locator('#prompt').fill('保留此会话的草稿')
     page.locator('#rename-session').click();expect(page.locator('#session-title')).to_have_value(agent['title'])
     title='手动名称 <img src=x onerror=window.nameInjected=true>'
     page.locator('#session-title').fill(title);page.locator('#rename-form button[type=submit]').click()
     expect(page.locator('#rename-dialog')).not_to_be_visible();expect(page.locator('#conversation-title')).to_have_text(title)
     expect(page.locator('#prompt')).to_have_value('保留此会话的草稿')
     expect(page.locator('#session-items .session-name').filter(has_text=title)).to_have_count(1)
     assert page.locator('#conversation-title img,#session-items img').count()==0
     assert not page.evaluate('window.nameInjected || false')
     # A genuine native update changes the fallback title but cannot erase manual settings.
     device=next(d for ident,d in devices if ident['id']==agent['device_id'])
     device.request('POST','/api/device/poll',{'sessions':[{'kind':agent['kind'],'session_id':agent['session_id'],'name':agent['name'],'cwd':agent['cwd'],'title':'新的原生标题','updated_at':agent['last_activity'],'busy':False}]})
     page.locator('#favorite-session').click();expect(page.locator('#favorite-session')).to_have_attribute('aria-pressed','true')
     expect(page.locator('#conversation-title')).to_have_text(title)
     # Renaming failure keeps the dialog and old title; retry is possible.
     page.route('**'+path,lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"保存失败 fixture"}'))
     page.locator('#rename-session').click();page.locator('#session-title').fill('失败时保留原名');page.locator('#rename-form button[type=submit]').click()
     expect(page.locator('#toast')).to_contain_text('保存失败');expect(page.locator('#rename-dialog')).to_be_visible()
     expect(page.locator('#conversation-title')).to_have_text(title)
     page.unroute('**'+path);page.locator('[data-close="rename-dialog"]').click()
     if name=='WebKit':page.locator('#open-sidebar').click()
     page.locator('[data-organization="favorites"]').click()
     items=page.locator('#session-list [data-agent]');expect(items).to_have_count(1)
     expect(items.first).to_have_attribute('data-agent',id)
     expect(page.locator('#conversation-title')).to_have_text(title)
     expect(page.locator('#prompt')).to_have_value('保留此会话的草稿')
     page.locator('#search').fill('手动名称');expect(items).to_have_count(1)
     page.locator('#search').fill('not present fixture');expect(items).to_have_count(0)
     page.locator('#search').fill('');expect(items).to_have_count(1)
     # More than one page, mixed devices, and filtering all run on the server.
     all_agents=client.request('GET','/api/state')['agents']
     others=sorted((a for a in all_agents if a['id']!=id),key=lambda a:(-a['last_activity'],a['id']))[:40]
     for a in others:client.request('POST','/api/agents/'+a['id']+'/settings',{'favorite':True})
     page.evaluate('refresh()');expect(items).to_have_count(30)
     page.locator('#load-more-sessions').evaluate('(el)=>el.click()');expect(items).to_have_count(41)
     assert len(set(items.evaluate_all('(els)=>els.map(el=>el.dataset.agent)')))==41
     expected=sorted([agent]+others,key=lambda a:(-a['last_activity'],a['id']))
     assert items.evaluate_all('(els)=>els.map(el=>el.dataset.agent)')==[a['id'] for a in expected]
     home=devices[1][0]['id'];page.locator('#device-filter').select_option(home)
     count=sum(a['device_id']==home for a in [agent]+others)
     expect(items).to_have_count(min(30,count))
     assert all('home' in value for value in items.evaluate_all('(els)=>els.map(el=>el.title)'))
     page.locator('#device-filter').select_option('');expect(items).to_have_count(30)
     page.reload();expect(page.locator('#workspace')).to_be_visible()
     expect(page.locator('[data-organization="favorites"]')).to_have_attribute('aria-pressed','true')
     # A new browser has the same server-side names/favorites, without copying local storage.
     other=browser.new_context(viewport={'width':1400,'height':900});reader=other.new_page();reader.goto(base);server.browser_login(reader)
     reader.evaluate('setSessionOrganization("favorites")');expect(reader.locator('#session-items [data-agent]')).to_have_count(30)
     reader.locator('#search').fill('手动名称');expect(reader.locator('#session-items [data-agent]')).to_have_count(1)
     reader.locator('#session-items [data-agent]').click();expect(reader.locator('#conversation-title')).to_have_text(title)
     reader.locator('#rename-session').click();reader.locator('#session-title').fill('');reader.locator('#rename-form button[type=submit]').click()
     expect(reader.locator('#rename-dialog')).not_to_be_visible();expect(reader.locator('#conversation-title')).to_have_text('新的原生标题')
     reader.locator('#favorite-session').click();expect(reader.locator('#favorite-session')).to_have_attribute('aria-pressed','false')
     expect(reader.locator('#conversation-title')).to_have_text('新的原生标题')
     assert client.request('GET','/api/agents/'+id+'/messages')['agent']['name']==agent['name']
     # All controls still fit a narrow phone, even with a long manual title.
     reader.set_viewport_size({'width':320,'height':700})
     expect(reader.locator('#favorite-session')).to_be_in_viewport();expect(reader.locator('#share-conversation')).to_be_in_viewport()
     assert reader.evaluate('document.documentElement.scrollWidth<=innerWidth')
     # A notification opens its target directory, even from filtered Favorites.
     reader.set_viewport_size({'width':1400,'height':900})
     target=next(a for a in all_agents if a['device_id']!=agent['device_id'])
     notice=client.request('POST','/api/notifications',{'title':'目录跳转 fixture','body':'打开另一个目录中的会话','agent':target['id']})
     reader.locator('#notifications-button').click()
     reader.locator('[data-notification="'+notice['id']+'"]').click()
     expect(reader.locator('#conversation-title')).to_have_text(target['title'])
     expect(reader.locator('[data-organization="directory"]')).to_have_attribute('aria-pressed','true')
     assert reader.evaluate('activeDirectory')==json.dumps([target['device_id'],target['cwd']],ensure_ascii=False,separators=(',',':'))
     # Creating from Favorites opens the new conversation in its directory.
     reader.evaluate('setSessionOrganization("favorites")')
     reader.locator('#new-agent').click();reader.locator('#agent-device').select_option(devices[0][0]['id'])
     reader.locator('#agent-cwd').fill('/projects/new-directory');reader.locator('#agent-form button[type=submit]').click()
     expect(reader.locator('#agent-dialog')).not_to_be_visible()
     expect(reader.locator('[data-organization="directory"]')).to_have_attribute('aria-pressed','true')
     expect(reader.locator('#conversation-meta')).to_contain_text('/projects/new-directory')
     assert not errors,errors
     assert not jobs,jobs
     other.close();context.close();browser.close()
     print(name+': manual rename/reset, native title precedence, favorites pagination/search/device filters, cross-browser persistence, failures, drafts, injection and mobile layout passed',flush=True)
   assert not client.request('GET','/api/state')['jobs']
  finally:server.close()

if __name__=='__main__':main()
