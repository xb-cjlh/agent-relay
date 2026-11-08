"""Exercise the published installer with actual artifacts and the Java server."""
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from scripts.devtools import Server
import test_relay as fixtures

PROJECT = Path(__file__).resolve().parents[1]


class InstallationCase(unittest.TestCase):
    def setUp(self):
        if not (PROJECT/'dist/downloads/current.json').exists():
            subprocess.run([sys.executable,str(PROJECT/'scripts/build_downloads.py')],cwd=PROJECT,check=True)
        fixtures.ServerCase.setUp(self)
    tearDown = fixtures.ServerCase.tearDown

    def request(self, path, method='GET'):
        with urllib.request.urlopen(urllib.request.Request(self.url + path, method=method)) as response:
            return response.read(), response.headers

    def test_public_documents_downloads_and_real_installation(self):
        for path in ['/docs/', '/docs/install.md', '/install-agent.md', '/install.sh', '/docs.js', '/docs.css', '/skills/agent-bridge/SKILL.md']:
            body, headers = self.request(path)
            self.assertTrue(body, path)
            self.assertIn('no-store', headers['Cache-Control'])
            head, metadata = self.request(path, 'HEAD')
            self.assertEqual(head, b'')
            self.assertEqual(int(metadata['Content-Length']), len(body))
        skill, _ = self.request('/skills/agent-bridge/SKILL.md')
        manifest = json.loads(self.request('/downloads/current.json')[0])
        self.assertEqual(len(manifest['assets']), 4)
        for asset in manifest['assets']:
            content, headers = self.request(asset['url'])
            self.assertEqual(hashlib.sha256(content).hexdigest(), asset['sha256'])
            self.assertEqual(len(content), asset['size'])
            self.assertIn('attachment', headers['Content-Disposition'])
        self.request(manifest['checksums'])
        for path in ['/downloads/current.json.bak', '/downloads/../config.json', '/downloads/0.5.1/../../admin.json', '/docs/nope', '/skills/agent-bridge/../../../admin.json']:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.request(path)
            self.assertIn(failure.exception.code, [400, 404])
        env = dict(os.environ, AM_CONFIG_DIR=str(self.root/'config'), CODEX_HOME=str(self.root/'codex'),
                   CLAUDE_CONFIG_DIR=str(self.root/'custom-claude'))
        env.pop('AM_TOKEN', None); env.pop('AM_SERVER', None)
        bindir = self.root/'user bin'
        installer = self.root/'install.sh'; installer.write_bytes(self.request('/install.sh')[0])
        def install(*args, success=True):
            result = subprocess.run(['bash', str(installer), '--server', self.url, '--bin-dir', str(bindir), *args],
                env=env, capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
            return result
        install()
        cli = str(bindir/'relayctl')
        for base in ['codex', 'custom-claude']:
            self.assertEqual((self.root/base/'skills/agent-bridge/SKILL.md').read_bytes(), skill)
        self.assertFalse((self.root/'config/config.json').exists())
        token = self.root/'private token'; token.write_text(fixtures.TOKEN + '\n'); token.chmod(0o600)
        result=install('--setup', '--no-daemon', '--token-file', str(token))
        self.assertNotIn(fixtures.TOKEN, result.stdout+result.stderr)
        self.assertEqual((self.root/'config/config.json').stat().st_mode & 0o777, 0o600)
        # Non-interactive setup can reuse the existing server configuration.
        install('--setup', '--no-daemon')
        result=subprocess.run([cli, 'devices'],env=env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout), [])
        status=json.loads(subprocess.run([cli,'daemon','status'],env=env,capture_output=True,text=True,check=True).stdout)
        self.assertFalse(status['running']); self.assertFalse(status['installed'])
        # An edited skill is preserved unless the caller explicitly requests an update.
        custom=self.root/'custom-claude/skills/agent-bridge/SKILL.md'; custom.write_text('local customization')
        install(success=False)
        self.assertEqual(custom.read_text(), 'local customization')
        install('--force-skill')
        backups=list(custom.parent.glob('SKILL.md.backup-*'))
        self.assertEqual(len(backups),1); self.assertEqual(backups[0].read_text(),'local customization')
        self.assertEqual(custom.read_bytes(),skill)
        empty=self.root/'empty-token'; empty.write_text('')
        install('--setup','--no-daemon','--token-file',str(empty),success=False)
        self.assertEqual(json.loads((self.root/'config/config.json').read_text())['token'],fixtures.TOKEN)

    def test_corrupted_download_never_replaces_existing_cli(self):
        dist=self.root/'downloads'; release=dist/'broken'; release.mkdir(parents=True)
        asset='relayctl-' + ('darwin' if os.uname().sysname=='Darwin' else 'linux') + '-' + ('arm64' if os.uname().machine in ['arm64','aarch64'] else 'amd64')
        (dist/'current.json').write_text(json.dumps({'version':'broken'},indent=2))
        (release/'SHA256SUMS').write_text('0'*64+'  '+asset+'\n')
        (release/asset).write_text('this binary is corrupted')
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self,*args): pass
        download_server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(self.root)))
        thread=threading.Thread(target=download_server.serve_forever,daemon=True); thread.start()
        bindir=self.root/'bin'; bindir.mkdir(); (bindir/'relayctl').write_text('old binary')
        try:
            result=subprocess.run(['bash',str(PROJECT/'public/install.sh'),'--server','http://127.0.0.1:'+str(download_server.server_port),
                '--bin-dir',str(bindir),'--cli-only'],capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('SHA-256',result.stderr)
            self.assertEqual((bindir/'relayctl').read_text(),'old binary')
        finally:
            download_server.shutdown(); download_server.server_close(); thread.join(timeout=3)
