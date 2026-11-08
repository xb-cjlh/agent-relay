"""Real Java/MySQL + Go agents, local SMTP and model-free image fixtures."""
import base64,json, os, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Server,Client,binary
from auth_support import Mailbox
from test_accounts import PNG
from test_relay import TOKEN,FAKE_AGENT,PROJECT
from test_sessions import codex_records,write_records
from oss_support import OssFixture
from playwright.sync_api import sync_playwright,expect

def main():
 with tempfile.TemporaryDirectory() as temporary:
  root=Path(temporary);mail=Mailbox();oss=OssFixture();server=Server(('127.0.0.1',0),root/'data',TOKEN,environment={**mail.environment(),**oss.environment()})
  url='http://127.0.0.1:'+str(server.server_port);daemon=None
  try:
   cwd=root/'project';cwd.mkdir();bindir=root/'bin';bindir.mkdir()
   for kind in ('codex','claude'):
    path=bindir/kind;path.write_text('#!'+sys.executable+'\n'+FAKE_AGENT);path.chmod(0o755)
   codex=root/'codex';claude=root/'claude'
   write_records(codex/'sessions/fixture.jsonl',codex_records(cwd))
   write_records(claude/'projects/fixture/session.jsonl',[{'type':'user','sessionId':'claude-images','cwd':str(cwd),'uuid':'user-fixture','message':{'content':'image conversation'}},{'type':'assistant','sessionId':'claude-images','cwd':str(cwd),'uuid':'assistant-fixture','message':{'content':'ready','stop_reason':'end_turn'}}])
   with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    browser=p.chromium.launch(**({'executable_path':str(chrome)} if chrome.exists() else {}));context=browser.new_context();page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    diagnostics=[]
    page.on('console',lambda m:diagnostics.append(m.text) if m.type=='error' else None)
    page.on('requestfailed',lambda r:diagnostics.append(r.method+' '+r.url.split('?')[0]+' '+str(r.failure)))
    direct=[];api_posts=[];previews=[]
    page.on('request',lambda r:direct.append(r) if r.method=='PUT' else api_posts.append(r) if r.method=='POST' and '/api/images' in r.url else None)
    page.on('response',lambda r:previews.append(r) if r.request.method=='GET' and r.url.startswith(url+'/api/images/') else None)
    page.goto(url);expect(page.locator('#auth-title')).to_have_text('登录 Relay')
    page.locator('[data-auth-mode=register]').click()
    page.locator('#auth-email').fill('browser@example.com');page.locator('#auth-password').fill('Relay-password-2026');page.locator('#auth-name').fill('Browser test')
    page.locator('#send-code').click();expect(page.locator('#auth-notice')).to_contain_text('验证码')
    page.locator('#auth-code').fill(mail.code('browser@example.com'));page.locator('#auth-submit').click();expect(page.locator('#workspace')).to_be_visible()
    page.locator('#account-button').click();page.locator('#key-name').fill('image fixture');page.locator('#key-form button[type=submit]').click()
    expect(page.locator('#new-key-secret')).to_contain_text('sk-relay_');secret=page.locator('#new-key-secret').inner_text()
    assert not page.locator('#key-permission').count()
    page.locator('[data-close=account-dialog]').click();expect(page.locator('#new-key-secret')).to_be_empty()
    env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ.get('PATH',''),CODEX_HOME=str(codex),CLAUDE_CONFIG_DIR=str(claude),AM_CONFIG_DIR=str(root/'config'))
    daemon=subprocess.Popen([binary(),'--server',url,'--token',secret,'daemon','run','--name','image-device','--state-dir',str(root/'device'),'--interval','0.1'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    client=Client(url,secret);end=time.monotonic()+15
    while len(client.request('GET','/api/state')['agents'])!=2:
     assert time.monotonic()<end,'image sessions not discovered';time.sleep(.1)
    agents=client.request('GET','/api/state')['agents']
    expect(page.locator('#session-list [data-agent]')).to_have_count(2)
    # Recover when PUT succeeds but its response is lost, then retry one transient confirmation failure.
    page.evaluate('''()=>{const original=window.fetch;window.fetch=async(...args)=>{
      const response=await original(...args);
      if(args[1]?.method==='PUT'){window.fetch=original;throw new TypeError('fixture lost PUT response');}
      return response;
    };}''')
    confirmation_attempts=[0]
    def confirm(route):
     confirmation_attempts[0]+=1
     if confirmation_attempts[0]==1:route.fulfill(status=503,content_type='application/json',body='{"error":"fixture temporary failure"}')
     else:route.continue_()
    page.route('**/api/images/uploads/*/complete',confirm)
    for kind in ('codex','claude'):
     agent=next(a for a in agents if a['kind']==kind);page.locator('[data-agent="'+agent['id']+'"]').click()
     page.locator('#image-input').set_input_files({'name':'fixture.png','mimeType':'image/png','buffer':PNG})
     expect(page.locator('#attachment-list img')).to_have_count(1)
     page.locator('#attachment-list button').click();expect(page.locator('#attachment-list')).not_to_be_visible()
     page.locator('#image-input').set_input_files({'name':'fixture.png','mimeType':'image/png','buffer':PNG})
     expect(page.locator('#attachment-list img')).to_have_count(1)
     page.locator('#prompt').fill('describe '+kind);page.locator('#send-button').click()
     expect(page.locator('#messages')).to_contain_text('IMAGES:1 describe '+kind,timeout=15000)
     expect(page.locator('#messages .message-images img')).to_have_count(1)
     expect(page.locator('#messages .message-images img')).to_have_js_property('naturalWidth',1)
     # The full-size viewer also loads directly from OSS, without navigating to a download.
     stable=page.locator('#messages .message-images a').get_attribute('href')
     assert stable.startswith('/api/images/')
     page.locator('#messages .message-images a').click()
     expect(page.locator('#image-dialog')).to_be_visible()
     expect(page.locator('#image-view img')).to_have_js_property('naturalWidth',1)
     assert page.locator('#image-view img').get_attribute('src').startswith(oss.url+'/')
     page.locator('[data-close=image-dialog]').click();expect(page.locator('#image-dialog')).not_to_be_visible()
     previous=len(previews)
     page.reload();expect(page.locator('#messages .message-images img')).to_have_js_property('naturalWidth',1)
     assert len(previews)>previous
    assert len(direct)==4
    for request in direct:
     assert request.url.startswith(oss.url+'/')
     assert 'cookie' not in request.headers and 'x-relay-request' not in request.headers and 'authorization' not in request.headers
    for request in api_posts:
     assert '/api/images/uploads' in request.url
     assert 'data' not in request.post_data_json
    assert previews
    for response in previews:
     assert response.status==200 and response.url.endswith('/url')
     assert 'no-store' in response.headers['cache-control']
     assert response.headers['content-type'].startswith('application/json')
    image_gets=[r for r in oss.requests if r['method']=='GET' and r['headers'].get('Sec-Fetch-Dest')=='image']
    assert image_gets
    for request in image_gets:
     headers={k.lower():v for k,v in request['headers'].items()}
     assert headers.get('origin')==url,headers
     assert not any(h in headers for h in ('cookie','authorization','x-relay-request','referer')),headers
    # Even a small-byte, very high resolution PNG must be resized before PUT.
    highres=page.evaluate('''async()=>{
      const canvas=document.createElement('canvas');canvas.width=5000;canvas.height=3000;
      const c=canvas.getContext('2d');c.fillStyle='white';c.fillRect(0,0,5000,3000);c.fillStyle='black';c.font='80px sans-serif';
      for(let y=100;y<3000;y+=150)c.fillText('Relay screenshot text 1234567890',40,y);
      return canvas.toDataURL('image/png').split(',')[1];
    }''')
    assert len(base64.b64decode(highres))<5*1024*1024
    expired=[0]
    def expire_once(route):
     if route.request.method=='GET' and expired[0]==0:
      expired[0]+=1
      route.fulfill(status=403,headers={'Access-Control-Allow-Origin':url},content_type='application/xml',body='<Error><Code>AccessDenied</Code></Error>')
     else:route.continue_()
    page.route(oss.url+'/**',expire_once)
    page.locator('#image-input').set_input_files({'name':'high-resolution.png','mimeType':'image/png','buffer':base64.b64decode(highres)})
    expect(page.locator('#attachment-list img')).to_have_count(1,timeout=15000)
    expect(page.locator('#attachment-list img')).to_have_js_property('naturalWidth',2048)
    assert expired[0]==1 # A failed/expired GET is re-signed once and then displays successfully.
    page.unroute(oss.url+'/**',expire_once)
    image_id=page.locator('#attachment-list img').get_attribute('data-image-id')
    compressed=next(value['data'] for key,value in oss.objects.items() if key.endswith('/'+image_id))
    assert len(compressed)<=1024*1024
    page.locator('#attachment-list button').click()
    noise=page.evaluate('''async()=>{
      const canvas=document.createElement('canvas');canvas.width=2400;canvas.height=1600;
      const c=canvas.getContext('2d'),pixels=c.createImageData(canvas.width,canvas.height);let seed=12345;
      for(let i=0;i<pixels.data.length;i+=4){seed=(1664525*seed+1013904223)>>>0;pixels.data[i]=seed&255;pixels.data[i+1]=(seed>>>8)&255;pixels.data[i+2]=(seed>>>16)&255;pixels.data[i+3]=255;}
      c.putImageData(pixels,0,0);const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      const prepared=await RelayImages.prepare(new File([blob],'noise.png',{type:'image/png'}));
      return {original:blob.size,size:prepared.size,width:prepared.width,height:prepared.height,mime:prepared.mime};
    }''')
    assert noise['original']>1024*1024 and noise['size']<=1024*1024 and noise['width']<=2048 and noise['height']<=2048 and noise['mime']=='image/jpeg',noise
    assert confirmation_attempts[0]>=6
    # Browser session login persists; other browser contexts have no access.
    anonymous=browser.new_context();other=anonymous.new_page();other.goto(url);expect(other.locator('#login')).to_be_visible();expect(other.locator('#workspace')).not_to_be_visible();anonymous.close()
    # Simulate keyboard viewport contraction, retaining zoom and the send button.
    for height in (844,430,340,844):
     page.set_viewport_size({'width':390,'height':height});page.locator('#prompt').focus();page.locator('#prompt').fill('mobile')
     expect(page.locator('#send-button')).to_be_in_viewport()
     assert page.locator('#prompt').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)>=16')
     assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    # Export only fixture cookies to test WebKit's mobile layout if installed.
    state=context.storage_state()
    browser.close()
    try:webkit=p.webkit.launch()
    except Exception:webkit=None
    if webkit:
     mobile=webkit.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,storage_state=state)
     safari=mobile.new_page();safari.goto(url);expect(safari.locator('#workspace')).to_be_visible()
     safari.locator('#image-input').set_input_files({'name':'safari.png','mimeType':'image/png','buffer':PNG})
     expect(safari.locator('#attachment-list img')).to_have_count(1,timeout=10000)
     expect(safari.locator('#attachment-list img')).to_have_js_property('naturalWidth',1)
     safari.locator('#attachment-list button').click()
     for height in (844,430,340,844):
      safari.set_viewport_size({'width':390,'height':height});safari.locator('#prompt').focus();safari.locator('#prompt').fill('Safari keyboard')
      expect(safari.locator('#send-button')).to_be_in_viewport()
      assert safari.locator('#prompt').evaluate('(el)=>getComputedStyle(el).fontSize')=='16px'
     webkit.close()
    assert not errors,errors
    print('Accounts/images browser passed: registration, SK, direct OSS PNG preview/history/full-size, fresh authenticated signatures, failed GET re-signing, no Relay credentials to OSS, compression, Codex/Claude image execution, account isolation, keyboard viewport. WebKit:',bool(webkit))
  finally:
   if daemon:
    daemon.terminate()
    try:daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:daemon.kill();daemon.wait()
    daemon.stderr.close()
   server.close();mail.close();oss.close()

if __name__=='__main__':main()
