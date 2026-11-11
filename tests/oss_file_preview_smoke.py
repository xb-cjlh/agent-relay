"""Actual Go -> OSS -> browser transfers, cross-browser reuse and offline snapshots."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server,binary
from test_relay import TOKEN
from oss_support import OssFixture
from playwright.sync_api import sync_playwright,expect


def main():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);oss=OssFixture()
        server=Server(('127.0.0.1',0),root/'data',TOKEN,environment=oss.environment())
        base='http://127.0.0.1:'+str(server.server_port)
        client=Client(base,TOKEN);daemon=None
        try:
            project=root/'project';project.mkdir();document=project/'note.md';document.write_text('# OSS original\n\noriginal content\n')
            (root/'codex').mkdir();(root/'claude').mkdir()
            daemon=subprocess.Popen([binary(),'--server',base,'--token',TOKEN,'daemon','run','--virtual-device','--name','oss-files','--state-dir',str(root/'state'),'--interval','0.1'],env=dict(os.environ,AM_CONFIG_DIR=str(root/'config'),CODEX_HOME=str(root/'codex'),CLAUDE_CONFIG_DIR=str(root/'claude')),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            end=time.monotonic()+20
            while not (root/'state'/'identity.json').exists():
                assert time.monotonic()<end;time.sleep(.1)
            identity=json.loads((root/'state'/'identity.json').read_text());device=Client(base,identity['token'])
            device.request('POST','/api/device/poll',{'sessions':[{'kind':'codex','session_id':'oss-preview','name':'OSS preview','cwd':str(project),'busy':False,'updated_at':time.time(),'messages':[{'id':'oss-link','role':'assistant','content':'[文件](note.md)','created_at':time.time()}]}]})
            agent=client.request('GET','/api/state')['agents'][0]
            with sync_playwright() as p:
                chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                browser=p.chromium.launch(**({'executable_path':str(chrome)} if chrome.exists() else {}));ctx=browser.new_context()
                page=ctx.new_page();errors=[];writes=[];proxy_reads=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda req:writes.append(req.post_data_json) if req.method=='POST' and req.url==base+'/api/jobs' else None)
                page.on('request',lambda req:proxy_reads.append(req.url) if req.url.startswith(base+'/api/files/file_') and not req.url.endswith('/url') else None)
                page.goto(base);server.browser_login(page)
                page.evaluate('id=>selectAgent(id)',agent['id'])
                page.locator('#messages a[data-file-link]').click()
                expect(page.locator('#file-preview-content h1')).to_have_text('OSS original',timeout=20000)
                assert len(writes)==1 and writes[0]['kind']=='file_read'
                assert not proxy_reads
                assert len([r for r in oss.requests if r['method']=='PUT'])==1
                assert len([r for r in oss.requests if r['method']=='GET'])==1
                assert not list(server.directory.joinpath('files').iterdir())
                print('Chromium: direct device upload and direct browser preview passed',flush=True)
                document.write_text('# OSS updated\n\nchanged content\n')
                expect(page.locator('#file-preview-content h1')).to_have_text('OSS updated',timeout=22000)
                assert len([r for r in oss.requests if r['method']=='PUT'])==2
                before=(len([r for r in oss.requests if r['method']=='PUT']),len([r for r in oss.requests if r['method']=='GET']))
                page.evaluate('fetchFilePreview(filePreview)')
                expect(page.locator('#file-preview-info')).to_contain_text('已是最新',timeout=15000)
                assert before==(len([r for r in oss.requests if r['method']=='PUT']),len([r for r in oss.requests if r['method']=='GET']))
                page.locator('#close-file-preview').click()
                daemon.terminate();daemon.wait(timeout=10)
                server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?',(identity['id'],))
                mobile=p.webkit.launch();fresh=mobile.new_context(viewport={'width':390,'height':650},is_mobile=True,has_touch=True)
                other=fresh.new_page();other_writes=[]
                other.on('pageerror',lambda error:errors.append(str(error)))
                other.on('request',lambda req:other_writes.append(req.post_data_json) if req.method=='POST' and req.url==base+'/api/jobs' else None)
                other.goto(base);server.browser_login(other)
                other.evaluate('id=>selectAgent(id)',agent['id'])
                other.locator('#messages a[data-file-link]').click()
                expect(other.locator('#file-preview-content h1')).to_have_text('OSS updated',timeout=15000)
                assert not other_writes,'fresh browser queued an offline device read instead of using OSS'
                with other.expect_download() as info:other.locator('#download-file-preview').click()
                assert Path(info.value.path()).read_bytes()==document.read_bytes()
                assert other.locator('#file-preview').bounding_box()['width']==390
                for request in oss.requests:
                    assert not any(k.lower()=='authorization' for k in request['headers']) or request['method'] in ('HEAD',),request['method']
                assert not errors,errors
                assert all(j['kind']=='file_read' for j in client.request('GET','/api/state')['jobs'])
                print('WebKit: fresh browser reused OSS while source device offline; no device jobs, original download passed',flush=True)
                fresh.close();mobile.close();ctx.close();browser.close()
        finally:
            if daemon:
                if daemon.poll() is None:daemon.terminate();daemon.wait(timeout=10)
                daemon.stderr.close()
            server.close();oss.close()
    print('OSS file previews passed: direct transfers, automatic changes, unchanged checks, cross-browser offline cache and downloads; no model requests.')

if __name__=='__main__':main()
