"""Real HTML rendering and authenticated/public standalone files, with sandbox probes."""
import base64, hashlib, sys, tempfile, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, Server
from auth_support import Mailbox
from oss_support import OssFixture, claims, put
from test_relay import TOKEN
from playwright.sync_api import sync_playwright, expect

HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Standalone report</title>
<style>h1 {color:rgb(17, 34, 51)} body {margin:20px} button {font-size:18px}</style></head>
<body><h1>HTML 报告</h1><button id="counter" onclick="this.textContent=++count">0</button>
<a id="escape" href="/?html-escape=1" target="_top">escape</a>
<script>
let count=0;
document.querySelector('#escape').onclick=event=>{event.preventDefault();try{top.location.href='/?html-escape=1';}catch{document.body.dataset.topBlocked='yes';}};
try { parent.document.body.dataset.injected='yes'; } catch { document.body.dataset.parentBlocked='yes'; }
try { document.cookie='html-injected=yes'; document.body.dataset.cookieBlocked='no'; } catch { document.body.dataset.cookieBlocked='yes'; }
try { localStorage.setItem('html-injected','yes'); document.body.dataset.storageBlocked='no'; } catch { document.body.dataset.storageBlocked='yes'; }
fetch('/api/state?html-probe=1').then(()=>document.body.dataset.fetchBlocked='no').catch(()=>document.body.dataset.fetchBlocked='yes');
</script>
<!-- PADDING -->
<p id="tail">完整 HTML 尾部</p></body></html>'''.replace('<!-- PADDING -->','<!--'+'x'*280000+'-->')


def main():
 with tempfile.TemporaryDirectory() as directory:
  root=Path(directory);mail=Mailbox();oss=OssFixture();server=Server(('127.0.0.1',0),root/'data',TOKEN,environment={**mail.environment(),**oss.environment()})
  base='http://127.0.0.1:'+str(server.server_port);client=Client(base,TOKEN)
  try:
   device=client.request('POST','/api/register',{'name':'html-fixture','platform':'test','roots':[],'capabilities':{'codex':True,'file_input':True,'unlimited_tasks':True}})
   dev=Client(base,device['token']);data=HTML.encode()
   job=client.request('POST','/api/jobs',{'kind':'file_read','device':device['id'],'path':'/private/report.html'})
   dev.request('POST','/api/device/poll',{'capacity':1})
   artifact=dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'name':'report.html','data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}})['artifact_id']
   dev.request('POST','/api/device/poll',{'sessions':[{'kind':'codex','session_id':'html-preview-fixture','name':'HTML preview','cwd':'/private','updated_at':time.time(),'messages':[{'id':'link','role':'assistant','content':'[报告](report.html)','created_at':time.time()}]}]})
   agent=client.request('GET','/api/state')['agents'][0]
   ticket=client.request('POST','/api/attachments/uploads',claims(data,'uploaded.HTM','text/html'));put(ticket,data)
   attachment=client.request('POST','/api/attachments/uploads/'+ticket['uploadId']+'/complete',{})
   job=client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'uploaded html','files':[attachment['id']]})
   dev.request('POST','/api/device/poll',{'capacity':1});dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','output':'done'})
   server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?',(device['id'],))
   with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    for engine,mobile in ((p.chromium,False),(p.webkit,True)):
     print(('WebKit mobile' if mobile else 'Chromium desktop')+': HTML and standalone tabs',flush=True)
     browser=engine.launch(**({'executable_path':str(chrome)} if not mobile and chrome.exists() else {}))
     owner=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));page=owner.new_page();errors=[];requests=[]
     # WebKit reports the intentional, caught top-navigation probe as a page error.
     def page_error(error):
      if 'The frame attempting navigation of the top-level window is sandboxed' not in str(error):errors.append(str(error))
     owner.on('page',lambda tab:tab.on('pageerror',page_error));page.on('pageerror',page_error)
     owner.on('request',lambda r:requests.append(r))
     page.goto(base);server.browser_login(page);page.evaluate('id=>selectAgent(id)',agent['id'])

     def check_html(tab):
      tab.bring_to_front()
      frame=tab.frame_locator('iframe.html-file-frame');expect(frame.locator('h1')).to_have_text('HTML 报告')
      expect(frame.locator('h1')).to_have_css('color','rgb(17, 34, 51)')
      expect(frame.locator('#tail')).to_have_text('完整 HTML 尾部')
      for name in ('parent','cookie','storage','fetch'):expect(frame.locator('body')).to_have_attribute('data-'+name+'-blocked','yes')
      frame.locator('#counter').click();expect(frame.locator('#counter')).to_have_text('1')
      assert tab.locator('body').get_attribute('data-injected') is None
      assert tab.evaluate('localStorage.getItem("html-injected")') is None
      assert not any(c['name']=='html-injected' for c in owner.cookies())
      old=tab.url;frame.locator('#escape').click();assert tab.url==old
      expect(frame.locator('body')).to_have_attribute('data-top-blocked','yes')
      assert tab.locator('iframe.html-file-frame').get_attribute('sandbox')=='allow-scripts'

     page.locator('#messages [data-file-link]').click();check_html(page)
     with page.expect_popup() as opened:page.locator('#open-file-preview').click()
     standalone=opened.value;standalone.wait_for_load_state('domcontentloaded');check_html(standalone)
     assert standalone.url==base+'/file#'+artifact
     assert standalone.evaluate('window.opener===null')
     expect(standalone.locator('#share-access')).to_have_text('文件预览')
     with standalone.expect_download() as downloaded:standalone.locator('#shared-file-download').click()
     assert Path(downloaded.value.path()).read_bytes()==data
     standalone.reload();check_html(standalone)
     # A standalone private view must not create an anonymous share.
     assert not client.request('GET','/api/file-shares?file='+artifact)['shares']
     anonymous=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));reader=anonymous.new_page()
     reader.goto(standalone.url);expect(reader.locator('#share-status')).to_contain_text('请先登录')
     expect(reader.locator('iframe.html-file-frame')).to_have_count(0)
     page.bring_to_front();page.locator('#share-file-preview').click();page.locator('#share-duration').select_option('3600');page.locator('#share-form button').click()
     expect(page.locator('#share-result')).to_be_visible();reader.goto(page.locator('#share-link').input_value());check_html(reader)
     with reader.expect_popup() as opened:reader.locator('#shared-file-open').click()
     shared=opened.value;shared.wait_for_load_state('domcontentloaded');check_html(shared)
     assert '/file#shr-relay_' in shared.url
     with shared.expect_download() as downloaded:shared.locator('#shared-file-download').click()
     assert Path(downloaded.value.path()).read_bytes()==data
     page.bring_to_front();page.locator('#share-list [data-revoke-share]').first.click();expect(page.locator('#share-list')).to_contain_text('没有有效')
     shared.bring_to_front()
     shared.locator('#shared-file-download').click();expect(shared.locator('#share-status')).to_contain_text('失效');expect(shared.locator('iframe.html-file-frame')).to_have_count(0)
     page.bring_to_front();page.locator('[data-close=share-dialog]').click();page.locator('#close-file-preview').click()
     with page.expect_popup() as opened:page.locator('#messages .file-card-actions a').click()
     uploaded=opened.value;uploaded.wait_for_load_state('domcontentloaded');check_html(uploaded)
     expect(uploaded.locator('#share-title')).to_have_text('uploaded.HTM')
     expiring=client.request('POST','/api/file-shares',{'file':artifact,'expiresIn':60})
     def short_expiry(route):
      response=route.fetch();body=response.json();body['expires_at']=time.time()+4;route.fulfill(response=response,json=body)
     reader.route('**/api/shared/content',short_expiry);reader.bring_to_front();reader.goto(base+'/file#'+expiring['token'])
     expect(reader.frame_locator('iframe.html-file-frame').locator('h1')).to_have_text('HTML 报告')
     expect(reader.locator('#share-status')).to_contain_text('失效',timeout=8000);expect(reader.locator('iframe.html-file-frame')).to_have_count(0)
     client.request('POST','/api/shares/'+expiring['id']+'/revoke',{})
     # Signing out invalidates new signed URLs and clears the standalone file.
     page.evaluate('logout()');standalone.reload();expect(standalone.locator('#share-status')).to_contain_text('请先登录');expect(standalone.locator('iframe.html-file-frame')).to_have_count(0)
     assert not any('html-probe' in r.url for r in requests),'HTML escaped the network sandbox'
     for r in requests:
      if r.url.startswith(oss.url):assert 'cookie' not in r.headers and 'authorization' not in r.headers
     assert not errors,errors
     assert len(client.request('GET','/api/state')['jobs'])==2,'preview queued an offline read'
     anonymous.close();owner.close();browser.close()
   print('PASS: desktop and mobile HTML styles/scripts, sandbox isolation, full untruncated documents, private and shared standalone tabs, uploaded HTML, exact OSS downloads, refresh, logout and revocation')
  finally:server.close();mail.close();oss.close()


if __name__=='__main__':main()
