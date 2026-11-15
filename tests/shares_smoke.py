"""Owner share UI and anonymous desktop/mobile readers; no model or cloud calls."""
import json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Server, Client
from auth_support import Mailbox
from oss_support import OssFixture, upload_image, claims, put
from test_accounts import PNG
from test_relay import TOKEN
from playwright.sync_api import sync_playwright, expect


def main():
 with tempfile.TemporaryDirectory() as directory:
  root=Path(directory);mail=Mailbox();oss=OssFixture();server=Server(('127.0.0.1',0),root/'data',TOKEN,environment={**mail.environment(),**oss.environment()})
  url='http://127.0.0.1:'+str(server.server_port)
  try:
   client=Client(url,TOKEN)
   device=client.request('POST','/api/register',{'name':'secret-device','platform':'test','roots':[],'capabilities':{'codex':True,'unlimited_tasks':True,'image_input':True,'file_input':True}})
   dev=Client(url,device['token'])
   history=[{'id':'history-'+str(i),'role':'user' if i%2==0 else 'assistant','content':'history '+str(i),'created_at':1000+i} for i in range(115)]
   session={'kind':'codex','session_id':'share-fixture','name':'A shared conversation','title':'A shared conversation','cwd':'/private/project','updated_at':1200,'messages':history}
   dev.request('POST','/api/device/poll',{'sessions':[session]})
   agent=client.request('GET','/api/state')['agents'][0]
   image=upload_image(client,PNG)
   data=b'%PDF-1.7 shared file\x00';ticket=client.request('POST','/api/attachments/uploads',claims(data,'shared.pdf','application/pdf'));put(ticket,data)
   file=client.request('POST','/api/attachments/uploads/'+ticket['uploadId']+'/complete',{})
   job=client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'See image and file','images':[image['id']],'files':[file['id']]})
   dev.request('POST','/api/device/poll',{'capacity':1})
   output='# Shared answer\n\n**Markdown works**\n\n[local file](private.txt)\n\n<script>window.pwned=true</script>\n\n```mermaid\nflowchart LR\n A-->B\n```\n'
   dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','output':output,'diagnostics':'private diagnostic'})
   with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    for engine,mobile in ((p.chromium,False),(p.webkit,True)):
     browser=engine.launch(**({'executable_path':str(chrome)} if not mobile and chrome.exists() else {}))
     owner=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));page=owner.new_page();errors=[]
     page.on('pageerror',lambda error:errors.append(str(error)))
     page.goto(url);server.browser_login(page);expect(page.locator('#workspace')).to_be_visible()
     if mobile:page.locator('#open-sidebar').click()
     page.locator('#session-list [data-agent]').first.click();page.locator('#share-conversation').click()
     expect(page.locator('#share-dialog')).to_be_visible();page.locator('#share-duration').select_option('3600')
     page.locator('#share-form button').click();expect(page.locator('#share-result')).to_be_visible()
     link=page.locator('#share-link').input_value();assert link.startswith(url+'/share#shr-relay_')
     reader=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));public=reader.new_page();requests=[]
     public.on('pageerror',lambda error:errors.append(str(error)))
     public.on('request',lambda r:requests.append(r))
     public.goto(link)
     expect(public.locator('#share-title')).to_have_text('A shared conversation')
     assert not public.locator('#login').count() and not public.locator('#prompt').count()
     expect(public.locator('#share-messages h1')).to_have_text('Shared answer')
     expect(public.locator('#share-messages strong')).to_have_text('Markdown works')
     public.locator('#share-messages img').scroll_into_view_if_needed()
     expect(public.locator('#share-messages img')).to_have_js_property('naturalWidth',1)
     expect(public.locator('iframe.mermaid-frame')).to_have_count(1,timeout=30000)
     assert public.evaluate('window.pwned') is None
     assert 'private diagnostic' not in public.locator('body').inner_text()
     assert '/private/project' not in public.locator('#share-meta').inner_text()
     public.locator('[data-unshared]').click();expect(public.locator('#share-status')).to_contain_text('未包含')
     with public.expect_download() as downloaded:public.locator('[data-file]').click()
     assert Path(downloaded.value.path()).read_bytes()==data
     public.locator('[data-view-image]').click();expect(public.locator('#share-image-dialog')).to_be_visible();expect(public.locator('#share-image-view img')).to_have_js_property('naturalWidth',1)
     public.locator('#share-image-close').click()
     public.locator('#share-older').click();expect(public.locator('#share-messages .message')).to_have_count(117)
     expect(public.locator('#share-older')).to_be_hidden()
     if mobile:
      session['messages']=history+[{'id':'future-'+str(mobile),'role':'user','content':'future secret','created_at':time.time()+1}]
      dev.request('POST','/api/device/poll',{'sessions':[session]})
     public.reload();expect(public.locator('#share-title')).to_have_text('A shared conversation');assert 'future secret' not in public.locator('#share-messages').inner_text()
     for request in requests:
      assert 'shr-relay_' not in request.url
      assert not '/api/auth/' in request.url
      if '/api/shared/' in request.url:assert 'cookie' not in request.headers and request.headers['authorization'].startswith('Bearer shr-relay_')
      if request.url.startswith(oss.url):assert 'authorization' not in request.headers and 'cookie' not in request.headers
     page.locator('#share-list [data-revoke-share]').first.click();expect(page.locator('#share-list')).to_contain_text('没有有效')
     public.reload();expect(public.locator('#share-status')).to_contain_text('失效');expect(public.locator('#share-messages')).to_be_empty()
     page.locator('[data-close=share-dialog]').click();assert page.locator('#share-link').input_value()==''
     expiring=client.request('POST','/api/agents/'+agent['id']+'/shares',{'expiresIn':60})
     def short_expiry(route):
      response=route.fetch();body=response.json();body['expires_at']=time.time()+3
      route.fulfill(response=response,json=body)
     public.route('**/api/shared/conversation',short_expiry)
     public.goto(url+expiring['path'],wait_until='domcontentloaded');expect(public.locator('#share-messages .message')).not_to_have_count(0)
     expect(public.locator('#share-status')).to_contain_text('失效',timeout=8000);expect(public.locator('#share-messages')).to_be_empty()
     public.unroute('**/api/shared/conversation',short_expiry)
     client.request('POST','/api/shares/'+expiring['id']+'/revoke',{})
     # A missing token opens the public error state directly, even with no login cookies.
     public.goto(url+'/share');expect(public.locator('#share-status')).to_contain_text('失效')
     assert not errors,errors
     reader.close();owner.close();browser.close()
   print('PASS: desktop and mobile share creation, anonymous Markdown/Mermaid/images/file downloads, pagination, immutable snapshot, revocation and token isolation')
  finally:server.close();mail.close();oss.close()


if __name__=='__main__':main()
