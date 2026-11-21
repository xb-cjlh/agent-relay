"""Dictation lifecycle with simulated browser speech events; no external speech/model calls."""
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server
from test_relay import TOKEN
from playwright.sync_api import expect,sync_playwright

SPEECH=r'''
window.speechInstances=[];
class FakeRecognition {
 constructor(){this.starts=0;this.stops=0;this.aborts=0;this.late={};speechInstances.push(this);}
 start(){this.starts++;if(window.speechThrow)throw Error('unavailable');this.late.start=this.onstart;if(!window.speechDelay)this.onstart?.();}
 stop(){this.stops++;}
 abort(){this.aborts++;}
 emit(results){this.onresult?.({resultIndex:0,results:results.map(([text,final])=>Object.assign([{transcript:text}],{isFinal:final}))});}
 error(code){this.onerror?.({error:code});}
 end(){this.onend?.();}
 remember(){this.late.result=this.onresult;this.late.end=this.onend;this.late.error=this.onerror;}
 delayed(text){this.late.result?.({resultIndex:0,results:[Object.assign([{transcript:text}],{isFinal:true})]});this.late.end?.();this.late.error?.({error:'network'});}
}
Object.defineProperty(window,'SpeechRecognition',{value:PREFIX?undefined:FakeRecognition,configurable:true});
Object.defineProperty(window,'webkitSpeechRecognition',{value:PREFIX?FakeRecognition:undefined,configurable:true});
'''

def press(page,mic,mobile):
 if mobile:
  mic.dispatch_event('pointerdown',{'pointerId':13,'pointerType':'touch','isPrimary':True,'button':0,'buttons':1})
 else:
  mic.hover();page.mouse.down()

def release(page,mobile,outside=False):
 if mobile:
  page.locator('body').dispatch_event('pointerup',{'pointerId':13,'pointerType':'touch','isPrimary':True,'button':0,'buttons':0})
 else:
  if outside:page.mouse.move(1,1)
  page.mouse.up()

def mobile_layout(page,field,mic,send):
 # Cover compact phones, a resized keyboard viewport, and the running-task controls.
 for width,height in [(320,568),(360,640),(390,844),(430,932),(390,420)]:
  page.set_viewport_size({'width':width,'height':height})
  page.wait_for_function('()=>Math.abs(document.getElementById("workspace").getBoundingClientRect().height-visualViewport.height)<1')
  page.evaluate('RelayVoice.cancel()');field.fill('')
  empty_height=field.bounding_box()['height'];assert page.locator('#send-form').bounding_box()['height']<=112
  field.fill('第一行\n第二行\n第三行\n第四行');assert field.bounding_box()['height']>empty_height
  field.fill('');assert abs(field.bounding_box()['height']-empty_height)<1
  for busy in (False,True):
   page.evaluate("busy=>{activeJobs=busy?[{id:'layout-fixture',status:'running'}]:[];renderHeader();}",busy)
   field.fill('手机语音');before=mic.bounding_box()
   for control in [mic,send,page.locator('#attach-image'),page.locator('#permission-mode')]+([page.locator('#stop-generation')] if busy else []):
    box=control.bounding_box();assert box and box['x']>=0 and box['x']+box['width']<=width and box['y']+box['height']<=height,(width,height,busy,box)
   assert before['width']>=44 and before['height']>=44 and before['x']>width/2
   send_box=send.bounding_box();assert before['x']+before['width']<=send_box['x'] and abs(before['y']-send_box['y'])<1
   press(page,mic,True)
   page.evaluate("speechInstances.at(-1).emit([['这是一段较长的识别内容，用来确认提示和文字出现在输入框上方，按住时不会推动按钮或遮挡发送操作。',false]])")
   status=page.locator('#voice-status').bounding_box();after=mic.bounding_box()
   assert status and status['y']>=0 and status['y']+status['height']<=field.bounding_box()['y']
   assert abs(before['x']-after['x'])<1 and abs(before['y']-after['y'])<1,(width,height,busy,before,after)
   release(page,True);page.evaluate('speechInstances.at(-1).end()')
   expect(mic).to_have_attribute('aria-pressed','false')
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')

