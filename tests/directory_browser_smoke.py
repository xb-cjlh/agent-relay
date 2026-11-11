"""Real device directory browsing, recent paths, creation and mobile layout; no models."""
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server,binary
from test_relay import TOKEN
from test_sessions import codex_records,write_records
from playwright.sync_api import expect,sync_playwright


def main():
 with tempfile.TemporaryDirectory() as temp:
  root=Path(temp).resolve();server=Server(('127.0.0.1',0),root/'server',TOKEN);client=Client('http://127.0.0.1:'+str(server.server_port),TOKEN);daemons=[]
  try:
   bindir=root/'bin';bindir.mkdir()
   for kind in ['codex','claude']:
    path=bindir/kind;path.write_text('#!/bin/sh\nexit 2\n');path.chmod(0o755)
   devices=[]
   for i in range(2):
    folder=root/('device-'+str(i));project=folder/'project';project.mkdir(parents=True)
    for name in ['child-a','中文项目','<img src=x>']:(project/name).mkdir()
    native=folder/'codex';write_records(native/'sessions'/'native.jsonl',codex_records(project,'native-'+str(i)))
    env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'CODEX_HOME':str(native),'CLAUDE_CONFIG_DIR':str(folder/'claude'),'AM_CONFIG_DIR':str(folder/'config')}
    daemon=subprocess.Popen([binary(),'--server',client.server,'--token',TOKEN,'daemon','run','--virtual-device','--name','folder-device-'+str(i),'--root',str(folder),'--state-dir',str(folder/'state'),'--interval','0.1'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);daemons.append(daemon)
    deadline=time.monotonic()+15
    while not (folder/'state'/'identity.json').exists():
     assert time.monotonic()<deadline;time.sleep(.1)
    devices.append((json.loads((folder/'state'/'identity.json').read_text())['id'],project))
   with sync_playwright() as p:
    for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
     chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
     browser=engine.launch(**({'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}))
     context=browser.new_context(viewport={'width':1400,'height':900} if name=='Chromium' else {'width':390,'height':844},**({'is_mobile':True,'has_touch':True} if name=='WebKit' else {}))
     page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
     page.goto(client.server);server.browser_login(page)
     expect(page.locator('#workspace')).to_be_visible()
     if name=='WebKit':page.locator('#open-sidebar').click()
     page.locator('#device-filter').select_option(devices[0][0]);page.locator('#new-agent').click()
     expect(page.locator('#recent-directory-items button').first).to_be_visible(timeout=20000)
     recent=page.locator('[data-recent-directory]')
     index=recent.evaluate_all('(els,path)=>els.findIndex(el=>el.dataset.recentDirectory===path)',str(devices[0][1]))
     assert index>=0,'native workdir missing from recent directories'
     recent.nth(index).click();expect(page.locator('#agent-cwd')).to_have_value(str(devices[0][1]))
     page.locator('#browse-agent-directory').click()
     expect(page.locator('.directory-entry')).to_have_count(3,timeout=20000)
     assert page.locator('#directory-entries img').count()==0
     page.locator('.directory-entry').filter(has_text='child-a').click()
     expect(page.locator('#directory-current')).to_have_text(str(devices[0][1]/'child-a'),timeout=20000)
     expect(page.locator('#choose-directory')).to_be_enabled(timeout=20000)
     page.locator('#directory-parent').click();expect(page.locator('.directory-entry')).to_have_count(3,timeout=20000)
     page.locator('.directory-entry').filter(has_text='中文项目').click()
     expect(page.locator('#choose-directory')).to_be_enabled(timeout=20000)
     page.locator('#choose-directory').click()
     chosen=str(devices[0][1]/'中文项目');expect(page.locator('#agent-cwd')).to_have_value(chosen)
     assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
     page.locator('#agent-form button[type=submit]').click()
     expect(page.locator('#agent-dialog')).not_to_be_visible()
     expect(page.locator('#conversation-meta')).to_contain_text(chosen)
     if name=='WebKit' and 'open' not in page.locator('#sidebar').get_attribute('class').split():page.locator('#open-sidebar').click()
     page.locator('#new-agent').click();expect(page.locator('#recent-directory-items button').first).to_have_attribute('data-recent-directory',chosen,timeout=20000)
     page.locator('#agent-device').select_option(devices[1][0])
     expect(page.locator('#recent-directory-items button').first).to_have_attribute('data-recent-directory',str(devices[1][1]),timeout=20000)
     assert chosen not in page.locator('#recent-directory-items').inner_text()
     page.locator('#recent-directory-items button').first.click();expect(page.locator('#agent-cwd')).to_have_value(str(devices[1][1]))
     page.locator('[data-close=agent-dialog]').click()
     assert not errors,errors
     assert not [j for j in client.request('GET','/api/state')['jobs'] if j['kind']=='prompt'],'folder browsing started a model job'
     print(name+': real Go folder browsing, parent/child selection, native names escaped, recent paths, device switching and creation passed',flush=True)
     context.close();browser.close()
  finally:
   for daemon in daemons:daemon.terminate()
   for daemon in daemons:
    try:daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:daemon.kill();daemon.wait()
    daemon.stderr.close()
   server.close()


if __name__=='__main__':main()
