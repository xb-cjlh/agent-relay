"""Mermaid in native messages and downloaded Markdown under production CSP."""
import base64
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server
from test_relay import TOKEN
from playwright.sync_api import expect,sync_playwright

FLOW='''flowchart LR
  UI[看板页面与 ASK AI] --> WS[Text2InsightsHandler]
  WS --> D[DashboardService / ChartService]
  WS --> C[逐图表拼接上下文 Markdown]
  C --> I[AgentIntegrationService]
  I --> R[ReActAgentExecutor]
  R --> M[AgentMemoryService / Compressor]
  R --> T[SaveChart / SaveDashboard 等工具]
  T --> D
  R --> E[AgentStep 与工具字符串结果]
  E --> V[AgentResponseConverter]
  V --> F[dashboard_refresh 消息]
  F --> UI
'''
# Tall enough to reproduce the clipped 800px initial view in full file previews.
SEQUENCE='sequenceDiagram\n  Alice->>Bob: 你好\n' + '  Bob-->>Alice: 收到\n' * 24
DOCUMENT='# 架构图\n\n```mermaid\n'+FLOW+'```\n\n```mermaid\n'+SEQUENCE+'```\n\n```mermaid\nflowchart LR\n A[broken\n```\n\n```python\nprint("ordinary code")\n```\n'

def fitted_geometry(frame):
 return frame.locator('#diagram-viewport').evaluate('''el=>{
  const v=el.getBoundingClientRect(),s=el.querySelector('svg').getBoundingClientRect();
  return {fits:s.left>=v.left-1&&s.top>=v.top-1&&s.right<=v.right+1&&s.bottom<=v.bottom+1,
   width:s.width,height:s.height,viewportWidth:v.width,viewportHeight:v.height,
   overflow:document.documentElement.scrollWidth>innerWidth,scale:diagramView.scale};
 }''')

def exercise_diagram_view(page, block, mobile=False):
 block.scroll_into_view_if_needed()
 frame=block.frame_locator('iframe')
 view=frame.locator('#diagram-viewport');drawing=frame.locator('#diagram')
 expect(block).to_have_attribute('data-diagram-rendered','1',timeout=30000)
 # Let the frame receive its fitted height and complete its ResizeObserver.
 frame.locator('body').evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
 geometry=fitted_geometry(frame);assert geometry['fits'] and not geometry['overflow'],geometry
 assert geometry['scale']<=1,geometry
 assert block.locator('iframe').bounding_box()['height']<=min(600,page.viewport_size['height']*.65)+1
 initial=geometry['scale']
 frame.locator('#zoom-in').click();assert fitted_geometry(frame)['scale']>initial
 frame.locator('#zoom-out').click();assert abs(fitted_geometry(frame)['scale']-initial)<.00001
 frame.locator('#zoom-actual').click();assert fitted_geometry(frame)['scale']==1
 frame.locator('#zoom-fit').click();assert fitted_geometry(frame)['fits']
 view.focus();view.press('+');assert fitted_geometry(frame)['scale']>initial
 view.press('0');assert fitted_geometry(frame)['fits']
 # Mobile WebKit has no automated wheel events; desktop uses trusted wheel input.
 view.hover();before=page.evaluate('scrollY');scroll=page.locator('#message-scroll').evaluate('(el)=>el.scrollTop')
 anchor=view.bounding_box();cx=anchor['x']+anchor['width']/2;cy=anchor['y']+anchor['height']/2
 page.mouse.move(cx,cy)
 if mobile:view.dispatch_event('wheel',{'deltaY':-50,'clientX':anchor['width']/2,'clientY':anchor['height']/2})
 else:page.mouse.wheel(0,-50)
 frame.locator('body').evaluate('()=>new Promise(requestAnimationFrame)')
 assert fitted_geometry(frame)['scale']>initial
 assert page.evaluate('scrollY')==before
 assert abs(page.locator('#message-scroll').evaluate('(el)=>el.scrollTop')-scroll)<1
 transform=drawing.evaluate('(el)=>el.style.transform')
 page.mouse.down();page.mouse.move(cx+35,cy+25,steps=4);page.mouse.up()
 assert drawing.evaluate('(el)=>el.style.transform')!=transform
 # WebKit has no multi-touch automation API. Simulate only the two-finger
 # events, leaving the trusted mouse path above to verify real pointer capture.
 touch= view.evaluate('''el=>{
  const original=el.setPointerCapture;el.setPointerCapture=()=>{};
  const fire=(type,id,x,y)=>el.dispatchEvent(new PointerEvent(type,{pointerId:id,pointerType:'touch',clientX:x,clientY:y,bubbles:true}));
  try {
   const b=el.getBoundingClientRect(),x=b.left+b.width/2,y=b.top+b.height/2;
   const start=diagramView.scale;
   fire('pointerdown',101,x-30,y);fire('pointerdown',102,x+30,y);
   fire('pointermove',101,x-60,y);fire('pointermove',102,x+60,y);
   const end=diagramView.scale;
   fire('pointercancel',101,x-60,y);fire('pointerup',102,x+60,y);
   return {start,end,clean:!el.classList.contains('dragging')};
  } finally {el.setPointerCapture=original;}
 }''')
 assert touch['end']>touch['start'] and touch['clean'],touch
 # Safari gesture events may omit coordinates: zoom around the canvas center.
 gesture=view.evaluate('''el=>{
  const start=diagramView.scale;
  el.dispatchEvent(new Event('gesturestart',{cancelable:true}));
  const event=new Event('gesturechange',{cancelable:true});event.scale=1.2;el.dispatchEvent(event);
  const end=diagramView.scale,valid=Number.isFinite(diagramView.x)&&Number.isFinite(diagramView.y);
  el.dispatchEvent(new Event('gestureend',{cancelable:true}));return {start,end,valid};
 }''')
 assert gesture['end']>gesture['start'] and gesture['valid'],gesture
 frame.locator('#zoom-fit').click();assert fitted_geometry(frame)['fits']
 if mobile:assert frame.locator('#zoom-in').bounding_box()['width']>=44
 # Resizing (including the file preview's full-screen mode) refits an untouched view.
 original=page.viewport_size
 page.set_viewport_size({'width':max(320,original['width']-60),'height':original['height']})
 frame.locator('body').evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
 assert fitted_geometry(frame)['fits']
 page.set_viewport_size(original)
 frame.locator('body').evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
 assert fitted_geometry(frame)['fits']

