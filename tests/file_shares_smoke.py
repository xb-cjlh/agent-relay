"""File sharing from owner UI to anonymous desktop/mobile OSS readers."""
import base64, hashlib, sys, tempfile, time
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
   device=client.request('POST','/api/register',{'name':'private-device','platform':'test','roots':[],'capabilities':{'codex':True,'file_input':True,'image_input':True,'unlimited_tasks':True}})
   dev=Client(url,device['token'])
   content=b'# Shared document\n\n**Preview works**\n\n[private sibling](secret.txt)\n\n<script>window.pwned=true</script>\n\n```mermaid\nflowchart LR\n A-->B\n```\n'
   job=client.request('POST','/api/jobs',{'kind':'file_read','device':device['id'],'path':'/private/note.md'})
   dev.request('POST','/api/device/poll',{'capacity':1})
   artifact=dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'name':'note.md','data':base64.b64encode(content).decode(),'sha256':hashlib.sha256(content).hexdigest()}})['artifact_id']
   dev.request('POST','/api/device/poll',{'sessions':[{'kind':'codex','session_id':'file-share-fixture','name':'Private conversation','cwd':'/private','updated_at':time.time(),'messages':[{'id':'link','role':'assistant','content':'[文件](note.md)','created_at':time.time()}]}]})
   agent=client.request('GET','/api/state')['agents'][0]
   image=upload_image(client,PNG)
   data=b'%PDF-binary\x00';ticket=client.request('POST','/api/attachments/uploads',claims(data,'shared.pdf','application/pdf'));put(ticket,data)
   file=client.request('POST','/api/attachments/uploads/'+ticket['uploadId']+'/complete',{})
   job=client.request('POST','/api/jobs',{'kind':'prompt','agent':agent['id'],'prompt':'private question','files':[file['id']],'images':[image['id']]})
   dev.request('POST','/api/device/poll',{'capacity':1})
   dev.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','output':'private answer'})
   server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?',(device['id'],))
   with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    for engine,mobile in ((p.chromium,False),(p.webkit,True)):
     browser=engine.launch(**({'executable_path':str(chrome)} if not mobile and chrome.exists() else {}))
     owner=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));page=owner.new_page();errors=[]
     page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);server.browser_login(page)
     page.evaluate('id=>selectAgent(id)',agent['id'])
     page.locator('#messages [data-file-link]').click()
     expect(page.locator('#file-preview-content h1')).to_have_text('Shared document')
     page.locator('#share-file-preview').click();expect(page.locator('#share-title')).to_have_text('分享文件')
     page.locator('#share-duration').select_option('3600');page.locator('#share-form button').click()
     expect(page.locator('#share-result')).to_be_visible();link=page.locator('#share-link').input_value()
     reader=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));public=reader.new_page();requests=[]
     public.on('pageerror',lambda e:errors.append(str(e)));public.on('request',lambda r:requests.append(r));public.goto(link)
     expect(public.locator('#share-title')).to_have_text('note.md');expect(public.locator('#shared-file-content strong')).to_have_text('Preview works')
     expect(public.locator('iframe.mermaid-frame')).to_have_count(1,timeout=30000)
     assert public.evaluate('window.pwned') is None
     assert 'private answer' not in public.locator('body').inner_text()
     public.locator('[data-unshared]').click();expect(public.locator('#share-status')).to_contain_text('未包含')
     with public.expect_download() as downloaded:public.locator('#shared-file-download').click()
     assert Path(downloaded.value.path()).read_bytes()==content
     page.locator('#share-list [data-revoke-share]').first.click();expect(page.locator('#share-list')).to_contain_text('没有有效')
     public.locator('#shared-file-download').click();expect(public.locator('#share-status')).to_contain_text('失效')
     expect(public.locator('#shared-file-content')).to_be_empty();expect(public.locator('#shared-file')).to_be_hidden()
     page.locator('[data-close=share-dialog]').click();page.locator('#close-file-preview').click()
     # File list uses the same dialog and can manage independent shares.
     page.evaluate("$('files-dialog').showModal()")
     page.locator('#file-list [data-share-file="'+artifact+'"]').click()
     page.locator('#share-form button').click();expect(page.locator('#share-result')).to_be_visible()
     page.locator('#share-list [data-revoke-share]').first.click();page.locator('[data-close=share-dialog]').click();page.locator('[data-close=files-dialog]').click()
     # Sent attachment cards also create standalone file shares.
     page.locator('#messages [data-share-file="'+file['id']+'"]').click();page.locator('#share-form button').click()
     expect(page.locator('#share-result')).to_be_visible();public.goto(page.locator('#share-link').input_value())
     expect(public.locator('#shared-file-content')).to_contain_text('此格式暂不支持预览')
     with public.expect_download() as downloaded:public.locator('#shared-file-download').click()
     assert Path(downloaded.value.path()).read_bytes()==data
     page.locator('#share-list [data-revoke-share]').first.click();page.locator('[data-close=share-dialog]').click()
     page.locator('#messages [data-view-image]').click();page.locator('#share-image').click();page.locator('#share-form button').click()
     expect(page.locator('#share-result')).to_be_visible();public.goto(page.locator('#share-link').input_value())
     expect(public.locator('#shared-file-content > img')).to_have_js_property('naturalWidth',1)
     page.locator('#share-list [data-revoke-share]').first.click();page.locator('[data-close=share-dialog]').click();page.locator('[data-close=image-dialog]').click()
     # Existing open file pages erase content at expiry without a refresh.
     expiring=client.request('POST','/api/file-shares',{'file':artifact,'expiresIn':60})
     def short_expiry(route):
      response=route.fetch();body=response.json();body['expires_at']=time.time()+3;route.fulfill(response=response,json=body)
     public.route('**/api/shared/content',short_expiry);public.goto(url+expiring['path'])
     expect(public.locator('#shared-file-content h1')).to_have_text('Shared document')
     expect(public.locator('#share-status')).to_contain_text('失效',timeout=8000);expect(public.locator('#shared-file-content')).to_be_empty()
     client.request('POST','/api/shares/'+expiring['id']+'/revoke',{})
     for request in requests:
      assert 'shr-relay_' not in request.url
      assert '/api/auth/' not in request.url
      if '/api/shared/' in request.url:assert 'cookie' not in request.headers and request.headers['authorization'].startswith('Bearer shr-relay_')
      if request.url.startswith(oss.url):assert 'authorization' not in request.headers and 'cookie' not in request.headers
     assert not errors,errors
     assert len(client.request('GET','/api/state')['jobs'])==2,'sharing queued a device job'
     reader.close();owner.close();browser.close()
   print('PASS: file shares from preview/list/attachments/images; anonymous desktop/mobile Markdown/Mermaid/image/binary previews and direct OSS downloads, expiry, revocation and credential isolation')
  finally:server.close();mail.close();oss.close()


if __name__=='__main__':main()
