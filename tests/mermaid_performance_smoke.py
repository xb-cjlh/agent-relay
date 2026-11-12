"""One lazy Mermaid engine, deduplicated output, and stable streaming diagrams."""
import hashlib
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, Server
from test_relay import TOKEN
from playwright.sync_api import expect, sync_playwright

FLOW = '```mermaid\nflowchart LR\n A[请求] --> B[响应]\n```\n'
SEQUENCE = '```mermaid\nsequenceDiagram\n Alice->>Bob: hello\n```\n'


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        server = Server(('127.0.0.1', 0), root / 'server', TOKEN)
        base = 'http://127.0.0.1:' + str(server.server_port)
        client = Client(base, TOKEN)
        try:
            identity = client.request('POST', '/api/register', {'name':'performance', 'platform':'test', 'roots':[], 'capabilities':{'codex':True, 'unlimited_tasks':True}})
            device = Client(base, identity['token'])
            stamp = time.time()
            def sync(content):
                device.request('POST', '/api/device/poll', {'sessions':[{'kind':'codex', 'session_id':'performance', 'name':'performance', 'cwd':str(root), 'busy':False, 'updated_at':time.time(), 'messages':[{'id':'stream', 'role':'assistant', 'content':content, 'created_at':stamp}]}]})
            source = '# 图表\n\n' + FLOW + '\n原有文字\n\n' + FLOW + '\n' + SEQUENCE
            sync(source)
            message_id = hashlib.sha256((identity['id'] + ':stream').encode()).hexdigest()
            with sync_playwright() as playwright:
                for name, engine in [('Chromium', playwright.chromium), ('WebKit', playwright.webkit)]:
                    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser = engine.launch(**({'executable_path':str(chrome)} if name == 'Chromium' and chrome.exists() else {}))
                    context = browser.new_context(viewport={'width':1400,'height':1200} if name == 'Chromium' else {'width':390,'height':844}, **({'is_mobile':True,'has_touch':True} if name == 'WebKit' else {}))
                    page = context.new_page()
                    requests, errors = [], []
                    page.on('request', lambda request: requests.append(request.url) if '/vendor/mermaid' in request.url else None)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.add_init_script("window.diagramResults=[];window.addEventListener('message',e=>{if(e.data?.type==='relay-mermaid-rendered')window.diagramResults.push(e.data.id)})")
                    page.goto(base); server.browser_login(page)
                    body = page.locator('#messages [data-id="' + message_id + '"]')
                    blocks = body.locator('[data-mermaid]')
                    expect(blocks).to_have_count(3)
                    start = time.monotonic()
                    for index in range(3):
                        blocks.nth(index).scroll_into_view_if_needed()
                        expect(blocks.nth(index)).to_have_attribute('data-diagram-rendered','1',timeout=15000)
                    cold_ms = round((time.monotonic()-start)*1000)
                    expect(page.locator('.mermaid-engine')).to_have_count(1)
                    assert page.frame_locator('.mermaid-engine').locator('body').evaluate('()=>{try{return !parent.document}catch{return true}}')
                    assert len(page.evaluate('diagramResults')) == 2, 'identical diagrams rendered twice'
                    assert not any(url.endswith('/vendor/mermaid.min.js') for url in requests), 'full bundle loaded'
                    module_urls = set(requests)
                    loaded_bytes = sum((Path(__file__).resolve().parents[1] / 'relayctl/static' / url.split(base + '/',1)[1]).stat().st_size for url in module_urls)
                    assert loaded_bytes < 1500000, loaded_bytes
                    assert not any('/elk-' in url for url in requests), 'ordinary flowcharts loaded the large ELK layout'
                    gzip_bytes = sum(len(gzip.compress((Path(__file__).resolve().parents[1] / 'relayctl/static' / url.split(base + '/',1)[1]).read_bytes())) for url in module_urls)
                    for index in range(3):
                        blocks.nth(index).frame_locator('iframe').locator('body').evaluate('(el,index)=>window.streamingIdentity=index', index)
                    # Append text in the middle and at the end without rebuilding any diagram.
                    updated = source.replace('原有文字', '原有文字，继续流式输出。')
                    sync(updated + '\n\n补充回复 ' + name)
                    page.evaluate('loadConversation()')
                    expect(body).to_contain_text('补充回复 ' + name)
                    for index in range(3):
                        assert blocks.nth(index).frame_locator('iframe').locator('body').evaluate('window.streamingIdentity') == index, 'streaming restarted a diagram frame'
                    assert len(page.evaluate('diagramResults')) == 2
                    assert set(requests) == module_urls
                    # A fresh view of the same source uses cached SVG, without model or layout work.
                    page.evaluate('document.getElementById("messages").replaceChildren();renderMessages()')
                    for index in range(3):
                        blocks.nth(index).scroll_into_view_if_needed()
                        expect(blocks.nth(index)).to_have_attribute('data-diagram-rendered','1',timeout=10000)
                    assert len(page.evaluate('diagramResults')) == 2
                    assert set(requests) == module_urls
                    assert not errors, errors
                    page.evaluate('logout(true)')
                    expect(page.locator('.mermaid-engine')).to_have_count(0)
                    assert page.evaluate('diagramCache.size + diagramPending.size + diagramQueue.length') == 0
                    print(json.dumps({'browser':name,'first_three_diagrams_ms':cold_ms,'unique_diagrams_rendered':2,'engine_instances':1,'loaded_library_bytes':loaded_bytes,'gzip_library_bytes':gzip_bytes,'streaming_frames_preserved':True,'repeat_layouts':0,'logout_cleared':True}),flush=True)
                    context.close(); browser.close()
                    sync(source)
            assert not client.request('GET','/api/state')['jobs'], 'diagram rendering started a remote task'
        finally:
            server.close()


if __name__ == '__main__':
    main()
