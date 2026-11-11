"""Original Codex/Claude titles through a real Go daemon into both browser views."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client,Server,binary
from test_relay import TOKEN
from test_sessions import codex_records,write_records
from playwright.sync_api import expect,sync_playwright


def append(path,record):
    with path.open('a') as out:out.write(json.dumps(record,ensure_ascii=False)+'\n')


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);server=Server(('127.0.0.1',0),root/'server',TOKEN)
        base='http://127.0.0.1:'+str(server.server_port);client=Client(base,TOKEN);daemons=[]
        try:
            with sync_playwright() as p:
                for name,engine in [('Chromium',p.chromium),('WebKit',p.webkit)]:
                    directory=root/name;project=directory/'backend';project.mkdir(parents=True)
                    codex=directory/'codex';claude=directory/'claude'
                    sid='codex-'+name;csid='claude-'+name
                    write_records(codex/'sessions'/'native.jsonl',codex_records(project,sid))
                    write_records(codex/'sessions'/'untitled.jsonl',codex_records(project,'untitled-'+name))
                    index=codex/'session_index.jsonl';index.write_text(json.dumps({'id':sid,'thread_name':'修复登录页面'})+'\n')
                    transcript=claude/'projects'/'fixture'/('native-'+name+'.jsonl')
                    write_records(transcript,[{'type':'user','sessionId':csid,'cwd':str(project),'uuid':'question-'+name,'message':{'content':'fixture question'}},
                        {'type':'assistant','sessionId':csid,'cwd':str(project),'uuid':'answer-'+name,'message':{'content':'fixture answer','stop_reason':'end_turn'}},
                        {'type':'ai-title','sessionId':csid,'aiTitle':'优化会话列表'}])
                    env=dict(os.environ,AM_CONFIG_DIR=str(directory/'config'),CODEX_HOME=str(codex),CLAUDE_CONFIG_DIR=str(claude))
                    daemon=subprocess.Popen([binary(),'--server',base,'--token',TOKEN,'daemon','run','--virtual-device','--name','titles-'+name,
                        '--root',str(directory),'--state-dir',str(directory/'state'),'--interval','0.1'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                    daemons.append(daemon)
                    deadline=time.monotonic()+20
                    while not (directory/'state'/'identity.json').exists():
                        assert time.monotonic()<deadline,'daemon registration timed out';time.sleep(.1)
                    device_id=json.loads((directory/'state'/'identity.json').read_text())['id']
                    chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser=engine.launch(**({'executable_path':str(chrome)} if name=='Chromium' and chrome.exists() else {}))
                    context=browser.new_context(viewport={'width':1400,'height':900} if name=='Chromium' else {'width':390,'height':844},
                        **({'is_mobile':True,'has_touch':True} if name=='WebKit' else {}))
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(base);server.browser_login(page)
                    if name=='WebKit':page.locator('#open-sidebar').click()
                    page.locator('#device-filter').select_option(device_id)
                    items=page.locator('#session-items [data-agent]');expect(items).to_have_count(3,timeout=20000)
                    expect(page.locator('.session-name').filter(has_text='修复登录页面')).to_have_count(1)
                    expect(page.locator('.session-name').filter(has_text='优化会话列表')).to_have_count(1)
                    expect(page.locator('.session-name').filter(has_text='backend')).to_have_count(1)
                    codex_agent=next(a for a in client.request('GET','/api/state')['agents'] if a['device_id']==device_id and a['session_id']==sid)
                    stable_id=codex_agent['id'];stable_name=codex_agent['name']
                    items.filter(has=page.locator('.session-name').filter(has_text='修复登录页面')).click()
                    expect(page.locator('#conversation-title')).to_have_text('修复登录页面')
                    page.locator('#prompt').fill('preserve my draft')
                    if name=='WebKit':page.locator('#open-sidebar').click()
                    page.locator('[data-organization="directory"]').click()
                    expect(items).to_have_count(3)
                    expect(page.locator('.session-name').filter(has_text='修复登录页面')).to_have_count(1)
                    expect(page.locator('#prompt')).to_have_value('preserve my draft')
                    # Index-only rename does not require changing/re-reading the Codex transcript.
                    append(index,{'id':sid,'thread_name':'修复图片上传'})
                    expect(page.locator('#conversation-title')).to_have_text('修复图片上传',timeout=20000)
                    expect(page.locator('.session-name').filter(has_text='修复图片上传')).to_have_count(1)
                    expect(page.locator('#prompt')).to_have_value('preserve my draft')
                    page.locator('#search').fill('修复图片上传');expect(items).to_have_count(1)
                    expect(page.locator('.directory-count')).to_have_text('1')
                    page.locator('#search').fill('');expect(items).to_have_count(3)
                    append(transcript,{'type':'custom-title','sessionId':csid,'customTitle':'手动命名的 Claude 会话'})
                    append(transcript,{'type':'ai-title','sessionId':csid,'aiTitle':'后来的自动标题'})
                    expect(page.locator('.session-name').filter(has_text='手动命名的 Claude 会话')).to_have_count(1,timeout=20000)
                    append(transcript,{'type':'custom-title','sessionId':csid,'customTitle':''})
                    expect(page.locator('.session-name').filter(has_text='后来的自动标题')).to_have_count(1,timeout=20000)
                    # Long and untrusted text remains plain text, clipped to the sidebar width.
                    hostile='<img src=x onerror=window.titleInjected=true> '+('长标题'*65)
                    append(index,{'id':sid,'thread_name':hostile})
                    expect(page.locator('#conversation-title')).to_have_text(hostile,timeout=20000)
                    title=page.locator('.session-name').filter(has_text='<img')
                    expect(title).to_have_text(hostile)
                    assert title.evaluate('(el)=>el.scrollWidth>el.clientWidth')
                    assert page.locator('#session-items img,#conversation-title img').count()==0
                    assert not page.evaluate('window.titleInjected || false')
                    assert page.locator('#session-list').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.reload();expect(page.locator('#workspace')).to_be_visible()
                    expect(page.locator('#conversation-title')).to_have_text(hostile)
                    expect(page.locator('#prompt')).to_have_value('preserve my draft')
                    current=next(a for a in client.request('GET','/api/state')['agents'] if a['id']==stable_id)
                    assert current['name']==stable_name and current['title']==hostile
                    conversation=client.request('GET','/api/agents/'+stable_id+'/messages')
                    assert all(m.get('content') not in ('修复登录页面','修复图片上传',hostile) for m in conversation['messages'])
                    assert not errors,errors
                    browser.close()
                    print(name+': real Go Codex index/Claude AI+custom titles, rename, search, both organizations, defaults, safe long titles and preserved IDs/drafts passed',flush=True)
            assert client.request('GET','/api/state')['jobs']==[],'title sync must never start model/file jobs'
        finally:
            for daemon in daemons:daemon.send_signal(signal.SIGINT)
            for daemon in daemons:
                try:daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:daemon.kill();daemon.wait()
                daemon.stderr.close()
            server.close()


if __name__=='__main__':main()