def main():
 with tempfile.TemporaryDirectory() as temporary:
  server=Server(('127.0.0.1',0),Path(temporary)/'data',TOKEN);url='http://127.0.0.1:'+str(server.server_port);client=Client(url,TOKEN)
  try:
   device=client.request('POST','/api/register',{'name':'voice-fixture','platform':'fixture','capabilities':{'codex':True,'claude':True,'unlimited_tasks':True,'writable':True,'codex_permissions':True,'claude_permissions':True,'image_input':True}})
   agents=[client.request('POST','/api/agents',{'device':device['id'],'kind':kind,'name':kind+' voice','cwd':temporary}) for kind in ('codex','claude')]
   with sync_playwright() as p:
    for name,engine,mobile in [('Chromium',p.chromium,False),('WebKit iPhone',p.webkit,True)]:
     chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');options={'executable_path':str(chrome)} if not mobile and chrome.exists() else {}
     browser=engine.launch(**options);context=browser.new_context(**(p.devices['iPhone 13'] if mobile else {}));page=context.new_page();errors=[];posts=[]
     page.on('pageerror',lambda e:errors.append(str(e)))
     page.on('request',lambda r:posts.append(r) if r.method=='POST' and r.url==url+'/api/jobs' else None)
     page.add_init_script(SPEECH.replace('PREFIX','true' if mobile else 'false'))
     try:
      page.goto(url);expect(page.locator('#login')).to_be_visible();assert page.evaluate('speechInstances.length')==0
      server.browser_login(page);expect(page.locator('#workspace')).to_be_visible()
      page.evaluate('id=>selectAgent(id)',agents[0]['id']);expect(page.locator('#thread')).to_be_visible()
      field=page.locator('#prompt');mic=page.locator('#voice-input');send=page.locator('#send-button');expect(mic).to_be_enabled()
      # Preserve the draft around a selection and preview interim text without committing it.
      field.fill('请帮我旧文字，谢谢');page.evaluate("()=>{const p=document.getElementById('prompt');p.setSelectionRange(3,6);}")
      press(page,mic,mobile);expect(mic).to_have_attribute('aria-pressed','true');expect(send).to_be_disabled()
      assert page.evaluate('speechInstances.at(-1).lang')=='zh-CN';assert page.evaluate('speechInstances.at(-1).continuous')
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',false]])");expect(page.locator('#voice-preview')).to_have_text('检查这个项目');expect(field).to_have_value('请帮我旧文字，谢谢')
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复',false]])");expect(field).to_have_value('请帮我检查这个项目，谢谢');expect(page.locator('#voice-preview')).to_have_text('并修复')
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复问题',true]])");expect(field).to_have_value('请帮我检查这个项目并修复问题，谢谢')
      # Cumulative duplicate events must not append final text a second time.
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复问题',true]])");expect(field).to_have_value('请帮我检查这个项目并修复问题，谢谢');assert not posts
      assert page.evaluate('id=>sessionStorage.getItem("relay-draft:"+id)',agents[0]['id'])==field.input_value()
      release(page,mobile,outside=True);expect(mic).to_have_attribute('aria-pressed','false');expect(mic).to_be_disabled();expect(send).to_be_disabled()
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复问题',true],['。',true]])");page.evaluate('speechInstances.at(-1).end()')
      expect(field).to_have_value('请帮我检查这个项目并修复问题。，谢谢');expect(send).to_be_enabled();assert not posts
      # Editing stays available during dictation; English segments get a word boundary.
      field.fill('Hello');press(page,mic,mobile);page.evaluate("speechInstances.at(-1).emit([['world',true]])");expect(field).to_have_value('Hello world')
      field.fill('手动修改');page.evaluate("speechInstances.at(-1).emit([['world',true],['后继续说',true]])");expect(field).to_have_value('手动修改后继续说')
      release(page,mobile);page.evaluate('speechInstances.at(-1).end()');send.click();page.wait_for_function('()=>!sending');assert len(posts)==1;assert posts[0].post_data_json['prompt']=='手动修改后继续说'
      # Cancel on session switch and reject late results from the previous instance.
      field.fill('原会话草稿');press(page,mic,mobile);page.evaluate('speechInstances.at(-1).remember()');page.evaluate('id=>selectAgent(id)',agents[1]['id'])
      expect(field).to_have_value('');page.evaluate("speechInstances.at(-1).delayed('不能进入另一个会话')");expect(field).to_have_value('');assert page.evaluate('speechInstances.at(-1).aborts')==1;release(page,mobile)
      page.evaluate('id=>selectAgent(id)',agents[0]['id']);expect(field).to_have_value('原会话草稿')
      # Permission denial, unavailable service and start failure remain recoverable.
      for code,notice in [('not-allowed','权限'),('network','连接失败'),('no-speech','没有识别到'),('audio-capture','麦克风')]:
       press(page,mic,mobile);page.evaluate('code=>speechInstances.at(-1).error(code)',code);expect(page.locator('#voice-message')).to_contain_text(notice);expect(mic).to_have_attribute('aria-pressed','false');expect(field).to_have_value('原会话草稿');release(page,mobile)
      page.evaluate('window.speechThrow=true');press(page,mic,mobile);expect(page.locator('#voice-message')).to_contain_text('无法启动');expect(mic).to_be_enabled();page.evaluate('window.speechThrow=false');release(page,mobile)
      # Leave the browser/tab: abort rather than resume the microphone in the background.
      press(page,mic,mobile);page.evaluate('speechInstances.at(-1).remember()');page.evaluate("()=>{Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));}")
      expect(mic).to_have_attribute('aria-pressed','false');page.evaluate("speechInstances.at(-1).delayed('late after hiding')");expect(field).to_have_value('原会话草稿')
      page.evaluate("()=>Object.defineProperty(document,'hidden',{value:false,configurable:true})");release(page,mobile)
      press(page,mic,mobile);page.evaluate('speechInstances.at(-1).remember()');page.evaluate("window.dispatchEvent(new Event('pagehide'))");expect(mic).to_have_attribute('aria-pressed','false');assert page.evaluate('speechInstances.at(-1).aborts')==1;release(page,mobile)
      page.evaluate("()=>{document.getElementById('prompt').disabled=true;RelayVoice.render();}");expect(mic).to_be_disabled();page.evaluate('renderHeader()');expect(mic).to_be_enabled()
      # Enforce textarea maxlength even though programmatic insertions bypass browser typing checks.
      field.fill('x'*31999);press(page,mic,mobile);page.evaluate("speechInstances.at(-1).emit([['🙂',true]])");release(page,mobile);page.evaluate('speechInstances.at(-1).end()');assert len(field.input_value())==31999
      field.fill('x'*31998);press(page,mic,mobile);page.evaluate("speechInstances.at(-1).emit([['甲乙丙',true]])");release(page,mobile);page.evaluate('speechInstances.at(-1).end()');assert len(field.input_value())==32000
      # A quick click cannot leave dictation running; there is no stop icon.
      field.fill('');idle_icon=mic.inner_html();mic.click();assert page.evaluate('speechInstances.at(-1).stops')==1
      expect(page.locator('#voice-message')).to_contain_text('正在结束');assert mic.inner_html()==idle_icon
      page.evaluate('speechInstances.at(-1).end()');expect(mic).to_be_enabled()
      # Fast release while waiting for permission aborts the request and rejects a late start/result.
      page.evaluate('window.speechDelay=true');press(page,mic,mobile);page.evaluate('speechInstances.at(-1).remember()')
      release(page,mobile);assert page.evaluate('speechInstances.at(-1).aborts')==1;assert not page.evaluate('RelayVoice.active()')
      page.evaluate("speechInstances.at(-1).late.start();speechInstances.at(-1).delayed('松手后不应录音')");expect(field).to_have_value('')
      page.evaluate('window.speechDelay=false')
      # Real Chromium touch events keep capture after dragging away, then stop on release.
      if not mobile:
       cdp=context.new_cdp_session(page);box=mic.bounding_box();point={'x':box['x']+box['width']/2,'y':box['y']+box['height']/2}
       cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[point]});expect(mic).to_have_attribute('aria-pressed','true')
       cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':1,'y':1}]})
       assert page.evaluate('speechInstances.at(-1).stops')==0
       cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});assert page.evaluate('speechInstances.at(-1).stops')==1
       page.evaluate('speechInstances.at(-1).end()');cdp.detach()
      # Pointer cancellation/capture loss and a blurred window stop the held microphone.
      for event in ('pointercancel','lostpointercapture'):
       mic.dispatch_event('pointerdown',{'pointerId':71,'pointerType':'touch','isPrimary':True,'button':0,'buttons':1})
       mic.dispatch_event(event,{'pointerId':71,'pointerType':'touch','isPrimary':True})
       assert page.evaluate('speechInstances.at(-1).stops')==1;page.evaluate('speechInstances.at(-1).end()')
      press(page,mic,mobile);page.evaluate("window.dispatchEvent(new Event('blur'))");release(page,mobile)
      assert page.evaluate('speechInstances.at(-1).stops')==1;page.evaluate('speechInstances.at(-1).end()')
      # Keyboard hold/release supports Space and Enter without repeats or accidental submission.
      for key in ('Space','Enter'):
       mic.focus();before=page.evaluate('speechInstances.length');page.keyboard.down(key);page.keyboard.down(key)
       assert page.evaluate('speechInstances.length')==before+1;expect(page.locator('#voice-message')).to_contain_text('松手结束')
       page.keyboard.up(key);assert page.evaluate('speechInstances.at(-1).stops')==1;page.evaluate('speechInstances.at(-1).end()')
      mic.focus();page.keyboard.down('Space');field.focus();page.keyboard.up('Space')
      assert page.evaluate('speechInstances.at(-1).stops')==1;page.evaluate('speechInstances.at(-1).end()');assert len(posts)==1
      # An assistive click gives instructions and never starts a persistent microphone.
      before=page.evaluate('speechInstances.length');mic.evaluate('el=>el.click()');assert page.evaluate('speechInstances.length')==before
      expect(page.locator('#voice-message')).to_contain_text('按住麦克风');assert mic.evaluate('el=>getComputedStyle(el).touchAction')=='none'
      # Ignore secondary pointers and non-primary mouse buttons.
      mic.dispatch_event('pointerdown',{'pointerId':72,'isPrimary':False,'button':0})
      mic.dispatch_event('pointerdown',{'pointerId':73,'isPrimary':True,'button':2})
      assert page.evaluate('speechInstances.length')==before
      # Logout invalidates callbacks before asynchronous sign-out and drops account drafts.
      field.fill('私有草稿');press(page,mic,mobile);page.evaluate('speechInstances.at(-1).remember()');page.evaluate('logout()');expect(page.locator('#login')).to_be_visible()
      page.evaluate("speechInstances.at(-1).delayed('旧账号内容')");assert page.evaluate('speechInstances.at(-1).aborts')==1;release(page,mobile)
      assert page.evaluate('id=>sessionStorage.getItem("relay-draft:"+id)',agents[0]['id']) is None
      assert not errors,errors
      if mobile:
       server.browser_login(page);expect(page.locator('#workspace')).to_be_visible();page.evaluate('id=>selectAgent(id)',agents[1]['id']);field.fill('手机语音')
       field.focus();page.wait_for_timeout(200)
       assert float(field.evaluate('el=>getComputedStyle(el).fontSize').removesuffix('px'))>=16
       box=send.bounding_box();assert box and box['x']>=0 and box['x']+box['width']<=page.viewport_size['width'];assert box['y']+box['height']<=page.viewport_size['height']
       mobile_layout(page,field,mic,send)
      print(name+': dictation drafts, cursor, cumulative results, hold/release, permission delay, keyboard, errors, account/session isolation and send passed',flush=True)
     finally:context.close()
     # An unsupported browser still offers an honest keyboard dictation hint.
     context=browser.new_context();page=context.new_page();page.add_init_script("Object.defineProperty(window,'SpeechRecognition',{value:undefined});Object.defineProperty(window,'webkitSpeechRecognition',{value:undefined});")
     page.goto(url);server.browser_login(page);expect(page.locator('#workspace')).to_be_visible();page.locator('#voice-input').click();expect(page.locator('#voice-message')).to_contain_text('手机键盘');context.close();browser.close()
  finally:server.close()
if __name__=='__main__':main()
