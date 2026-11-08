"""Optional macOS smoke: install a real isolated LaunchAgent, then remove only it."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Server,Client
from test_sessions import codex_records,write_records

if sys.platform!='darwin':
    print('macOS LaunchAgent smoke skipped on this platform.'); raise SystemExit(0)
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder); shared=root/'shared'; shared.mkdir()
    cli=root/'relayctl'; cli.write_bytes((Path(__file__).resolve().parents[1]/('dist/downloads/0.5.2/relayctl-darwin-'+('arm64' if os.uname().machine=='arm64' else 'amd64'))).read_bytes()); cli.chmod(0o755)
    server=Server(('127.0.0.1',0),root/'data',('sk-relay_'+'d'*43))
    url='http://127.0.0.1:'+str(server.server_port)
    c=Client(url,('sk-relay_'+'d'*43))
    env=dict(os.environ,AM_CONFIG_DIR=str(root/'config'),CODEX_HOME=str(root/'codex'),CLAUDE_CONFIG_DIR=str(root/'claude'))
    write_records(root/'codex/sessions/existing.jsonl',codex_records(shared,sid='rootless-smoke'))
    secret=root/'token';secret.write_text(('sk-relay_'+'d'*43));secret.chmod(0o600)
    env['PATH']=str(root)+os.pathsep+env.get('PATH','')
    installed=False
    service_path=None
    def run(*args):
        r=subprocess.run([str(cli),*args],env=env,capture_output=True,text=True,timeout=30)
        assert r.returncode==0,r.stdout+r.stderr
        return r.stdout
    try:
        result=subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'public/install.sh'),'--server',url,'--bin-dir',str(root),'--setup','--allow-agent-writes','--token-file',str(secret)],env=env,capture_output=True,text=True,timeout=45)
        preliminary=json.loads(run('daemon','status'))
        installed=preliminary['installed']; service_path=preliminary['service_path']
        assert result.returncode==0,result.stdout+result.stderr
        installed=True
        end=time.monotonic()+15
        while True:
            status=json.loads(run('daemon','status'))
            state=c.request('GET','/api/state')
            if status['running'] and state['devices'] and state['agents']:break
            assert time.monotonic()<end,status
            time.sleep(.2)
        identity=status['device_id']; assert status['installed']
        assert status['access_scope']=='user' and state['devices'][0]['capabilities']['access_scope']=='user'
        assert state['devices'][0]['capabilities']['writable'] is True
        assert state['agents'][0]['controllable']
        # An exact rerun restarts, and explicit restart keeps the physical ID.
        repeated=subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'public/install.sh'),'--server',url,'--bin-dir',str(root),'--setup','--allow-agent-writes','--token-file',str(secret)],env=env,capture_output=True,text=True,timeout=45)
        assert repeated.returncode==0,repeated.stdout+repeated.stderr
        run('daemon','restart')
        end=time.monotonic()+15
        while True:
            status=json.loads(run('daemon','status'))
            if status['running']:break
            assert time.monotonic()<end
            time.sleep(.2)
        assert status['device_id']==identity
        assert len(c.request('GET','/api/state')['devices'])==1
        run('daemon','uninstall');installed=False
        status=json.loads(run('daemon','status'))
        assert not status['installed'] and not status['running']
        assert status['device_id']==identity
        print('Rootless full installer + real LaunchAgent passed: install, online registration, idempotent reinstall, restart, status and uninstall; original identity retained.')
    except Exception:
        for log in (root/'config/logs').glob('*.log'):
            print(log.read_text()[-4000:],file=sys.stderr)
        if 'status' in locals():
            label=Path(status['service_path']).stem
            detail=subprocess.run(['launchctl','print','gui/'+str(os.getuid())+'/'+label],capture_output=True,text=True)
            print(detail.stdout[-4500:],file=sys.stderr)
        raise
    finally:
        if installed: run('daemon','uninstall')
        server.close()
