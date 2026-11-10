"""Incremental session lists, loading retries and stale filter responses."""
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, Server
from test_relay import TOKEN
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        server = Server(('127.0.0.1', 0), root / 'data', TOKEN)
        base = 'http://127.0.0.1:' + str(server.server_port)
        client = Client(base, TOKEN)
        try:
            devices = []
            for name, count in [('work', 45), ('home', 40)]:
                identity = client.request('POST', '/api/register', {
                    'name': name, 'platform': 'fixture', 'roots': [],
                    'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True}})
                device = Client(base, identity['token'])
                device.request('POST', '/api/device/poll', {'sessions': [{
                    'kind': 'codex' if name == 'work' else 'claude',
                    'session_id': name + '-' + str(i), 'name': name + '-project-' + str(i),
                    'cwd': str(root / (name + '-' + str(i))), 'updated_at': 1000 + i,
                    'busy': False} for i in range(count)]})
                devices.append(identity)
            all_agents = client.request('GET', '/api/state')['agents']
            oldest = min(all_agents, key=lambda a: (a['last_activity'], a['id']))
            with sync_playwright() as p:
                for name, engine in [('Chromium', p.chromium), ('WebKit', p.webkit)]:
                    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser = engine.launch(**({'executable_path': str(chrome)} if name == 'Chromium' and chrome.exists() else {}))
                    context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    # Polling is covered by session_list_smoke; keep race cases deterministic here.
                    page.add_init_script('window.setInterval=()=>0;')
                    page.goto(base)
                    server.browser_login(page)
                    page.locator('#open-sidebar').click()
                    items = page.locator('#session-list [data-agent]')
                    more = page.locator('#load-more-sessions')
                    expect(items).to_have_count(30)
                    expect(page.locator('#device-filter option').first).to_have_text('全部设备 · 85 个会话')
                    expect(more).to_have_text('加载更多')
                    first_ids = items.evaluate_all('(nodes)=>nodes.map(el=>el.dataset.agent)')
                    page.locator('#prompt').fill('draft survives list pagination')
                    selected = page.evaluate('selected')

                    pending = []
                    page.route('**/api/agents?*', lambda route: pending.append(route))
                    more.evaluate('(el)=>{el.click();el.click();}')
                    expect(more).to_have_text('正在加载…')
                    page.wait_for_timeout(100)
                    assert len(pending) == 1
                    pending.pop().fulfill(status=503, content_type='application/json', body='{"error":"fixture retry"}')
                    expect(more).to_have_text('加载失败，点击重试')
                    expect(items).to_have_count(30)
                    more.evaluate('(el)=>el.click()')
                    page.wait_for_timeout(100)
                    pending.pop().continue_()
                    expect(items).to_have_count(60)
                    page.unroute('**/api/agents?*')
                    ids = items.evaluate_all('(nodes)=>nodes.map(el=>el.dataset.agent)')
                    assert ids[:30] == first_ids and len(set(ids)) == 60
                    assert page.evaluate('selected') == selected
                    expect(page.locator('#prompt')).to_have_value('draft survives list pagination')
                    page.evaluate('refresh()')
                    expect(items).to_have_count(60)
                    page.locator('#session-list').evaluate('(el)=>el.scrollTop=el.scrollHeight')
                    expect(items).to_have_count(85)
                    expect(more).not_to_be_visible()
                    assert len(set(items.evaluate_all('(nodes)=>nodes.map(el=>el.dataset.agent)'))) == 85

                    # Search must find an old session even after the list is reset to page one.
                    page.locator('#search').fill(oldest['cwd'])
                    expect(items).to_have_count(1)
                    expect(items.first).to_have_attribute('data-agent', oldest['id'])
                    page.locator('#search').fill('')
                    expect(items).to_have_count(30)

                    # A delayed page for all devices must not leak into the newly selected device.
                    page.route('**/api/agents?*', lambda route: pending.append(route))
                    more.evaluate('(el)=>el.click()')
                    page.wait_for_timeout(100)
                    old_page = pending.pop()
                    old_path = urlsplit(old_page.request.url)
                    old_data = client.request('GET', old_path.path + '?' + old_path.query)
                    page.locator('#device-filter').select_option(devices[1]['id'])
                    expect(items).to_have_count(30)
                    old_page.fulfill(status=200, content_type='application/json', body=json.dumps(old_data))
                    page.wait_for_timeout(100)
                    assert all('home' in title for title in items.evaluate_all('(nodes)=>nodes.map(el=>el.title)'))
                    page.unroute('**/api/agents?*')
                    more.evaluate('(el)=>el.click()')
                    expect(items).to_have_count(40)
                    expect(more).not_to_be_visible()
                    expect(page.locator('#device-filter option').nth(1)).to_contain_text('40 个会话')

                    # Restoring an older selected session does not require loading every list page.
                    page.locator('#device-filter').select_option('')
                    expect(items).to_have_count(30)
                    page.evaluate('(id)=>{sessionStorage.setItem("relay-selected",id);sessionStorage.removeItem("relay-device-filter");}', oldest['id'])
                    page.reload()
                    expect(page.locator('#workspace')).to_be_visible()
                    expect(items).to_have_count(30)
                    assert page.evaluate('selected') == oldest['id']
                    expect(page.locator('#conversation-title')).to_have_text(oldest['name'])
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not errors, errors
                    browser.close()
                    print(name + ': session pagination, scroll/click, deduplication, retries, full search, filter races, drafts and restored selection passed', flush=True)
        finally:
            server.close()


if __name__ == '__main__':
    main()
