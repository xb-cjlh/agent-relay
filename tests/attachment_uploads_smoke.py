"""Mixed arbitrary attachments through the real browser, Java API, Go daemon and OSS."""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Server, Client, binary
from auth_support import Mailbox
from oss_support import OssFixture
from test_accounts import PNG
from test_relay import TOKEN, FAKE_AGENT
from test_sessions import codex_records, write_records
from playwright.sync_api import sync_playwright, expect

FILE_FIXTURE = r'''
if "[Relay attachments]" in prompt:
 original, manifest = prompt.split("\n\n[Relay attachments]\n", 1)
 refs = json.loads(manifest.split("\n[/Relay attachments]", 1)[0])
 for ref in refs:
  data = pathlib.Path(ref["path"]).read_bytes()
  assert data == (b"" if ref["name"] == "empty.txt" else b"%PDF-1.7\x00file fixture")
 pathlib.Path("attachment-paths.json").write_text(json.dumps([ref["path"] for ref in refs]))
 prompt = "FILES:%d " % len(refs) + original
'''


def main():
 with tempfile.TemporaryDirectory() as temporary:
  root = Path(temporary); mail = Mailbox(); oss = OssFixture()
  server = Server(('127.0.0.1', 0), root/'data', TOKEN, environment={**mail.environment(), **oss.environment()})
  url = 'http://127.0.0.1:'+str(server.server_port); daemon = None
  try:
   cwd = root/'project'; cwd.mkdir(); bindir = root/'bin'; bindir.mkdir()
   for kind in ('codex', 'claude'):
    cli = bindir/kind; cli.write_text('#!'+sys.executable+'\n'+FAKE_AGENT.replace('if image_count:', FILE_FIXTURE+'\nif image_count:', 1)); cli.chmod(0o755)
   codex = root/'codex'; claude = root/'claude'
   write_records(codex/'sessions/fixture.jsonl', codex_records(cwd))
   write_records(claude/'projects/fixture/session.jsonl', [{'type':'user','sessionId':'claude-files','cwd':str(cwd),'uuid':'fixture','message':{'content':'files conversation'}}, {'type':'assistant','sessionId':'claude-files','cwd':str(cwd),'uuid':'ready','message':{'content':'ready','stop_reason':'end_turn'}}])
   with sync_playwright() as p:
    for engine, mobile in ((p.chromium, False), (p.webkit, True)):
     chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
     browser = engine.launch(**({'executable_path':str(chrome)} if not mobile and chrome.exists() else {}))
     context = browser.new_context(**(p.devices['iPhone 13'] if mobile else {})); page = context.new_page(); errors = []
     page.on('pageerror', lambda error: errors.append(str(error)))
     puts = []; posts = []
     page.on('request', lambda r: puts.append(r) if r.method == 'PUT' else posts.append(r) if r.method == 'POST' and '/api/attachments' in r.url else None)
     page.goto(url)
     if not mobile:
      page.locator('[data-auth-mode=register]').click(); page.locator('#auth-email').fill('files@example.com'); page.locator('#auth-password').fill('Relay-password-2026'); page.locator('#auth-name').fill('Files')
      page.locator('#send-code').click(); expect(page.locator('#auth-notice')).to_contain_text('验证码')
      page.locator('#auth-code').fill(mail.code('files@example.com')); page.locator('#auth-submit').click(); expect(page.locator('#workspace')).to_be_visible()
      page.locator('#account-button').click(); page.locator('#key-name').fill('file test'); page.locator('#key-form button[type=submit]').click(); expect(page.locator('#new-key-secret')).to_contain_text('sk-relay_')
      secret = page.locator('#new-key-secret').inner_text(); page.locator('[data-close=account-dialog]').click()
      env = dict(os.environ, PATH=str(bindir)+os.pathsep+os.environ.get('PATH',''), CODEX_HOME=str(codex), CLAUDE_CONFIG_DIR=str(claude), AM_CONFIG_DIR=str(root/'config'))
      command = [binary(), '--server', url, '--token', secret, 'daemon', 'run', '--name', 'files-device', '--state-dir', str(root/'device'), '--interval', '0.1']
      daemon = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
      client = Client(url, secret); deadline = time.monotonic()+15
      while len(client.request('GET', '/api/state')['agents']) != 2:
       assert time.monotonic() < deadline; time.sleep(.1)
      agents = client.request('GET', '/api/state')['agents']
     else:
      page.locator('#auth-email').fill('files@example.com'); page.locator('#auth-password').fill('Relay-password-2026'); page.locator('#auth-submit').click(); expect(page.locator('#workspace')).to_be_visible()
     expect(page.locator('#session-list [data-agent]')).to_have_count(2)
     for kind in ('codex', 'claude'):
      agent = next(a for a in agents if a['kind'] == kind)
      if mobile:page.locator('#open-sidebar').click()
      page.locator('[data-agent="'+agent['id']+'"]').click()
      assert page.locator('#image-input').get_attribute('accept') is None
      files = [{'name':'中文 report.pdf','mimeType':'application/pdf','buffer':b'%PDF-1.7\x00file fixture'}, {'name':'empty.txt','mimeType':'text/plain','buffer':b''}, {'name':'image.png','mimeType':'image/png','buffer':PNG}]
      page.locator('#image-input').set_input_files(files)
      expect(page.locator('#attachment-list .attachment')).to_have_count(3)
      expect(page.locator('#attachment-list .file-attachment')).to_have_count(2)
      expect(page.locator('#attachment-list img')).to_have_count(1)
      page.locator('#attachment-list .file-attachment').first.locator('button').click()
      expect(page.locator('#attachment-list .attachment')).to_have_count(2)
      page.locator('#image-input').set_input_files(files[:1]); expect(page.locator('#attachment-list .attachment')).to_have_count(3)
      page.locator('#prompt').fill('read '+kind+(' mobile' if mobile else ' desktop'))
      expect(page.locator('#send-button')).to_be_enabled(); page.locator('#send-button').click()
      expect(page.locator('#messages')).to_contain_text('IMAGES:1 FILES:2 read '+kind, timeout=15000)
      expect(page.locator('#attachment-list')).not_to_be_visible()
      user_files = page.locator('#messages .message.user .message-files a')
      expect(user_files).to_have_count(4 if mobile else 2)
      with page.expect_download() as downloaded:user_files.filter(has_text='中文 report.pdf').last.click()
      assert Path(downloaded.value.path()).read_bytes() == b'%PDF-1.7\x00file fixture'
      page.reload(); expect(user_files).to_have_count(4 if mobile else 2)
      # Reconstruct the daemon to verify files survive completion and restart.
      daemon.terminate(); daemon.communicate(timeout=8)
      for path in json.loads((cwd/'attachment-paths.json').read_text()):assert Path(path).exists()
      daemon = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
     assert puts and all(r.url.startswith(oss.url+'/') for r in puts)
     assert all(not any(h in r.headers for h in ('cookie','authorization','x-relay-request')) for r in puts)
     assert all('data' not in r.post_data_json for r in posts)
     assert not errors, errors
     context.close(); browser.close()
   print('PASS: desktop Chromium and mobile WebKit mixed file/image uploads, downloads, both agents, direct OSS, restart persistence')
  finally:
   if daemon and daemon.poll() is None:daemon.terminate(); daemon.communicate(timeout=8)
   server.close(); mail.close(); oss.close()


if __name__ == '__main__':main()