def main():
 with tempfile.TemporaryDirectory() as td:
  root=Path(td);server=Server(('127.0.0.1',0),root/'data',TOKEN);base='http://127.0.0.1:'+str(server.server_port);client=Client(base,TOKEN)
  try:
   identity=client.request('POST','/api/register',{'name':'diagrams','platform':'test','roots':[], 'capabilities':{'codex':True,'claude':True,'unlimited_tasks':True}})
   device=Client(base,identity['token']);agents={}
   def sync(kind,content,mid='diagram'):
    device.request('POST','/api/device/poll',{'sessions':[{'kind':kind,'session_id':'diagram-'+kind,'name':kind,'cwd':str(root),'busy':False,'updated_at':time.time(),'messages':[{'id':kind+'-'+mid,'role':'assistant','content':content,'created_at':time.time()}]}]})
   for kind in ('codex','claude'):
    sync(kind,DOCUMENT+'\n[预览文件](diagram.md)\n')
    agents[kind]=next(a for a in client.request('GET','/api/state')['agents'] if a['kind']==kind)
   with sync_playwright() as p:
    for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
     if os.environ.get('RELAY_TEST_BROWSER') and os.environ['RELAY_TEST_BROWSER']!=name:continue
     chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');browser=engine.launch(**({'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}))
     ctx=browser.new_context(viewport={'width':1400,'height':1000} if name=='Chromium' else {'width':390,'height':650},**({'is_mobile':True,'has_touch':True} if name=='WebKit' else {}));page=ctx.new_page();errors=[];external=[]
     page.on('pageerror',lambda e:errors.append(str(e)))
     page.on('console',lambda m:errors.append(m.text) if m.type=='error' and not m.text.startswith('Viewport argument key "interactive-widget"') else None)
     ctx.route('https://untrusted.example.test/**',lambda r:(external.append(r.request.url),r.abort()))
     page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiedCode=text}}})")
     page.goto(base);server.browser_login(page)
     for kind,agent in agents.items():
      page.evaluate('id=>selectAgent(id)',agent['id'])
      print(name+': testing '+kind,flush=True)
      mid=hashlib.sha256((identity['id']+':'+kind+'-diagram').encode()).hexdigest()
      body=page.locator('#messages [data-id="'+mid+'"]')
      blocks=body.locator('[data-mermaid]');expect(blocks).to_have_count(3)
      blocks.nth(0).scroll_into_view_if_needed()
      frame=blocks.nth(0).frame_locator('iframe')
      try:expect(frame.locator('svg')).to_be_visible(timeout=30000)
      except Exception:
       print('diagram diagnostics',name,errors,blocks.nth(0).inner_text()[:250],flush=True)
       raise
      expect(frame.locator('svg')).to_contain_text('看板页面与 ASK AI')
      expect(blocks.nth(0)).to_have_attribute('data-diagram-rendered','1')
      assert frame.locator('body').evaluate('()=>{try{return !parent.document}catch{return true}}'),'diagram can access parent page'
      assert frame.locator('body').evaluate('()=>{try{localStorage.getItem("x");return false}catch{return true}}'),'diagram can access site storage'
      blocks.nth(1).scroll_into_view_if_needed()
      expect(blocks.nth(1).frame_locator('iframe').locator('svg')).to_contain_text('你好',timeout=30000)
      blocks.nth(2).scroll_into_view_if_needed()
      expect(blocks.nth(2).locator('details')).to_have_attribute('open','',timeout=30000)
      expect(blocks.nth(2).locator('pre')).to_contain_text('A[broken')
      blocks.nth(0).locator('[data-copy-code]').click()
      assert page.evaluate('window.copiedCode')==FLOW
      expect(body.locator('.code-block pre').last).to_have_text('print("ordinary code")\n')
      assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
      exercise_diagram_view(page,blocks.nth(0),name=='WebKit')
      exercise_diagram_view(page,blocks.nth(1),name=='WebKit')
      before=blocks.nth(0).locator('iframe').get_attribute('height')
      page.evaluate("postMessage({type:'relay-mermaid-result',ok:true,height:1},'*')")
      assert blocks.nth(0).locator('iframe').get_attribute('height')==before
      blocks.nth(0).evaluate('(el)=>el.dataset.identity="same-diagram"')
      page.evaluate('loadConversation()');expect(blocks.nth(0)).to_have_attribute('data-identity','same-diagram')
     # Markdown file previews share exactly the same rendering and source-copy behavior.
     page.locator('#messages a[data-file-link]').click()
     end=time.monotonic()+10;job=None
     while job is None and time.monotonic()<end:
      jobs=device.request('POST','/api/device/poll',{'capacity':4})['jobs']
      job=next((j for j in jobs if j['kind']=='file_read'),None)
      if job is None:time.sleep(.1)
     assert job
     data=DOCUMENT.encode()
     device.request('POST','/api/device/jobs/'+job['id']+'/complete',{'status':'succeeded','file':{'name':'diagram.md','data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}})
     preview=page.locator('#file-preview-content')
     for i in range(3):
      preview.locator('[data-mermaid]').nth(i).scroll_into_view_if_needed()
      if i<2:expect(preview.locator('[data-mermaid]').nth(i)).to_have_attribute('data-diagram-rendered','1',timeout=30000)
     expect(preview.locator('[data-diagram-rendered]')).to_have_count(2,timeout=30000)
     expect(preview.locator('[data-mermaid]').nth(2).locator('details')).to_have_attribute('open','')
     exercise_diagram_view(page,preview.locator('[data-mermaid]').nth(1),name=='WebKit')
     if name=='Chromium':
      page.locator('#fullscreen-file-preview').click()
      exercise_diagram_view(page,preview.locator('[data-mermaid]').nth(1))
      page.locator('#fullscreen-file-preview').click()
     with page.expect_download() as info:page.locator('#download-file-preview').click()
     assert Path(info.value.path()).read_bytes()==data
     if name=='WebKit':assert page.locator('#file-preview').bounding_box()['width']==390
     page.locator('#close-file-preview').click()
     # Incomplete streamed fences stay code until closed; no render error is shown.
     stream='stream-'+name
     sync('claude','```mermaid\nflowchart LR\n A-->B',stream);page.evaluate('loadConversation()')
     stream_id=hashlib.sha256((identity['id']+':claude-'+stream).encode()).hexdigest()
     last=page.locator('#messages [data-id="'+stream_id+'"]')
     expect(last.locator('pre')).to_contain_text('A-->B')
     expect(last.locator('[data-mermaid]')).to_have_count(0)
     sync('claude','```mermaid\nflowchart LR\n A-->B\n```',stream);page.evaluate('loadConversation()')
     last.scroll_into_view_if_needed()
     expect(last.locator('[data-diagram-rendered]')).to_have_count(1,timeout=30000)
     # Links and HTML in diagrams cannot run code or navigate the host page.
     assert not errors,errors
     hostile='```mermaid\nflowchart LR\n A["<img src=https://untrusted.example.test/pixel onerror=parent.mermaidInjected=true>"] --> B[Safe]\n click B "javascript:parent.mermaidInjected=true"\n```'
     sync('claude',hostile,stream);page.evaluate('loadConversation()')
     last.scroll_into_view_if_needed()
     expect(last.locator('[data-diagram-rendered]')).to_have_count(1,timeout=30000)
     inner=last.frame_locator('iframe')
     assert inner.locator('a[href],a[*|href],svg script,svg [onerror]').count()==0
     assert not page.evaluate('window.mermaidInjected || false')
     assert not external,external
     assert all('Content Security Policy' in error and ('untrusted.example.test/pixel' in error or ('inline' in error.lower() and ('script' in error.lower() or 'event handler' in error.lower()))) for error in errors),errors
     # Do not leave the hostile fixture in the next browser's conversation.
     sync('claude','```mermaid\nflowchart LR\n A-->B\n```',stream)
     print(name+': flowchart/sequence, Codex/Claude, file previews/downloads, source copy, mobile overflow, malformed/streamed fences and sandbox checks passed',flush=True)
     ctx.close();browser.close()
   assert all(j['kind']=='file_read' for j in client.request('GET','/api/state')['jobs'])
  finally:server.close()
 print('Mermaid passed in Chromium and mobile WebKit; no model requests.')

if __name__=='__main__':main()
