import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_FILE = 16 * 1024 * 1024
MAX_BODY = 23 * 1024 * 1024
MAX_OUTPUT = 64000
LEASE_SECONDS = 60
ONLINE_SECONDS = 15
TERMINAL = {"succeeded", "failed", "cancelled"}


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Client:
    def __init__(self, server, token, timeout=15):
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("服务端地址必须是 http(s) URL，且不能包含账号或密码")
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method, path, body=None, raw=False):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.server + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
        })
        # Do not forward bearer credentials to a redirected host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=self.timeout) as response:
                if raw:
                    data = response.read(MAX_FILE + 1)
                    if len(data) > MAX_FILE:
                        raise APIError("文件超过 16 MiB 限制")
                    return data, dict(response.headers)
                return json.loads(response.read(MAX_BODY).decode())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read(4096)).get("error", str(exc))
            except (ValueError, AttributeError):
                message = "HTTP " + str(exc.code)
            raise APIError(message, exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise APIError("无法连接服务端: " + str(exc), 503) from None

    def wait(self, job_id, timeout=330):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.request("GET", "/api/jobs/" + job_id)
            if job["status"] in TERMINAL:
                return job
            time.sleep(0.4)
        raise APIError("等待超时，任务仍可通过 relayctl jobs show " + job_id + " 查看", 408)

# Development/test harness only. Both application processes are native Java/Go.
import queue
import re
import shutil
import subprocess
import threading

PROJECT = Path(__file__).resolve().parents[1]
BINARY = PROJECT / 'bin/relayctl'


def binary():
    if not BINARY.exists():
        subprocess.run(['go', 'build', '-o', str(BINARY), './cmd/relayctl'], cwd=PROJECT, check=True)
    return str(BINARY)


def java_binary():
    candidates = [str(Path(os.environ.get('JAVA_HOME', '/nonexistent')) / 'bin/java'), shutil.which('java'),
                  '/opt/homebrew/opt/openjdk/bin/java', '/usr/local/opt/openjdk/bin/java']
    candidates += [str(p) for p in Path('/Library/Java/JavaVirtualMachines').glob('*/Contents/Home/bin/java')]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        result = subprocess.run([candidate, '-version'], capture_output=True, text=True)
        version = re.search(r'version "(\d+)', result.stderr + result.stdout)
        if result.returncode == 0 and version and int(version[1]) >= 17:
            return candidate
    raise RuntimeError('Java 17+ is required')


class Server:
    def __init__(self, address, directory, token, database=None, isolated=True, environment=None):
        if isolated and database is None:
            from scripts.mysql_test import Database
            database = Database()
        self.database = database
        db_env = database.environment() if database else {}
        if not isolated:
            env_file = PROJECT / '.local/relay.env'
            if env_file.exists():
                db_env = dict(line.split('=', 1) for line in env_file.read_text().splitlines()
                              if line and not line.startswith('#') and '=' in line)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / 'server.log').open('a')
        self.process = subprocess.Popen([binary(), 'serve', '--host', address[0], '--port', str(address[1]),
                                         '--data', str(self.directory)], cwd=PROJECT,
                                        env={**os.environ, **db_env, **(environment or {})}, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
        ready = queue.Queue()

        def read():
            for line in self.process.stdout:
                self.log.write(line)
                self.log.flush()
                if line.startswith('RELAY_READY '):
                    ready.put(int(line.split()[1]))
            ready.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            self.server_port = ready.get(timeout=60)
            if self.server_port is None:
                raise RuntimeError('Java server failed to start; see ' + str(self.directory / 'server.log') + '\n' + (self.directory / 'server.log').read_text()[-6000:])
            if isolated and token:
                # Test-only bootstrap. Production has no token login/bootstrap bypass.
                database.execute("INSERT IGNORE INTO app_users(id,email,display_name,created_at) VALUES('fixture','fixture@test.invalid','Fixture',0)")
                database.execute("INSERT IGNORE INTO agent_keys(id,user_id,name,token_hash,token_prefix,created_at,expires_at) VALUES('fixture-key','fixture','Fixture',?,?,0,4102444800)",(hashlib.sha256(token.encode()).hexdigest(),token[:17]))
        except Exception:
            self.close()
            raise

    def browser_login(self, page):
        import secrets
        raw=secrets.token_urlsafe(32)
        self.database.execute("INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(?,'fixture',4102444800)",(hashlib.sha256(raw.encode()).hexdigest(),))
        page.context.add_cookies([{'name':'relay_session','value':raw,'url':'http://127.0.0.1:'+str(self.server_port),'httpOnly':True,'sameSite':'Lax'}])
        page.reload()

    def shutdown(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=3)
        self.process.stdout.close()
        self.log.close()

    def close(self):
        self.shutdown()
        if self.database:
            self.database.close()


def download(client, file_id, output):
    result = subprocess.run([binary(), 'files', 'download', file_id, '--output', str(output)],
                            env=dict(os.environ, AM_SERVER=client.server, AM_TOKEN=client.token),
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        if Path(output).exists():
            raise FileExistsError(output)
        raise APIError(result.stderr)
    return json.loads(result.stdout)
