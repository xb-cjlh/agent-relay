"""Browser/API approval round trip with isolated Java/MySQL and device fixtures."""
import sys
import tempfile
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
        try:
            devices = {}
            for name, capabilities in [('new', {'writable': True, 'codex_permissions': True, 'claude_permissions': True}),
                                       ('old', {'writable': True}),
                                       ('readonly', {'writable': False, 'codex_permissions': True})]:
                devices[name] = client.request('POST', '/api/register', {
                    'name': name, 'platform': 'fixture', 'roots': [str(root)],
                    'capabilities': {'unlimited_tasks': True, 'codex': True, 'claude': True, **capabilities}})
            agents = {}
            for name, device_name, kind in [('codex', 'new', 'codex'), ('claude', 'new', 'claude'),
                                             ('old', 'old', 'codex'), ('readonly', 'readonly', 'codex')]:
                agents[name] = client.request('POST', '/api/agents', {
                    'device': devices[device_name]['id'], 'name': name, 'kind': kind, 'cwd': str(root)})
            device = Client(base, devices['new']['token'])
            with sync_playwright() as playwright:
                chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                browser = playwright.chromium.launch(**({'executable_path': str(chrome)} if chrome.is_file() else {}))
                context = browser.new_context()
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                server.browser_login(page)
                # Eligible sessions default to full access and submit that mode.
                for kind in ('codex', 'claude'):
                    page.locator('[data-agent="' + agents[kind]['id'] + '"]').click()
                    expect(page.locator('#permission-mode')).to_have_value('full')
                    page.locator('#prompt').fill(kind + ' default full fixture')
                    page.locator('#send-button').click()
                    expect(page.locator('#prompt')).to_be_empty()
                    default_job = next(j for j in client.request('GET', '/api/state')['jobs'] if j['agent_id'] == agents[kind]['id'])
                    assert default_job['payload']['permission_mode'] == 'full'
                    device.request('POST', '/api/device/poll', {'capacity': 1})
                    device.request('POST', '/api/device/jobs/' + default_job['id'] + '/complete', {'status': 'succeeded', 'output': 'default full fixture done'})
                page.locator('[data-agent="' + agents['codex']['id'] + '"]').click()
                expect(page.locator('#permission-mode')).to_be_visible()
                page.locator('#permission-mode').select_option('ask')
                page.locator('#prompt').fill('approval fixture')
                page.locator('#send-button').click()
                expect(page.locator('#prompt')).to_be_empty()
                job = next(j for j in client.request('GET', '/api/state')['jobs'] if j['payload'].get('prompt') == 'approval fixture')
                assert job['payload']['permission_mode'] == 'ask'
                assert job['payload']['timeout'] == 0
                device.request('POST', '/api/device/poll', {'capacity': 1})
                path = '/api/device/jobs/' + job['id'] + '/approvals'
                aid = 'apr_' + 'f' * 48
                command = 'echo <img src=x onerror="window.approvalInjected=true">'
                device.request('POST', path, {'id': aid, 'method': 'item/commandExecution/requestApproval',
                    'details': {'command': command, 'cwd': str(root), 'reason': 'fixture extra access'}})
                expect(page.locator('#approval-list')).to_contain_text(command)
                expect(page.locator('#conversation-status')).to_contain_text('等待审批')
                assert not page.evaluate('window.approvalInjected || false')
                assert page.locator('#approval-list img').count() == 0
                page.reload()
                expect(page.locator('#approval-list')).to_be_visible()
                expect(page.locator('#permission-mode')).to_have_value('ask')
                page.locator('[data-decision="accept"]').click()
                expect(page.locator('#approval-list')).not_to_be_visible()
                assert device.request('GET', path + '/' + aid)['decision'] == 'accept'

                aid = 'apr_' + 'b' * 48
                device.request('POST', path, {'id': aid, 'method': 'item/fileChange/requestApproval',
                    'details': {'reason': 'fixture change', 'item': {'changes': [{'path': '/fixture.txt', 'diff': '+hello'}]}}})
                expect(page.locator('#approval-list')).to_contain_text('/fixture.txt')
                page.locator('[data-agent="' + agents['claude']['id'] + '"]').click()
                expect(page.locator('#approval-list')).not_to_be_visible()
                expect(page.locator('#permission-mode')).to_be_visible()
                expect(page.locator('#permission-mode')).to_have_value('full')
                page.locator('#permission-mode').select_option('device')
                page.reload()
                expect(page.locator('#permission-mode')).to_have_value('device')
                page.locator('[data-agent="' + agents['codex']['id'] + '"]').click()
                expect(page.locator('#permission-mode')).to_have_value('ask')
                expect(page.locator('#approval-list')).to_be_visible()
                page.locator('[data-decision="decline"]').click()
                expect(page.locator('#approval-list')).not_to_be_visible()
                assert device.request('GET', path + '/' + aid)['decision'] == 'decline'
                diagnostic = 'OutputTextDelta without active item <img src=x onerror="window.diagnosticInjected=true">'
                device.request('POST', '/api/device/jobs/' + job['id'] + '/complete', {'status': 'succeeded', 'output': 'fixture done', 'diagnostics': diagnostic})
                expect(page.locator('#messages .execution-details summary')).to_have_text('执行日志')
                expect(page.locator('#messages .execution-details pre')).not_to_be_visible()
                assert 'OutputTextDelta' not in page.locator('#messages .message-body').all_text_contents()[-1]
                page.locator('#messages .execution-details summary').click()
                expect(page.locator('#messages .execution-details pre')).to_have_text(diagnostic)
                assert page.locator('#messages .execution-details img').count() == 0
                assert not page.evaluate('window.diagnosticInjected || false')

                page.locator('[data-agent="' + agents['claude']['id'] + '"]').click()
                page.locator('#permission-mode').select_option('ask')
                page.locator('#prompt').fill('Claude approval fixture')
                page.locator('#send-button').click()
                expect(page.locator('#prompt')).to_be_empty()
                claude_job = next(j for j in client.request('GET', '/api/state')['jobs'] if j['payload'].get('prompt') == 'Claude approval fixture')
                assert claude_job['payload']['permission_mode'] == 'ask'
                device.request('POST', '/api/device/poll', {'capacity': 1})
                claude_path = '/api/device/jobs/' + claude_job['id'] + '/approvals'
                aid = 'apr_' + 'c' * 48
                device.request('POST', claude_path, {'id': aid, 'method': 'claude/tool/requestApproval',
                    'details': {'tool_name': 'Write', 'input': {'file_path': '/claude-fixture.txt', 'content': '<fixture>'}}})
                expect(page.locator('#approval-list')).to_contain_text('允许 Claude 使用 Write')
                expect(page.locator('#approval-list')).to_contain_text('/claude-fixture.txt')
                page.locator('[data-decision="accept"]').click()
                expect(page.locator('#approval-list')).not_to_be_visible()
                assert device.request('GET', claude_path + '/' + aid)['decision'] == 'accept'
                device.request('POST', '/api/device/jobs/' + claude_job['id'] + '/complete', {'status': 'succeeded', 'output': 'Claude fixture done'})
                page.locator('#permission-mode').select_option('full')
                page.locator('#prompt').fill('Claude full access fixture')
                page.locator('#send-button').click()
                expect(page.locator('#prompt')).to_be_empty()
                claude_full = next(j for j in client.request('GET', '/api/state')['jobs'] if j['payload'].get('prompt') == 'Claude full access fixture')
                assert claude_full['payload']['permission_mode'] == 'full'
                page.locator('[data-agent="' + agents['codex']['id'] + '"]').click()

                page.locator('#permission-mode').select_option('full')
                expect(page.locator('#permission-note')).to_contain_text('不再逐项审批')
                page.locator('#prompt').fill('full access fixture')
                page.locator('#send-button').click()
                expect(page.locator('#prompt')).to_be_empty()
                full = next(j for j in client.request('GET', '/api/state')['jobs'] if j['payload'].get('prompt') == 'full access fixture')
                assert full['payload']['permission_mode'] == 'full'
                # A later selection must not alter a previously submitted job.
                page.locator('#permission-mode').select_option('ask')
                assert client.request('GET', '/api/jobs/' + full['id'])['payload']['permission_mode'] == 'full'
                for name in ('old', 'readonly'):
                    page.locator('[data-agent="' + agents[name]['id'] + '"]').click()
                    expect(page.locator('#permission-mode')).to_have_value('device')
                    assert page.locator('#permission-mode option[value=ask]').is_disabled()
                    assert page.locator('#permission-mode option[value=full]').is_disabled()
                assert not errors, errors
                state = context.storage_state()
                browser.close()
                webkit = playwright.webkit.launch()
                mobile = webkit.new_context(viewport={'width': 390, 'height': 430}, is_mobile=True, has_touch=True, storage_state=state)
                safari = mobile.new_page()
                safari.goto(base)
                safari.locator('[data-agent="' + agents['codex']['id'] + '"]').evaluate('(button)=>button.click()')
                expect(safari.locator('#permission-mode')).to_be_visible()
                expect(safari.locator('#permission-mode')).to_have_value('full')
                assert safari.locator('#permission-mode').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)') >= 16
                expect(safari.locator('#send-button')).to_be_in_viewport()
                webkit.close()
            print('Codex and Claude browser permissions passed: mode snapshots, allow/deny, native tool details, refresh, conversation isolation, escaped previews, old/read-only devices and mobile WebKit.')
        finally:
            server.close()


if __name__ == '__main__':
    main()
