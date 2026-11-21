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
 start(){this.starts++;if(window.speechThrow)throw Error('unavailable');this.late.start=this.onstart;this.onstart?.();}
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
      mic.click();expect(mic).to_have_attribute('aria-pressed','true');expect(send).to_be_disabled()
      assert page.evaluate('speechInstances.at(-1).lang')=='zh-CN';assert page.evaluate('speechInstances.at(-1).continuous')
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',false]])");expect(page.locator('#voice-preview')).to_have_text('检查这个项目');expect(field).to_have_value('请帮我旧文字，谢谢')
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复',false]])");expect(field).to_have_value('请帮我检查这个项目，谢谢');expect(page.locator('#voice-preview')).to_have_text('并修复')
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复问题',true]])");expect(field).to_have_value('请帮我检查这个项目并修复问题，谢谢')
      # Cumulative duplicate events must not append final text a second time.
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复问题',true]])");expect(field).to_have_value('请帮我检查这个项目并修复问题，谢谢');assert not posts
      assert page.evaluate('id=>sessionStorage.getItem("relay-draft:"+id)',agents[0]['id'])==field.input_value()
      mic.click();expect(mic).to_be_disabled();expect(send).to_be_disabled()
      page.evaluate("speechInstances.at(-1).emit([['检查这个项目',true],['并修复问题',true],['。',true]])");page.evaluate('speechInstances.at(-1).end()')
      expect(field).to_have_value('请帮我检查这个项目并修复问题。，谢谢');expect(send).to_be_enabled();assert not posts
      # Editing stays available during dictation; English segments get a word boundary.
      field.fill('Hello');mic.click();page.evaluate("speechInstances.at(-1).emit([['world',true]])");expect(field).to_have_value('Hello world')
      field.fill('手动修改');page.evaluate("speechInstances.at(-1).emit([['world',true],['后继续说',true]])");expect(field).to_have_value('手动修改后继续说')
      page.evaluate('speechInstances.at(-1).end()');send.click();page.wait_for_function('()=>!sending');assert len(posts)==1;assert posts[0].post_data_json['prompt']=='手动修改后继续说'
      # Cancel on session switch and reject late results from the previous instance.
      field.fill('原会话草稿');mic.click();page.evaluate('speechInstances.at(-1).remember()');page.evaluate('id=>selectAgent(id)',agents[1]['id'])
      expect(field).to_have_value('');page.evaluate("speechInstances.at(-1).delayed('不能进入另一个会话')");expect(field).to_have_value('');assert page.evaluate('speechInstances.at(-1).aborts')==1
      page.evaluate('id=>selectAgent(id)',agents[0]['id']);expect(field).to_have_value('原会话草稿')
      # Permission denial, unavailable service and start failure remain recoverable.
      for code,notice in [('not-allowed','权限'),('network','连接失败'),('no-speech','没有识别到'),('audio-capture','麦克风')]:
       mic.click();page.evaluate('code=>speechInstances.at(-1).error(code)',code);expect(page.locator('#voice-message')).to_contain_text(notice);expect(mic).to_have_attribute('aria-pressed','false');expect(field).to_have_value('原会话草稿')
      page.evaluate('window.speechThrow=true');mic.click();expect(page.locator('#voice-message')).to_contain_text('无法启动');expect(mic).to_be_enabled();page.evaluate('window.speechThrow=false')
      # Leave the browser/tab: abort rather than resume the microphone in the background.
      mic.click();page.evaluate('speechInstances.at(-1).remember()');page.evaluate("()=>{Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));}")
      expect(mic).to_have_attribute('aria-pressed','false');page.evaluate("speechInstances.at(-1).delayed('late after hiding')");expect(field).to_have_value('原会话草稿')
      page.evaluate("()=>Object.defineProperty(document,'hidden',{value:false,configurable:true})")
      mic.click();page.evaluate('speechInstances.at(-1).remember()');page.evaluate("window.dispatchEvent(new Event('pagehide'))");expect(mic).to_have_attribute('aria-pressed','false');assert page.evaluate('speechInstances.at(-1).aborts')==1
      page.evaluate("()=>{document.getElementById('prompt').disabled=true;RelayVoice.render();}");expect(mic).to_be_disabled();page.evaluate('renderHeader()');expect(mic).to_be_enabled()
      # Enforce textarea maxlength even though programmatic insertions bypass browser typing checks.
      field.fill('x'*31999);mic.click();page.evaluate("speechInstances.at(-1).emit([['🙂',true]])");page.evaluate('speechInstances.at(-1).end()');assert len(field.input_value())==31999
      field.fill('x'*31998);mic.click();page.evaluate("speechInstances.at(-1).emit([['甲乙丙',true]])");page.evaluate('speechInstances.at(-1).end()');assert len(field.input_value())==32000
      # Logout invalidates callbacks before asynchronous sign-out and drops account drafts.
      field.fill('私有草稿');mic.click();page.evaluate('speechInstances.at(-1).remember()');page.evaluate('logout()');expect(page.locator('#login')).to_be_visible()
      page.evaluate("speechInstances.at(-1).delayed('旧账号内容')");assert page.evaluate('speechInstances.at(-1).aborts')==1
      assert page.evaluate('id=>sessionStorage.getItem("relay-draft:"+id)',agents[0]['id']) is None
      assert not errors,errors
      if mobile:
       server.browser_login(page);expect(page.locator('#workspace')).to_be_visible();page.evaluate('id=>selectAgent(id)',agents[1]['id']);field.fill('手机语音')
       field.focus();page.wait_for_timeout(200)
       assert float(field.evaluate('el=>getComputedStyle(el).fontSize').removesuffix('px'))>=16
       box=send.bounding_box();assert box and box['x']>=0 and box['x']+box['width']<=page.viewport_size['width'];assert box['y']+box['height']<=page.viewport_size['height']
      print(name+': dictation drafts, cursor, cumulative results, stop, errors, account/session isolation and send passed',flush=True)
     finally:context.close()
     # An unsupported browser still offers an honest keyboard dictation hint.
     context=browser.new_context();page=context.new_page();page.add_init_script("Object.defineProperty(window,'SpeechRecognition',{value:undefined});Object.defineProperty(window,'webkitSpeechRecognition',{value:undefined});")
     page.goto(url);server.browser_login(page);expect(page.locator('#workspace')).to_be_visible();page.locator('#voice-input').click();expect(page.locator('#voice-message')).to_contain_text('手机键盘');context.close();browser.close()
  finally:server.close()
if __name__=='__main__':main()
