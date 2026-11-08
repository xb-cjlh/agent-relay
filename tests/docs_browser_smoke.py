"""Check public installation docs, dynamic origin, copying, downloads and mobile."""
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Server
from playwright.sync_api import sync_playwright, expect

with tempfile.TemporaryDirectory() as folder:
    url=os.environ.get('RELAY_DOCS_TEST_URL','')
    server=None
    if not url:
        server=Server(('127.0.0.1',0),Path(folder)/'data','relay-docs-browser-test-token-0001')
        url='http://127.0.0.1:'+str(server.server_port)
    errors=[]
    try:
        with sync_playwright() as p:
            chrome=Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
            browser=p.chromium.launch(**({'executable_path':str(chrome)} if chrome.is_file() else {}))
            context=browser.new_context(permissions=['clipboard-read','clipboard-write'])
            page=context.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(url)
            page.get_by_role('link',name='安装与使用文档').click()
            expect(page.locator('#manual')).to_contain_text('注册登录')
            expect(page.locator('#manual')).to_contain_text('服务端邮件设置')
            expect(page.locator('#contents a')).to_have_count(page.locator('#manual h2').count())
            expect(page.locator('#download-list a')).to_have_count(5)
            expect(page.locator('#agent-instruction')).to_contain_text(url+'/install-agent.md')
            expect(page.locator('#manual')).to_contain_text('daemon status')
            page.locator('#copy-agent').click()
            expect(page.locator('#copy-agent')).to_have_text('已复制')
            assert url+'/install-agent.md' in page.evaluate('navigator.clipboard.readText()')
            page.locator('#manual .copy-code').first.click()
            assert 'codex --version' in page.evaluate('navigator.clipboard.readText()')
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if os.environ.get('RELAY_DOCS_SCREENSHOT'):
                page.set_viewport_size({'width':1280,'height':1000})
                page.wait_for_timeout(300)
                page.screenshot(path=os.environ['RELAY_DOCS_SCREENSHOT'],animations='disabled')
            assert not errors,errors
            browser.close()
        print('Docs browser passed: unauthenticated entry, full manual, origin, copy, downloads and mobile.')
    finally:
        if server: server.close()
