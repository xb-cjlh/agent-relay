"""Directory organization across devices, complete search, pagination and live changes."""
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
            for name, groups in [('work', [('/workspace/team/app', 36), ('/workspace/other/app', 3), ('/workspace/Team/app', 1), ('/workspace/中文 & "project"', 1)]),
                                 ('home', [('/workspace/team/app', 4)])]:
                identity = client.request('POST', '/api/register', {'name': name, 'platform': 'fixture', 'roots': [],
                    'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True, 'home': '/workspace'}})
                device = Client(base, identity['token'])
                sessions = []
                for group, (cwd, count) in enumerate(groups):
                    for i in range(count):
                        sid = name + '-' + str(group) + '-' + str(i)
                        stamp = 1000 + i - group * 100 - (10 if name == 'home' else 0)
                        sessions.append({'kind': 'codex' if i % 2 else 'claude', 'session_id': sid,
                            'name': sid, 'cwd': cwd, 'updated_at': stamp, 'busy': False,
                            'messages': [{'id': sid, 'role': 'assistant', 'content': 'old needle' if sid == 'work-0-0' else sid, 'created_at': stamp}]})
                device.request('POST', '/api/device/poll', {'sessions': sessions})
                devices.append((identity, device))
            all_agents = client.request('GET', '/api/state')['agents']
            work = devices[0][0]['id']; home = devices[1][0]['id']
            def key(device, cwd): return json.dumps([device, cwd], separators=(',', ':'), ensure_ascii=False)
            with sync_playwright() as p:
                for name, engine in [('Chromium', p.chromium), ('WebKit', p.webkit)]:
                    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser = engine.launch(**({'executable_path': str(chrome)} if name == 'Chromium' and chrome.exists() else {}))
                    context = browser.new_context(viewport={'width': 1440, 'height': 900} if name == 'Chromium' else {'width': 390, 'height': 844},
                        **({'is_mobile': True, 'has_touch': True} if name == 'WebKit' else {}))
                    page = context.new_page(); errors = []; writes = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.on('request', lambda r: writes.append(r.url) if r.method == 'POST' and r.url == base + '/api/jobs' else None)
                    page.add_init_script('window.setInterval=()=>0')
                    page.goto(base); server.browser_login(page)
                    if name == 'WebKit': page.locator('#open-sidebar').click()
                    items = page.locator('#session-list [data-agent]')
                    expect(items).to_have_count(30)
                    selected = page.evaluate('selected')
                    page.locator('#prompt').fill('keep this draft')
                    page.locator('[data-organization="directory"]').click()
                    headings = page.locator('[data-directory]')
                    expect(headings).to_have_count(5)
                    def group(device, cwd):
                        # JSON keys may contain quotes and non-ASCII paths; avoid CSS interpolation.
                        index = headings.evaluate_all('(els,key)=>els.findIndex(el=>el.dataset.directory===key)', key(device, cwd))
                        assert index >= 0, (device, cwd)
                        return headings.nth(index)
                    expect(group(work, '/workspace/team/app')).to_have_attribute('aria-expanded', 'true')
                    expect(group(work, '/workspace/team/app').locator('.directory-count')).to_have_text('36')
                    expect(items).to_have_count(30)
                    assert page.evaluate('selected') == selected
                    expect(page.locator('#prompt')).to_have_value('keep this draft')
                    expect(page.locator('#session-list .agent-codex').first).to_be_attached()
                    expect(page.locator('#session-list .agent-claude').first).to_be_attached()
                    expect(headings.locator('.directory-name').filter(has_text='app')).to_have_count(4)
                    group(work, '/workspace/team/app').click()
                    expect(items).to_have_count(0)
                    expect(page.locator('#load-more-sessions')).to_be_hidden()
                    group(work, '/workspace/team/app').click()
                    page.locator('#load-more-sessions').evaluate('(el)=>el.click()')
                    expect(items).to_have_count(36)
                    page.evaluate('refresh()'); expect(items).to_have_count(36)
                    assert len(set(items.evaluate_all('(els)=>els.map(el=>el.dataset.agent)'))) == 36
                    # Same path on a different device remains separate.
                    group(home, '/workspace/team/app').click(); expect(items).to_have_count(4)
                    assert all('home' in title for title in items.evaluate_all('(els)=>els.map(el=>el.title)'))
                    items.first.click()
                    expect(page.locator('#conversation-meta')).to_contain_text('home · /workspace/team/app')
                    page.locator('#prompt').fill('home draft')
                    if name == 'WebKit': page.locator('#open-sidebar').click()
                    # Same basename, different complete path, and case-sensitive paths stay separate.
                    group(work, '/workspace/other/app').click(); expect(items).to_have_count(3)
                    assert all('/workspace/other/app' in title for title in items.evaluate_all('(els)=>els.map(el=>el.title)'))
                    group(work, '/workspace/Team/app').click(); expect(items).to_have_count(1)
                    assert '/workspace/Team/app' in items.first.get_attribute('title')
                    group(work, '/workspace/中文 & "project"').click(); expect(items).to_have_count(1)
                    expect(group(work, '/workspace/中文 & "project"').locator('.directory-name')).to_have_text('中文 & "project"')
                    # Complete search finds an old session beyond the initial page and updates group totals.
                    page.locator('#search').fill('old needle')
                    expect(headings).to_have_count(1); expect(items).to_have_count(1)
                    expect(headings.locator('.directory-count')).to_have_text('1')
                    page.locator('#search').fill('no results fixture')
                    expect(headings).to_have_count(0); expect(page.locator('.sidebar-empty')).to_have_text('没有匹配的会话。')
                    page.locator('#search').fill(''); expect(headings).to_have_count(5)
                    # Device selection applies to every group; refresh preserves mode and directory.
                    page.locator('#device-filter').select_option(home); expect(headings).to_have_count(1)
                    expect(items).to_have_count(4)
                    page.reload(); expect(page.locator('#workspace')).to_be_visible()
                    expect(page.locator('[data-organization="directory"]')).to_have_attribute('aria-pressed', 'true')
                    expect(headings).to_have_count(1); expect(items).to_have_count(4)
                    expect(page.locator('#prompt')).to_have_value('home draft')
                    if name == 'WebKit': page.locator('#open-sidebar').click()
                    page.locator('#new-agent').click()
                    expect(page.locator('#agent-device')).to_have_value(home)
                    expect(page.locator('#agent-cwd')).to_have_value('/workspace/team/app')
                    page.locator('[data-close="agent-dialog"]').click()
                    # Delayed pagination for one directory cannot populate a different directory.
                    page.locator('#device-filter').select_option(''); expect(headings).to_have_count(5)
                    if group(work, '/workspace/team/app').get_attribute('aria-expanded') != 'true':
                        group(work, '/workspace/team/app').click()
                    expect(items).to_have_count(30)
                    held = []
                    page.route('**/api/agents?*', lambda route: held.append(route))
                    page.locator('#load-more-sessions').evaluate('(el)=>el.click()')
                    expect(page.locator('#load-more-sessions')).to_have_text('正在加载…')
                    page.wait_for_timeout(100); assert len(held) == 1
                    old = held.pop(); url = urlsplit(old.request.url)
                    reply = client.request('GET', url.path + '?' + url.query)
                    group(home, '/workspace/team/app').click(); expect(items).to_have_count(4)
                    old.fulfill(status=200, content_type='application/json', body=json.dumps(reply))
                    page.wait_for_timeout(100); expect(items).to_have_count(4)
                    page.unroute('**/api/agents?*')
                    assert all('home' in title for title in items.evaluate_all('(els)=>els.map(el=>el.title)'))
                    # A previously old directory moves to the top after a live update.
                    other = next(a for a in all_agents if a['cwd'] == '/workspace/other/app')
                    devices[0][1].request('POST', '/api/device/poll', {'sessions': [{
                        'kind': other['kind'], 'session_id': other['session_id'], 'name': other['name'], 'cwd': other['cwd'],
                        'updated_at': 3000 if name == 'Chromium' else 4000, 'busy': False}]})
                    page.evaluate('refresh()')
                    expect(headings.first).to_have_attribute('data-directory', key(work, '/workspace/other/app'))
                    expect(items).to_have_count(4)
                    page.locator('[data-organization="time"]').click(); expect(items).to_have_count(30)
                    expect(headings).to_have_count(0)
                    assert page.locator('#session-list').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('[data-organization="directory"]').click()
                    expect(headings).to_have_count(5)
                    page.locator('#new-agent').click()
                    new_cwd = '/workspace/created-' + name
                    page.locator('#agent-cwd').fill(new_cwd)
                    page.locator('#agent-form button[type="submit"]').click()
                    expect(page.locator('#agent-dialog')).not_to_be_visible()
                    expect(headings).to_have_count(6)
                    expect(items).to_have_count(1)
                    expect(page.locator('#conversation-meta')).to_contain_text(new_cwd)
                    created = page.evaluate('selected')
                    page.reload(); expect(page.locator('#workspace')).to_be_visible()
                    expect(items).to_have_count(1)
                    assert page.evaluate('selected') == created
                    expect(page.locator('#conversation-meta')).to_contain_text(new_cwd)
                    assert not errors, errors
                    assert not writes, writes
                    browser.close()
                    server.database.execute('DELETE FROM agents WHERE id=?', (created,))
                    devices[0][1].request('POST', '/api/device/poll', {'sessions': [{
                        'kind': other['kind'], 'session_id': other['session_id'], 'name': other['name'], 'cwd': other['cwd'],
                        'updated_at': other['last_activity'], 'busy': False}]})
                    print(name + ': directories, exact paths/devices, Codex/Claude, counts, pagination, search, stale requests, reload/drafts, new-session cwd and live order passed', flush=True)
        finally:
            server.close()


if __name__ == '__main__': main()
