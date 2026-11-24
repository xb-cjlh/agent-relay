"""Real default-cadence daemon, filesystem events, direct OSS updates and browser leases."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server,binary
from test_relay import TOKEN
from oss_support import OssFixture
from playwright.sync_api import sync_playwright,expect


def main():
 with tempfile.TemporaryDirectory() as temp:
  root=Path(temp);oss=OssFixture();server=Server(('127.0.0.1',0),root/'data',TOKEN,environment=oss.environment());base='http://127.0.0.1:'+str(server.server_port);client=Client(base,TOKEN);daemon=None
  try:
   project=root/'project';project.mkdir();document=project/'note.md';document.write_text('# Original\n');(root/'codex').mkdir();(root/'claude').mkdir()
   daemon=subprocess.Popen([binary(),'--server',base,'--token',TOKEN,'daemon','run','--virtual-device','--name','file-watch','--state-dir',str(root/'identity')],env=dict(os.environ,AM_CONFIG_DIR=str(root/'config'),CODEX_HOME=str(root/'codex'),CLAUDE_CONFIG_DIR=str(root/'claude')),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
   deadline=time.monotonic()+20
   while not (root/'identity/identity.json').exists():assert time.monotonic()<deadline;time.sleep(.05)
   identity=json.loads((root/'identity/identity.json').read_text());dev=Client(base,identity['token'])
   dev.request('POST','/api/device/poll',{'sessions':[{'kind':'codex','session_id':'file-watch','name':'watch fixture','cwd':str(project),'updated_at':time.time(),'messages':[{'id':'link','role':'assistant','content':'[文件](note.md)','created_at':time.time()}]}]})
   agent=client.request('GET','/api/state')['agents'][0]
   # Complete a normal read immediately, independently of the two-second heartbeat.
   job=client.request('POST','/api/jobs',{'kind':'file_read','device':identity['id'],'path':str(document)})
   finished=client.wait(job['id'],20);latency=finished['finished_at']-finished['started_at'];assert latency<1.5,latency
   with sync_playwright() as p:
    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
     document.write_text('# Original\n')
     browser=engine.launch(**({'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}));context=browser.new_context(viewport={'width':1400,'height':900});page=context.new_page();errors=[]
     page.on('pageerror',lambda error:errors.append(str(error)));page.goto(base);server.browser_login(page);page.evaluate('id=>selectAgent(id)',agent['id']);page.locator('#messages [data-file-link]').click()
     expect(page.locator('#file-preview-content h1')).to_have_text('Original',timeout=15000)
     page.wait_for_function('()=>filePreview?.watchExpiresAt>Date.now()')
     # Let the daemon install parent-directory watches and finish its initial verification.
     page.wait_for_timeout(3500);page.evaluate('clearTimeout(filePreview.checkTimer)')
     before=len([r for r in oss.requests if r['method']=='PUT']);start=time.monotonic();document.write_text('# Live event\n')
     expect(page.locator('#file-preview-content h1')).to_have_text('Live event',timeout=5000);event_latency=time.monotonic()-start
     assert len([r for r in oss.requests if r['method']=='PUT'])==before+1
     # The timer is disabled: only device events plus the existing state stream can update this.
     replacement=project/'save.tmp';replacement.write_text('# Atomic save\n');os.replace(replacement,document)
     expect(page.locator('#file-preview-content h1')).to_have_text('Atomic save',timeout=5000)
     baseline=(len(client.request('GET','/api/state')['jobs']),len(oss.requests))
     page.wait_for_timeout(3500)
     assert len(client.request('GET','/api/state')['jobs'])==baseline[0],'unchanged files created polling jobs'
     assert len(oss.requests)==baseline[1],'watch metadata queried OSS or transferred unchanged bytes'
     # Another browser can view the same path; closing one must keep the other's lease.
     other=browser.new_context();reader=other.new_page();reader.goto(base);server.browser_login(reader);reader.evaluate('id=>selectAgent(id)',agent['id']);reader.locator('#messages [data-file-link]').click()
     expect(reader.locator('#file-preview-content h1')).to_have_text('Atomic save');reader.wait_for_function('()=>filePreview?.watchExpiresAt>Date.now()')
     page.locator('#close-file-preview').click();document.write_text('# Other viewer\n');expect(reader.locator('#file-preview-content h1')).to_have_text('Other viewer',timeout=5000)
     # Background tabs release leases and do no checking; foreground resumes the watch.
     reader.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
     reader.wait_for_function('()=>!filePreview.watchId');reader.wait_for_timeout(300)
     assert not dev.request('POST','/api/device/poll',{})['file_watches']
     document.write_text('# Foreground catchup\n')
     reader.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))")
     expect(reader.locator('#file-preview-content h1')).to_have_text('Foreground catchup',timeout=6000)
     # Fallback cadence is adaptive; legacy devices retain their safe transfer interval.
     intervals=reader.evaluate("[0,70000,200000,700000].map(age=>filePreviewCheckInterval({...filePreview,lastChangeAt:Date.now()-age}))")
     assert intervals==[2500,5000,15000,30000],intervals
     reader.locator('#close-file-preview').click();reader.wait_for_timeout(300);assert not dev.request('POST','/api/device/poll',{})['file_watches']
     assert not errors,errors
     print(name+': event update %.2fs, immediate completion %.2fs; atomic saves, no idle jobs/OSS requests, multiple viewers, background stop/resume and adaptive fallback passed'%(event_latency,latency),flush=True)
     other.close();context.close();browser.close()
   assert all(j['kind']=='file_read' for j in client.request('GET','/api/state')['jobs'])
  finally:
   if daemon is not None:daemon.terminate();daemon.wait(timeout=15);daemon.stderr.close()
   server.close();oss.close()

if __name__=='__main__':main()
