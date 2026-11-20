"""Notification inbox, real worker delivery, deep links and iPhone install guidance."""
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server
from test_relay import TOKEN
from push_support import PushFixture
from playwright.sync_api import expect,sync_playwright

def main():
 with tempfile.TemporaryDirectory() as tmp:
  root=Path(tmp);push=PushFixture(root);server=Server(('127.0.0.1',0),root/'data',TOKEN,environment=push.environment());base='http://127.0.0.1:'+str(server.server_port);client=Client(base,TOKEN)
  n=client.request('POST','/api/notifications',{'title':'<img src=x onerror=alert(1)>','body':'First line\nSecond line'})
  try:
   with sync_playwright() as p:
    for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
     chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');options={'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}
     browser=engine.launch(**options);context=browser.new_context();page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
     try:
      page.goto(base+'/?notification='+n['id']);expect(page.locator('#login')).to_be_visible();server.browser_login(page)
      expect(page.locator('#workspace')).to_be_visible();expect(page.locator('#notifications-dialog')).to_be_visible();expect(page.locator('[data-notification="'+n['id']+'"] strong')).to_have_text(n['title']);assert page.locator('#notification-list img').count()==0
      expect(page.locator('[data-notification="'+n['id']+'"]')).not_to_have_class('notification-entry unread')
      if name=='Chromium':
       context.grant_permissions(['notifications']);page.locator('[data-close="notifications-dialog"]').click();page.locator('#notifications-button').click();expect(page.locator('#enable-push')).to_be_enabled()
       # UI uses the real worker, with a loopback subscription replacing the external browser provider.
       page.evaluate('''async sub=>{const r=await navigator.serviceWorker.ready;let active=false;const s={endpoint:sub.endpoint,options:{applicationServerKey:new Uint8Array(%s).buffer},toJSON:()=>sub,unsubscribe:async()=>{active=false;return true;}};r.pushManager.getSubscription=async()=>active?s:null;r.pushManager.subscribe=async()=>{active=true;return s;};}'''%json.dumps(list(__import__('push_support').un64(push.public))),push.subscription())
       page.locator('#enable-push').click();expect(page.locator('#disable-push')).to_be_visible();expect(page.locator('#push-status')).to_contain_text('已开启')
       cdp=context.new_cdp_session(page);versions=[];cdp.on('ServiceWorker.workerRegistrationUpdated',lambda e:versions.extend(e['registrations']));cdp.send('ServiceWorker.enable');page.wait_for_function('()=>!!navigator.serviceWorker.controller');page.wait_for_timeout(300)
       registration=next(v for v in versions if v['scopeURL']==base+'/')
       cdp.send('ServiceWorker.deliverPushMessage',{'origin':base,'registrationId':registration['registrationId'],'data':json.dumps({'id':n['id'],'body':'untrusted push content'})})
       shown=None;deadline=time.monotonic()+8
       while not shown and time.monotonic()<deadline:
        shown=page.evaluate('async()=>{const items=await(await navigator.serviceWorker.ready).getNotifications();if(!items.length)return null;const n=items[0];return {title:n.title,body:n.body,id:n.data.id};}')
        if not shown:page.wait_for_timeout(100)
       page.evaluate('async()=> (await(await navigator.serviceWorker.ready).getNotifications()).forEach(n=>n.close())')
       assert shown=={'title':'Relay','body':'你有一条新的 Agent 通知','id':n['id']},repr(shown)
       page.locator('#disable-push').click();expect(page.locator('#enable-push')).to_be_visible();assert not errors,errors
      device=client.request('POST','/api/register',{'name':'notification-'+name,'platform':'fixture','capabilities':{'codex':True,'unlimited_tasks':True}})
      agent=client.request('POST','/api/agents',{'name':'Linked '+name,'device':device['id'],'kind':'codex','cwd':tmp})
      linked=client.request('POST','/api/notifications',{'title':'Linked task','body':'Open the right session','agent':agent['id']})
      page.locator('[data-close="notifications-dialog"]').click();page.locator('#notifications-button').click();page.locator('[data-notification="'+linked['id']+'"]').click();expect(page.locator('#conversation-title')).to_have_text(agent['name']);expect(page.locator('#notifications-dialog')).not_to_be_visible()
      page.locator('#logout').click();expect(page.locator('#login')).to_be_visible();assert not errors,errors
      print(name+': notification inbox, safe rendering and logout passed')
     finally:context.close();browser.close()
    browser=p.webkit.launch();context=browser.new_context(**p.devices['iPhone 13']);page=context.new_page();page.goto(base);server.browser_login(page);expect(page.locator('#workspace')).to_be_visible();page.locator('#open-sidebar').click();page.locator('#notifications-button').click();expect(page.locator('#push-status')).to_contain_text('添加到主屏幕');expect(page.locator('#enable-push')).to_be_disabled();context.close();browser.close();print('iPhone: installation guidance passed')
  finally:server.close();push.close()
if __name__=='__main__':main()
