"""Markdown file links through actual Go daemons, without any model requests."""
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, Server, binary
from test_relay import TOKEN
from playwright.sync_api import expect, sync_playwright


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        server = Server(('127.0.0.1', 0), root / 'data', TOKEN)
        base = 'http://127.0.0.1:' + str(server.server_port)
        client = Client(base, TOKEN)
        daemons = []
        try:
            # Both devices expose identical relative filenames under different cwd.
            # The device and path in each read job must match the clicked session.
            agents = {}
            projects = {}
            for name, kind in [('work', 'codex'), ('home', 'claude')]:
                project = root / name; (project / 'docs').mkdir(parents=True)
                (project / 'docs' / 'note 中文.md').write_text('# ' + name + ' 文件\n\n**正文**\n\n[代码](../main.py#L3)\n\n[百分号](../100%25.txt)\n\n[章节](#章节)\n\n## 章节\n\n<script>window.fileInjected=true</script>\n')
                (project / 'main.py').write_text('first\nsecond\n' + name + ' third\nfourth\n')
                (project / '100%.txt').write_text(name + ' percent')
                (project / 'plain.html').write_text('<script>window.fileInjected=true</script>\n<h1>literal HTML</h1>')
                (project / 'binary.bin').write_bytes(bytes(range(256)))
                (project / 'large.txt').write_text('Long line\n' * 40000)
                (project / 'image.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII='))
                codex_home = root / (name + '-codex'); codex_home.mkdir()
                claude_home = root / (name + '-claude'); claude_home.mkdir()
                env = dict(os.environ, AM_CONFIG_DIR=str(root / (name + '-config')), CODEX_HOME=str(codex_home), CLAUDE_CONFIG_DIR=str(claude_home))
                daemon = subprocess.Popen([binary(), '--server', base, '--token', TOKEN, 'daemon', 'run', '--virtual-device',
                    '--name', name, '--root', str(root), '--state-dir', str(root / (name + '-state')), '--interval', '0.1'],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                daemons.append(daemon)
                end = time.monotonic() + 20
                while not (root / (name + '-state') / 'identity.json').exists():
                    assert time.monotonic() < end, 'daemon registration timed out'
                    time.sleep(0.1)
                identity = json.loads((root / (name + '-state') / 'identity.json').read_text())
                device = Client(base, identity['token'])
                markdown = '\n\n'.join([
                    '[说明](docs/note%20%E4%B8%AD%E6%96%87.md)',
                    '[绝对路径](' + quote(str(project / 'main.py')) + ':3)',
                    '[文件 URI](file://' + quote(str(project / 'main.py')) + '#L2)',
                    '[图片](image.png)', '[二进制](binary.bin)', '[大文本](large.txt)',
                    '[HTML](plain.html)', '[缺失](missing.txt)',
                    '[网页](https://example.test/docs/readme.md)',
                    '[恶意](javascript:alert(1))', '[恶意 URI](file://other-host/etc/passwd)',
                    '[编码协议](javascript%3Aalert(1))'])
                device.request('POST', '/api/device/poll', {'sessions': [{'kind': kind, 'session_id': name + '-session',
                    'name': name, 'cwd': str(project), 'updated_at': time.time(), 'busy': False,
                    'messages': [{'id': name + '-reply', 'role': 'assistant', 'content': markdown, 'created_at': time.time()}]}]})
                agents[name] = next(a for a in client.request('GET', '/api/state')['agents'] if a['device_id'] == identity['id'])
                projects[name] = project

            with sync_playwright() as p:
                for browser_name, engine in [('Chromium', p.chromium), ('WebKit', p.webkit)]:
                    if os.environ.get('RELAY_TEST_BROWSER') and browser_name != os.environ['RELAY_TEST_BROWSER']: continue
                    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                    browser = engine.launch(**({'executable_path': str(chrome)} if browser_name == 'Chromium' and chrome.exists() else {}))
                    context = browser.new_context(accept_downloads=True, viewport={'width': 1440, 'height': 900} if browser_name == 'Chromium' else {'width': 390, 'height': 650}, **({'is_mobile': True, 'has_touch': True} if browser_name == 'WebKit' else {}))
                    page = context.new_page(); errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    reads = []; artifact_reads = []
                    page.on('request', lambda request: reads.append(request.post_data_json) if request.method == 'POST' and request.url == base + '/api/jobs' else None)
                    page.on('request', lambda request: artifact_reads.append(request.url) if request.method == 'GET' and request.url.startswith(base + '/api/files/file_') and not request.url.endswith('/url') else None)
                    page.goto(base); server.browser_login(page)

                    def click_link(label):
                        page.locator('#messages a').filter(has_text=label).evaluate('(el)=>el.click()')

                    print(browser_name + ': testing previews', flush=True)
                    for name, agent in agents.items():
                        page.evaluate('id=>selectAgent(id)', agent['id'])
                        expect(page.locator('#messages a[data-file-link]')).to_have_count(8)
                        before = len(reads)
                        page.wait_for_timeout(1100)
                        assert len(reads) == before, 'file links must not read files before a click'
                        external = page.locator('#messages a').filter(has_text='网页')
                        expect(external).to_have_attribute('href', 'https://example.test/docs/readme.md')
                        expect(external).to_have_attribute('target', '_blank')
                        click_link('说明')
                        expect(page.locator('#file-preview-content h1')).to_have_text(name + ' 文件', timeout=15000)
                        expect(page.locator('#file-preview-content strong')).to_have_text('正文')
                        assert reads[-1]['device'] == agent['device_id']
                        assert reads[-1]['path'] == str(projects[name] / 'docs' / 'note 中文.md')
                        assert all(r['kind'] == 'file_read' for r in reads)
                        assert not page.evaluate('window.fileInjected || false')
                        assert page.locator('#file-preview-content script, #file-preview-content iframe').count() == 0
                        if browser_name == 'Chromium':
                            assert page.locator('#file-preview').bounding_box()['x'] > page.locator('.conversation').bounding_box()['x']
                        else:
                            assert page.locator('#file-preview').bounding_box()['width'] == 390
                            assert page.locator('.conversation').evaluate('(el)=>el.inert')
                        if name == 'work':
                            # Full-page preview works in desktop Chrome and WebKit, including resize.
                            if browser_name == 'WebKit': page.set_viewport_size({'width': 1200, 'height': 850})
                            before_fullscreen = (len(reads),len(artifact_reads))
                            page.locator('#file-preview-content').evaluate('(el)=>el.scrollTop=40')
                            scroll = page.locator('#file-preview-content').evaluate('(el)=>el.scrollTop')
                            page.locator('#fullscreen-file-preview').click()
                            expect(page.locator('#fullscreen-file-preview')).to_have_attribute('aria-pressed','true')
                            assert page.locator('#file-preview').bounding_box()['width'] == page.viewport_size['width']
                            assert page.locator('#file-preview').bounding_box()['x'] == 0
                            assert page.locator('.conversation').evaluate('(el)=>el.inert')
                            assert page.locator('#sidebar').evaluate('(el)=>el.inert')
                            assert page.locator('#file-preview-content').evaluate('(el)=>el.scrollTop') == scroll
                            assert (len(reads),len(artifact_reads)) == before_fullscreen, 'fullscreen must reuse the current file'
                            expect(page.locator('#download-file-preview')).to_be_in_viewport()
                            with page.expect_download() as full_download:
                                page.locator('#download-file-preview').click()
                            assert Path(full_download.value.path()).read_bytes() == (projects[name] / 'docs' / 'note 中文.md').read_bytes()
                            # Nested file links retain full-page mode and use the document's directory.
                            page.locator('#file-preview-content a').filter(has_text='代码').click()
                            expect(page.locator('#file-preview-content .highlighted')).to_have_text(name + ' third',timeout=15000)
                            assert reads[-1]['path'] == str(projects[name] / 'main.py')
                            expect(page.locator('#fullscreen-file-preview')).to_have_attribute('aria-pressed','true')
                            page.keyboard.press('Escape')
                            expect(page.locator('#fullscreen-file-preview')).to_have_attribute('aria-pressed','false')
                            expect(page.locator('#file-preview')).to_be_visible()
                            assert not page.locator('.conversation').evaluate('(el)=>el.inert')
                            page.locator('#fullscreen-file-preview').click()
                            page.locator('#close-file-preview').click()
                            assert not page.locator('#sidebar').evaluate('(el)=>el.inert')
                            click_link('说明')
                            expect(page.locator('#file-preview-content h1')).to_have_text(name + ' 文件')
                            expect(page.locator('#fullscreen-file-preview')).to_have_attribute('aria-pressed','false')
                            page.locator('#fullscreen-file-preview').click()
                            page.set_viewport_size({'width':390,'height':650})
                            assert page.locator('#file-preview').bounding_box()['width'] == 390
                            page.keyboard.press('Escape')
                            assert page.locator('.conversation').evaluate('(el)=>el.inert')
                            page.keyboard.press('Escape')
                            expect(page.locator('#file-preview')).to_be_hidden()
                            assert not page.locator('.conversation').evaluate('(el)=>el.inert')
                            page.set_viewport_size({'width':1440,'height':900} if browser_name=='Chromium' else {'width':390,'height':650})
                            click_link('说明')
                            expect(page.locator('#file-preview-content h1')).to_have_text(name + ' 文件')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        with page.expect_download() as info:
                            page.locator('#download-file-preview').click()
                        assert info.value.suggested_filename == 'note 中文.md'
                        assert Path(info.value.path()).read_bytes() == (projects[name] / 'docs' / 'note 中文.md').read_bytes()
                        # A heading anchor in the preview reuses the same retrieved bytes.
                        before = len(reads)
                        page.locator('#file-preview-content a').filter(has_text='章节').click()
                        assert len(reads) == before
                        page.locator('#file-preview-content a').filter(has_text='百分号').click()
                        expect(page.locator('#file-preview-content')).to_contain_text(name + ' percent', timeout=15000)
                        page.locator('#close-file-preview').click()
                        before = (len(reads),len(artifact_reads))
                        click_link('说明')
                        expect(page.locator('#file-preview-content h1')).to_have_text(name + ' 文件', timeout=15000)
                        assert len(artifact_reads) == before[1], 'reopening a document must reuse verified bytes'
                        expect(page.locator('#file-preview-info')).to_contain_text('已缓存')
                        page.locator('#file-preview-content a').filter(has_text='代码').click()
                        expect(page.locator('#file-preview-content .highlighted')).to_have_text(name + ' third', timeout=15000)
                        assert page.evaluate('filePreview.path') == str(projects[name] / 'main.py')
                        page.locator('#close-file-preview').click()
                        expect(page.locator('#file-preview')).to_be_hidden()
                        assert not page.locator('.conversation').evaluate('(el)=>el.inert')
                        before = (len(reads),len(artifact_reads))
                        click_link('文件 URI')
                        expect(page.locator('.file-source-line.highlighted')).to_have_text('second', timeout=15000)
                        assert len(artifact_reads) == before[1], 'line anchors must share a cached file'
                        page.locator('#close-file-preview').click()
                        click_link('绝对路径')
                        expect(page.locator('.file-source-line.highlighted')).to_have_text(name + ' third', timeout=15000)
                        assert len(artifact_reads) == before[1]
                        page.locator('#close-file-preview').click()

                    # Cache survives reload; a change while viewing updates without a button.
                    note = projects['home'] / 'docs' / 'note 中文.md'
                    original = note.read_bytes()
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('home 文件')
                    expect(page.locator('#refresh-file-preview')).to_have_count(0)
                    before = len(artifact_reads)
                    page.reload()
                    expect(page.locator('#workspace')).to_be_visible()
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('home 文件')
                    assert len(artifact_reads) == before, 'reload must reuse IndexedDB bytes'
                    note.write_text('# updated document\n\nnew content\n')
                    expect(page.locator('#file-preview-content h1')).to_have_text('updated document',timeout=22000)
                    assert len(artifact_reads) == before+1
                    with page.expect_download() as info:
                        page.locator('#download-file-preview').click()
                    assert Path(info.value.path()).read_bytes() == note.read_bytes()
                    # A conditional check returns the same artifact and uploads no contents.
                    artifact = page.evaluate('filePreview.file.id')
                    before = len(artifact_reads)
                    page.evaluate('fetchFilePreview(filePreview)')
                    expect(page.locator('#file-preview-info')).to_contain_text('已是最新')
                    assert page.evaluate('filePreview.file.id') == artifact
                    assert len(artifact_reads) == before
                    note.unlink()
                    expect(page.locator('#file-preview-info')).to_have_text('暂时无法检查，显示缓存',timeout=22000)
                    expect(page.locator('#file-preview-content h1')).to_have_text('updated document')
                    expect(page.locator('#download-file-preview')).to_be_enabled()
                    note.write_bytes(original)
                    page.locator('#close-file-preview').click()
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('home 文件',timeout=22000)
                    page.locator('#close-file-preview').click()
                    # Expiration applies to persistent storage as well as memory.
                    page.evaluate('filePreviewCache.clear()')
                    page.evaluate("async()=>{await fileCacheOperation(db=>fileCacheTransaction(db,'readwrite',store=>{const r=store.getAll();r.onsuccess=()=>{for(const entry of r.result){entry.checkedAt-=filePreviewCacheTTL;store.put(entry);}};}));}")
                    before = len(artifact_reads)
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('home 文件',timeout=15000)
                    assert len(artifact_reads) == before+1, 'expired snapshots need fresh verified bytes'
                    page.locator('#close-file-preview').click()
                    page.evaluate('id=>selectAgent(id)',agents['work']['id'])
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('work 文件',timeout=15000)
                    page.evaluate('id=>selectAgent(id)',agents['home']['id'])
                    before = len(artifact_reads)
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('home 文件')
                    assert len(artifact_reads) == before, 'switching sessions must retain cached files'
                    page.locator('#close-file-preview').click()

                    click_link('图片')
                    expect(page.locator('#file-preview-content img')).not_to_have_js_property('naturalWidth', 0, timeout=15000)
                    page.locator('#close-file-preview').click()
                    click_link('二进制')
                    expect(page.locator('#file-preview-content')).to_contain_text('此格式暂不支持预览', timeout=15000)
                    with page.expect_download() as info:
                        page.locator('#download-file-preview').click()
                    assert Path(info.value.path()).read_bytes() == bytes(range(256))
                    page.locator('#close-file-preview').click()
                    click_link('大文本')
                    expect(page.locator('.file-preview-notice')).to_contain_text('仅预览', timeout=15000)
                    expect(page.locator('.file-source-line')).to_have_count(5000)
                    with page.expect_download() as info:
                        page.locator('#download-file-preview').click()
                    assert Path(info.value.path()).read_bytes() == (projects['home'] / 'large.txt').read_bytes()
                    page.locator('#close-file-preview').click()
                    click_link('HTML')
                    expect(page.locator('#file-preview-content pre')).to_contain_text('<script>window.fileInjected=true</script>', timeout=15000)
                    assert not page.evaluate('window.fileInjected || false')
                    page.locator('#close-file-preview').click()
                    click_link('缺失')
                    expect(page.locator('#retry-file-preview')).to_be_visible(timeout=15000)
                    expect(page.locator('#download-file-preview')).to_be_disabled()
                    (projects['home'] / 'missing.txt').write_text('created after first attempt')
                    page.locator('#retry-file-preview').click()
                    expect(page.locator('#file-preview-content')).to_contain_text('created after first attempt', timeout=15000)
                    (projects['home'] / 'missing.txt').unlink()
                    page.evaluate('id=>selectAgent(id)', agents['work']['id'])
                    expect(page.locator('#file-preview')).to_be_hidden()

                    # A slow old response cannot reopen a preview on another session.
                    page.evaluate('clearFilePreviewCache()')
                    held = []
                    context.route(base + '/api/files/*', lambda route: held.append(route))
                    click_link('说明')
                    end = time.monotonic() + 15
                    while not held:
                        assert time.monotonic() < end
                        page.wait_for_timeout(100)
                    page.evaluate('id=>selectAgent(id)', agents['home']['id'])
                    held[0].continue_()
                    page.wait_for_timeout(300)
                    expect(page.locator('#file-preview')).to_be_hidden()
                    context.unroute(base + '/api/files/*')

                    cases = [
                        ['~/notes/a.md#L7', {'base': '/work'}, '~/notes/a.md', 7],
                        ['../a%2520b.md', {'base': '/work/docs'}, '/work/a%20b.md', 0],
                        ['#part', {'base': '/work', 'path': '/work/100%.md'}, '/work/100%.md', 0],
                    ]
                    for href, scope, path, line in cases:
                        parsed = page.evaluate('([href,scope])=>localFileLink(href,scope)', [href, scope])
                        assert (parsed['path'], parsed['line']) == (path, line)
                    for href in ['javascript:alert(1)', 'data:text/html,foo', '//other/file', 'file://other/file', 'file:///tmp/a%00b', '%2F%2Fhost/file']:
                        assert page.evaluate('href=>localFileLink(href,{base:"/work"})', href) is None

                    print(browser_name + ': downloads, errors and source-device checks passed', flush=True)
                    # Closing a queued request retracts it, even on an offline device.
                    offline = client.request('POST', '/api/register', {'name': browser_name + '-offline', 'platform': 'test', 'capabilities': {}})
                    server.database.execute('UPDATE devices SET last_seen=0 WHERE id=?', (offline['id'],))
                    page.evaluate('refresh()')
                    page.evaluate("id=>{void openFilePreview({deviceId:id,path:'/offline.txt'},null);}", offline['id'])
                    expect(page.locator('#file-preview-info')).to_contain_text('设备离线', timeout=15000)
                    job_id = page.evaluate('filePreview.job.id')
                    page.locator('#close-file-preview').click()
                    end = time.monotonic() + 5
                    while client.request('GET', '/api/jobs/' + job_id)['status'] != 'cancelled':
                        assert time.monotonic() < end
                        page.wait_for_timeout(100)
                    assert not errors, errors
                    # Signing out clears private file bytes before another account can sign in.
                    click_link('说明')
                    expect(page.locator('#file-preview-content h1')).to_have_text('home 文件',timeout=15000)
                    assert page.evaluate('filePreviewCache.size') > 0
                    page.evaluate('logout(true)')
                    assert page.evaluate('filePreviewCache.size') == 0
                    page.evaluate('fileCacheOperations')
                    assert page.evaluate("async()=>fileCacheOperation(db=>fileCacheTransaction(db,'readonly',(store,result)=>{const r=store.count();r.onsuccess=()=>result(r.result);}))") == 0
                    expect(page.locator('#file-preview')).to_be_hidden()
                    context.close()
                    browser.close()
            print('File previews passed: persistent cached revisits without artifact requests, automatic change detection, unchanged responses, offline snapshots, expiration, session/device isolation, logout cleanup, actual Go reads, downloads, Markdown/text/image/binary/large files, retries, cancellation and mobile WebKit; no model requests.')
        finally:
            for daemon in daemons:
                daemon.send_signal(signal.SIGINT)
            for daemon in daemons:
                try: daemon.wait(timeout=10)
                except subprocess.TimeoutExpired: daemon.kill(); daemon.wait()
                daemon.stderr.close()
            server.close()


if __name__ == '__main__':
    main()
