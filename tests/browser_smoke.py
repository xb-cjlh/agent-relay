"""Optional browser test. Uses fixture CLIs and two actual device daemons."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, Server, binary
from test_relay import FAKE_AGENT, TOKEN, PROJECT
from test_sessions import codex_records, write_records
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shared, remote = root / "local", root / "remote"
        shared.mkdir(); remote.mkdir()
        content = "browser file transfer\n".encode()
        (shared / "XX.txt").write_bytes(content)
        codex_home, claude_home = root / "codex", root / "claude"
        local_transcript = codex_home / "sessions/local.jsonl"
        write_records(local_transcript, codex_records(shared))
        write_records(claude_home / "projects/remote/session.jsonl", [
            {"type": "user", "sessionId": "native-claude-1", "cwd": str(remote), "uuid": "remote-user", "message": {"content": "remote question"}},
            {"type": "assistant", "sessionId": "native-claude-1", "cwd": str(remote), "uuid": "remote-assistant", "message": {"content": [{"type": "text", "text": "remote answer"}], "stop_reason": "end_turn"}},
        ])
        bindir = root / "bin"
        bindir.mkdir()
        for kind in ("codex", "claude"):
            path = bindir / kind
            path.write_text("#!" + sys.executable + "\n" + FAKE_AGENT)
            path.chmod(0o755)
        server = Server(("127.0.0.1", 0), root / "data", TOKEN)
        url = "http://127.0.0.1:" + str(server.server_port)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                   CODEX_HOME=str(codex_home), CLAUDE_CONFIG_DIR=str(claude_home))
        daemons = []
        errors = []
        try:
            for name, folder in (("local", shared), ("remote", remote)):
                daemons.append(subprocess.Popen([binary(), "--server", url, "--token", TOKEN,
                    "daemon", "run", "--name", name, "--root", str(folder), "--state-dir", str(root / (name + "-state")),
                    "--interval", "0.1"] + (["--virtual-device"] if name == "remote" else []), env=env, cwd=str(PROJECT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
            client = Client(url, TOKEN)
            end = time.monotonic() + 15
            while len(client.request("GET", "/api/state")["agents"]) != 3:
                assert time.monotonic() < end, "native sessions were not discovered"
                time.sleep(0.1)
            initial = client.request("GET", "/api/state")
            local_id = next(a["id"] for a in initial["agents"] if a["device_name"] == "local" and a["kind"] == "codex")
            other_project_id = next(a["id"] for a in initial["agents"] if a["device_name"] == "local" and a["kind"] == "claude")
            remote_id = next(a["id"] for a in initial["agents"] if a["device_name"] == "remote")
            local_device = next(d['id'] for d in initial['devices'] if d['name'] == 'local')
            remote_device = next(d['id'] for d in initial['devices'] if d['name'] == 'remote')
            with sync_playwright() as playwright:
                chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                browser = playwright.chromium.launch(**({"executable_path": str(chrome)} if chrome.is_file() else {}))
                context = browser.new_context(viewport={"width":1440,"height":1000}, accept_downloads=True)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.goto(url)
                server.browser_login(page)
                expect(page.locator('#workspace')).to_be_visible()
                expect(page.get_by_label('切换设备')).to_have_value('')
                expect(page.locator('#device-filter option')).to_have_count(3)
                expect(page.locator('#session-list .device-group')).to_have_count(0)
                expect(page.locator('#session-list .session-device')).to_have_count(3)
                expect(page.locator('#session-list .agent-codex')).to_have_text('Codex')
                expect(page.locator('#session-list .agent-claude').first).to_have_text('Claude')
                # Other projects can be resumed without granting directory roots.
                page.locator(f'[data-agent="{other_project_id}"]').click()
                expect(page.locator('#conversation-kind')).to_have_text('Claude Code')
                expect(page.locator('#messages')).to_contain_text('remote answer')
                expect(page.locator('#prompt')).to_be_enabled()
                expect(page.locator('#send-button')).to_be_disabled()
                expect(page.locator('#queue-hint')).not_to_contain_text('未授权')
                with (claude_home / 'projects/remote/session.jsonl').open('a') as source:
                    source.write(json.dumps({'type': 'assistant', 'sessionId': 'native-claude-1', 'cwd': str(remote),
                        'uuid': 'readonly-live', 'message': {'content': 'read-only live update', 'stop_reason': 'end_turn'}}) + '\n')
                expect(page.locator('#messages')).to_contain_text('read-only live update', timeout=15000)
                page.locator(f'[data-agent="{local_id}"]').click()
                expect(page.locator('#conversation-kind')).to_have_text('Codex')
                expect(page.locator('#prompt')).to_be_enabled()
                expect(page.locator('#messages')).to_contain_text('terminal question')
                expect(page.locator('#messages')).to_contain_text('terminal answer')

                # Drafts survive refreshes, switching devices, and reloading the page.
                page.locator('#prompt').fill('a local draft')
                page.wait_for_timeout(1300)
                page.get_by_label('切换设备').select_option(remote_device)
                expect(page.locator('#session-list [data-agent]')).to_have_count(1)
                expect(page.locator('#conversation-meta')).to_contain_text('remote ·')
                page.locator(f'[data-agent="{remote_id}"]').click()
                expect(page.locator('#messages')).to_contain_text('remote answer')
                page.locator('#prompt').fill('a remote draft')
                page.get_by_label('切换设备').select_option(local_device)
                expect(page.locator('#session-list [data-agent]')).to_have_count(2)
                expect(page.locator('#prompt')).to_have_value('a local draft')
                page.locator(f'[data-agent="{local_id}"]').click()
                expect(page.locator('#prompt')).to_have_value('a local draft')
                page.reload()
                expect(page.get_by_label('切换设备')).to_have_value(local_device)
                expect(page.locator('#session-list [data-agent]')).to_have_count(2)
                expect(page.locator('#prompt')).to_have_value('a local draft')
                page.locator('#search').fill(str(shared))
                expect(page.locator('#session-list [data-agent]')).to_have_count(1)
                page.get_by_label('切换设备').select_option(remote_device)
                expect(page.locator('#search')).to_have_value('')
                expect(page.locator('#prompt')).to_have_value('a remote draft')
                page.locator('#new-agent').click()
                expect(page.locator('#agent-device')).to_have_value(remote_device)
                page.locator('[data-close=agent-dialog]').click()
                page.locator('#files-button').click()
                expect(page.locator('#file-device')).to_have_value(remote_device)
                page.locator('[data-close=files-dialog]').click()
                page.get_by_label('切换设备').select_option(local_device)

                # A local native conversation continues through its original session ID.
                page.locator('#prompt').fill('continue local')
                page.locator('#prompt').press('Enter')
                expect(page.locator('#messages')).to_contain_text('RESUMED: continue local', timeout=15000)
                page.locator('#prompt').fill('another local turn')
                page.locator('#send-button').click()
                expect(page.locator('#messages')).to_contain_text('RESUMED: another local turn', timeout=15000)
                expect(page.locator('#messages .message.user')).to_have_count(3)

                # Native writer conflicts show a useful message and collapse raw logs.
                page.locator('#prompt').fill('writer-conflict')
                page.locator('#send-button').click()
                expect(page.locator('#messages .session-conflict')).to_have_text('本次指令未执行：原会话仍由本机 Codex 持有。', timeout=15000)
                details = page.locator('#messages .execution-details')
                expect(details.locator('pre')).not_to_be_visible()
                details.locator('summary').click()
                expect(details.locator('pre')).to_contain_text('already has an active writer')
                expect(page.locator('#messages')).to_contain_text('无需删除会话或锁文件')

                # A message written in the native terminal appears without reloading.
                event = codex_records(shared)[3]
                event['payload']['content'][0]['text'] = 'new message from the terminal'
                with local_transcript.open('a') as source:
                    source.write(json.dumps(event) + '\n')
                expect(page.locator('#messages')).to_contain_text('new message from the terminal', timeout=15000)

                # Reading history is stable while another terminal keeps talking.
                with local_transcript.open('a') as source:
                    for index in range(12):
                        event = codex_records(shared)[3]
                        event['payload']['content'][0]['text'] = 'history %s\n' % index + ('a longer message\n' * 8)
                        source.write(json.dumps(event) + '\n')
                expect(page.locator('#messages')).to_contain_text('history 11', timeout=15000)
                page.locator('#message-scroll').evaluate('(el) => { el.scrollTop = 0; }')
                with local_transcript.open('a') as source:
                    event = codex_records(shared)[3]
                    event['payload']['content'][0]['text'] = 'arrived while reading history'
                    source.write(json.dumps(event) + '\n')
                expect(page.locator('#new-messages')).to_be_visible(timeout=15000)
                assert page.locator('#message-scroll').evaluate('(el) => el.scrollTop') < 5
                page.locator('#new-messages').click()
                expect(page.locator('#new-messages')).not_to_be_visible()

                # Streaming is observable before a task finishes; follow-ups can queue.
                page.locator('#prompt').fill('stream')
                page.locator('#send-button').click()
                expect(page.locator('#messages')).to_contain_text('first live update', timeout=15000)
                expect(page.locator('#stop-generation')).to_be_visible()
                page.locator('#prompt').fill('queued follow-up')
                page.locator('#send-button').click()
                expect(page.locator('#messages')).to_contain_text('RESUMED: queued follow-up', timeout=15000)

                # Remote Claude sessions use exactly the same composer.
                page.get_by_label('切换设备').select_option(remote_device)
                page.locator(f'[data-agent="{remote_id}"]').click()
                expect(page.locator('#prompt')).to_have_value('a remote draft')
                page.locator('#prompt').fill('continue remote')
                page.locator('#send-button').click()
                expect(page.locator('#messages')).to_contain_text('RESUMED: continue remote', timeout=15000)

                # New conversations and file transfers stay accessible without extra pages.
                page.get_by_label('切换设备').select_option(local_device)
                page.locator('#new-agent').click()
                page.locator('#agent-device').select_option(label='local')
                page.locator('#agent-form button[type=submit]').click()
                expect(page.locator('#agent-dialog')).not_to_be_visible()
                expect(page.locator('#thread-empty')).to_be_visible()
                page.locator('#files-button').click()
                page.locator('#file-device').select_option(label='local')
                page.locator('#file-path').fill(os.path.relpath(shared / 'XX.txt', Path.home()))
                page.locator('#file-form button[type=submit]').click()
                expect(page.locator('#file-list [data-download]')).to_be_visible(timeout=15000)
                with page.expect_download() as info:
                    page.locator('#file-list [data-download]').click()
                assert Path(info.value.path()).read_bytes() == content
                page.locator('[data-close=files-dialog]').click()

                # Empty/offline devices remain selectable and never show another device's thread.
                empty = client.request('POST', '/api/register', {'name':'empty-device', 'platform':'test',
                    'capabilities':{'codex':True, 'access_scope':'user', 'home':str(remote)}})
                server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?', (empty['id'],))
                expect(page.locator('#device-filter option')).to_have_count(4)
                page.get_by_label('切换设备').select_option(empty['id'])
                expect(page.locator('#session-list')).to_contain_text('还没有同步到会话')
                expect(page.locator('#empty-description')).to_contain_text('empty-device')
                expect(page.locator('#thread')).not_to_be_visible()
                page.wait_for_timeout(1200)
                expect(page.get_by_label('切换设备')).to_have_value(empty['id'])
                expect(page.locator('#device-filter option:checked')).to_contain_text('离线')
                page.locator('#empty-new-agent').click()
                expect(page.locator('#agent-device')).to_have_value(empty['id'])
                page.locator('[data-close=agent-dialog]').click()
                page.locator('#devices-button').click()
                page.locator(f'[data-view-device="{remote_device}"]').click()
                expect(page.locator('#devices-dialog')).not_to_be_visible()
                expect(page.get_by_label('切换设备')).to_have_value(remote_device)
                expect(page.locator('#messages')).to_contain_text('RESUMED: continue remote')

                # The composer remains on screen on mobile and conversation switching works.
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
                expect(page.locator('#prompt')).to_be_in_viewport()
                page.locator('#open-sidebar').click()
                expect(page.locator('#sidebar')).to_have_class('sidebar open')
                page.get_by_label('切换设备').select_option(local_device)
                expect(page.locator('#sidebar')).to_have_class('sidebar open')
                expect(page.locator('#session-list [data-agent]')).to_have_count(3)
                page.locator(f'[data-agent="{local_id}"]').click()
                expect(page.locator('#sidebar')).to_have_class('sidebar')
                expect(page.locator('#messages')).to_contain_text('terminal question')
                page.locator('#open-sidebar').click()
                page.locator('#logout').click()
                expect(page.locator('#login')).to_be_visible()
                assert not errors, '\n'.join(errors)
                browser.close()
            print('Browser passed: device switching, scoped search, remembered sessions/drafts, empty/offline device, dialog defaults, live messages, streaming, create, download and mobile.')
        finally:
            for daemon in daemons:
                daemon.terminate()
            for daemon in daemons:
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill(); daemon.wait()
                daemon.stderr.close()
            server.close()


if __name__ == '__main__':
    main()
