"""Keep native updates visible while a Relay task is running or queued."""
import sys
import tempfile
import time
from pathlib import Path

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
        identity = client.request('POST', '/api/register', {
            'name': 'sync-fixture', 'platform': 'fixture', 'roots': [],
            'capabilities': {'codex': True, 'claude': True, 'unlimited_tasks': True}})
        device = Client(base, identity['token'])
        try:
            with sync_playwright() as p:
                for name, engine in [('Chromium', p.chromium), ('WebKit', p.webkit)]:
                    agents = []
                    for kind in ['codex', 'claude']:
                        agent = client.request('POST', '/api/agents', {
                            'device': identity['id'], 'name': name + '-' + kind,
                            'kind': kind, 'cwd': str(root)})
                        prompt = 'relay question ' + name + kind
                        job = client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': prompt})
                        claimed = device.request('POST', '/api/device/poll', {'capacity': 1})['jobs']
                        started = next(j['started_at'] for j in claimed if j['id'] == job['id'])
                        stamp = time.time()
                        first = 'first streamed update\n\nSecond paragraph 👋.\n\n```text\nline one\nline two\n```'
                        session = {'kind': kind, 'session_id': name + '-' + kind, 'name': kind,
                                   'cwd': str(root), 'busy': True, 'updated_at': stamp,
                                   'messages': [
                                       {'id': name + kind + '-user', 'role': 'user', 'content': prompt, 'created_at': started - 1.65},
                                       {'id': name + kind + '-echo', 'role': 'assistant', 'content': first, 'created_at': stamp}]}
                        device.request('POST', '/api/device/poll', {
                            'active': [{'id': job['id'], 'session_id': session['session_id'], 'output': first + '\n'}],
                            'sessions': [session]})
                        agents.append((agent, job, session))
                    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser = engine.launch(**({'executable_path': str(chrome)} if name == 'Chromium' and chrome.exists() else {}))
                    page = browser.new_page(viewport={'width': 390, 'height': 700})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base)
                    server.browser_login(page)
                    for agent, job, session in agents:
                        page.set_viewport_size({'width': 1100 if session['kind'] == 'codex' else 390, 'height': 700})
                        page.locator('[data-agent="' + agent['id'] + '"]').evaluate('(el)=>el.click()')
                        expect(page.locator('#messages')).to_contain_text('first streamed update')
                        expect(page.locator('#messages .user')).to_have_count(1)
                        queued = client.request('POST', '/api/jobs', {'kind': 'prompt', 'agent': agent['id'], 'prompt': 'queued next question'})
                        latest = 'native reply missing from task stream: ' + name + session['kind']
                        stamp = time.time()
                        session.update(updated_at=stamp, messages=[{
                            'id': name + session['kind'] + '-latest', 'role': 'assistant',
                            'content': latest, 'created_at': stamp}])
                        device.request('POST', '/api/device/poll', {'sessions': [session]})
                        row = page.locator('[data-agent="' + agent['id'] + '"] .session-preview')
                        expect(row).to_have_text(latest, timeout=15000)
                        expect(page.locator('#messages')).to_contain_text(latest, timeout=15000)
                        expect(page.locator('#queue-hint')).to_contain_text('1 条消息等待执行')
                        reply = client.request('GET', '/api/agents/' + agent['id'] + '/messages')
                        native_id = next(m['id'] for m in reply['messages'] if m['content'] == latest)
                        expect(page.locator('#messages [data-id="' + native_id + '"]')).to_have_count(1)

                        # Once the task stream catches up, remove the cached native copy.
                        output = first + '\n' + latest + '\n'
                        device.request('POST', '/api/device/poll', {'active': [{'id': job['id'], 'output': output}]})
                        expect(page.locator('#messages [data-id="' + native_id + '"]')).to_have_count(0, timeout=15000)
                        expect(page.locator('#messages .assistant .message-body').filter(has_text=latest)).to_have_count(1)
                        task = page.locator('#messages [data-id="' + job['id'] + ':assistant"]')
                        expect(task.locator('.message-part')).to_have_count(2)
                        expect(task.locator('.message-byline')).to_have_count(2)
                        expect(task.locator('.message-part').first.locator('p')).to_have_count(2)
                        expect(task.locator('.message-part').first.locator('pre code')).to_have_text('line one\nline two\n')
                        gap = task.locator('.message-part').evaluate_all('(nodes)=>nodes[1].getBoundingClientRect().top-nodes[0].getBoundingClientRect().bottom')
                        assert gap >= 29, gap

                        # A not-yet-mirrored message streams into its own final block.
                        for partial in ['Partial follow-up', 'Partial follow-up complete']:
                            device.request('POST', '/api/device/poll', {'active': [{'id': job['id'], 'output': output + partial}]})
                            expect(task.locator('.message-part')).to_have_count(3, timeout=15000)
                            expect(task.locator('.message-part').last.locator('.message-body')).to_have_text(partial)
                        output += 'Partial follow-up complete'

                        final = 'final native message while task output is stale: ' + session['kind']
                        stamp = time.time()
                        session.update(updated_at=stamp, messages=[{
                            'id': name + session['kind'] + '-final', 'role': 'assistant',
                            'content': final, 'created_at': stamp}])
                        device.request('POST', '/api/device/poll', {'sessions': [session]})
                        expect(row).to_have_text(final, timeout=15000)
                        expect(page.locator('#messages')).to_contain_text(final, timeout=15000)
                        device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'output': output})
                        expect(page.locator('#stop-generation')).not_to_be_visible(timeout=15000)
                        expect(page.locator('#messages')).to_contain_text(final)
                        client.request('POST', '/api/jobs/' + queued['id'] + '/cancel', {})
                    assert not errors, errors
                    browser.close()
                    print(name + ': Codex/Claude message boundaries, Markdown, partial streaming, native catch-up deduplication and retained final replies passed', flush=True)
        finally:
            server.close()


if __name__ == '__main__':
    main()
