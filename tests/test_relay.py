import base64
import concurrent.futures
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path

from scripts.devtools import APIError, Client, MAX_FILE, read_json, Server, binary, download, java_binary


TOKEN = "sk-relay_" + "r" * 43
PROJECT = Path(__file__).resolve().parents[1]


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server(("127.0.0.1", 0), self.root / "data", TOKEN)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = Client(self.url, TOKEN)

    def tearDown(self):
        self.server.close()
        self.temp.cleanup()

    def register(self, name="home"):
        identity = self.client.request("POST", "/api/register", {
            "name": name, "platform": "test", "roots": [str(self.root)],
            "capabilities": {"unlimited_tasks": True, "codex": True, "claude": True},
        })
        return identity, Client(self.url, identity["token"])

    def create_agent(self, device="home", name="test-agent", kind="codex"):
        return self.client.request("POST", "/api/agents", {"device": device, "kind": kind, "name": name, "cwd": str(self.root)})

    def prompt(self, agent, text="hello"):
        return self.client.request("POST", "/api/jobs", {"kind": "prompt", "agent": agent["id"], "prompt": text})

    def test_authentication_and_device_scope(self):
        identity, device = self.register()
        for client, path, expected in [(Client(self.url, "wrong"), "/api/state", 401),
                                       (device, "/api/state", 403),
                                       (device, "/api/jobs/not-a-job", 403)]:
            with self.assertRaises(APIError) as error:
                client.request("GET", path)
            self.assertEqual(error.exception.status, expected)
        with self.assertRaises(APIError) as error:
            self.client.request("POST", "/api/device/poll", {"capacity": 1})
        self.assertEqual(error.exception.status, 403)
        state = self.client.request("GET", "/api/state")
        self.assertNotIn("token_hash", state["devices"][0])
        self.assertNotIn(identity["token"], json.dumps(state))

    def test_agents_are_serialized_and_independent_sessions_parallelize(self):
        _, device = self.register()
        first = self.create_agent(name="first")
        second = self.create_agent(name="second")
        one, two, other = self.prompt(first), self.prompt(first), self.prompt(second)
        claimed = device.request("POST", "/api/device/poll", {"capacity": 4})["jobs"]
        self.assertEqual({j["id"] for j in claimed}, {one["id"], other["id"]})
        self.assertEqual(device.request("POST", "/api/device/poll", {"capacity": 4})["jobs"], [])
        device.request("POST", "/api/device/jobs/" + one["id"] + "/complete", {"status": "succeeded", "session_id": "session-123", "output": "done"})
        next_job = device.request("POST", "/api/device/poll", {"capacity": 4})["jobs"][0]
        self.assertEqual(next_job["id"], two["id"])
        self.assertEqual(next_job["agent"]["session_id"], "session-123")

    def test_concurrent_poll_claims_exactly_once(self):
        _, device = self.register()
        agent = self.create_agent()
        job = self.prompt(agent)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: device.request("POST", "/api/device/poll", {"capacity": 4}), range(4)))
        claimed = [j["id"] for reply in replies for j in reply["jobs"]]
        self.assertEqual(claimed, [job["id"]])

    def test_device_cannot_complete_another_devices_job(self):
        _, home = self.register()
        _, work = self.register("work")
        job = self.prompt(self.create_agent())
        home.request("POST", "/api/device/poll", {"capacity": 1})
        with self.assertRaises(APIError) as error:
            work.request("POST", "/api/device/jobs/" + job["id"] + "/complete", {"status": "succeeded"})
        self.assertEqual(error.exception.status, 404)

    def test_cancel_queued_and_running_tasks(self):
        _, device = self.register()
        agent = self.create_agent()
        queued = self.prompt(agent)
        cancelled = self.client.request("POST", "/api/jobs/" + queued["id"] + "/cancel", {})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(device.request("POST", "/api/device/poll", {"capacity": 1})["jobs"], [])
        running = self.prompt(agent)
        device.request("POST", "/api/device/poll", {"capacity": 1})
        self.client.request("POST", "/api/jobs/" + running["id"] + "/cancel", {})
        self.assertIn(running["id"], device.request("POST", "/api/device/poll", {"capacity": 1})["cancel"])
        result = device.request("POST", "/api/device/jobs/" + running["id"] + "/complete", {"status": "succeeded"})
        self.assertEqual(result["status"], "cancelled")

    def test_expired_lease_fails_without_requeue_or_late_success(self):
        _, device = self.register()
        job = self.prompt(self.create_agent())
        device.request("POST", "/api/device/poll", {"capacity": 1})
        self.server.database.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
        result = device.request("POST", "/api/device/poll", {"capacity": 1, "active": [{"id": job["id"], "output": "late"}]})
        self.assertEqual(result["jobs"], [])
        result = device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", {"status": "succeeded"})
        self.assertEqual(result["status"], "failed")

    def test_file_integrity_idempotency_and_no_local_overwrite(self):
        _, device = self.register()
        job = self.client.request("POST", "/api/jobs", {"kind": "file_read", "device": "home", "path": "中文.bin"})
        device.request("POST", "/api/device/poll", {"capacity": 1})
        content = "你好\x00\n".encode() + bytes(range(256))
        body = {"status": "succeeded", "file": {"name": "中文.bin", "data": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest()}}
        result = device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", body)
        retry = device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", body)
        self.assertEqual(result["artifact_id"], retry["artifact_id"])
        target = self.root / "received.bin"
        download(self.client, result["artifact_id"], target)
        self.assertEqual(target.read_bytes(), content)
        with self.assertRaises(FileExistsError):
            download(self.client, result["artifact_id"], target)
        self.assertEqual(len(self.client.request("GET", "/api/state")["artifacts"]), 1)

    def test_invalid_file_checksum_is_rejected(self):
        _, device = self.register()
        job = self.client.request("POST", "/api/jobs", {"kind": "file_read", "device": "home", "path": "test"})
        device.request("POST", "/api/device/poll", {"capacity": 1})
        with self.assertRaises(APIError):
            device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", {"status": "succeeded", "file": {"name": "test", "data": "YWJj", "sha256": "wrong"}})
        self.assertEqual(self.client.request("GET", "/api/state")["artifacts"], [])

    def test_failed_poll_rolls_back_session_and_claim_changes(self):
        _, device = self.register()
        with self.assertRaises(APIError):
            device.request("POST", "/api/device/poll", {"capacity": True, "sessions": [{
                "kind": "codex", "session_id": "rollback-session", "cwd": str(self.root),
                "name": "must not persist", "busy": False}]})
        self.assertEqual(self.client.request("GET", "/api/state")["agents"], [])

    def test_oversized_file_and_malformed_json_are_rejected(self):
        _, device = self.register()
        job = self.client.request("POST", "/api/jobs", {"kind": "file_read", "device": "home", "path": "big.bin"})
        device.request("POST", "/api/device/poll", {"capacity": 1})
        content = b"x" * (MAX_FILE + 1)
        with self.assertRaises(APIError) as error:
            device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", {
                "status": "succeeded", "file": {"name": "big.bin", "data": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest()}})
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(self.client.request("GET", "/api/state")["artifacts"], [])
        for invalid in (b"[]", b'{}{}', b'{broken'):
            request = urllib.request.Request(self.url + "/api/register", data=invalid,
                headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
            self.assertIn("error", json.loads(error.exception.read()))

    def test_exact_file_limit_and_empty_files_download_through_go_cli(self):
        _, device = self.register()
        for content in (b"", b"x" * MAX_FILE):
            job = self.client.request("POST", "/api/jobs", {"kind": "file_read", "device": "home", "path": "limit.bin"})
            device.request("POST", "/api/device/poll", {"capacity": 1})
            result = device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", {
                "status": "succeeded", "file": {"name": "limit.bin", "data": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest()}})
            target = self.root / ("received-" + str(len(content)))
            download(self.client, result["artifact_id"], target)
            self.assertEqual(target.read_bytes(), content)

    def test_persistence_and_offline_state(self):
        self.register()
        job = self.prompt(self.create_agent())
        self.server.database.execute("UPDATE devices SET last_seen=0")
        self.assertFalse(self.client.request("GET", "/api/state")["devices"][0]["online"])
        database = self.server.database
        self.server.shutdown()
        self.server = Server(("127.0.0.1", 0), self.root / "data", TOKEN, database=database)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = Client(self.url, TOKEN)
        self.assertEqual(self.client.request("GET", "/api/jobs/" + job["id"])["status"], "queued")


    def test_invalid_payload_and_static_traversal(self):
        self.register()
        agent = self.create_agent()
        with self.assertRaises(APIError):
            self.client.request("POST", "/api/jobs", {"kind": "prompt", "agent": agent["id"], "prompt": "x", "timeout": True})
        with self.assertRaises(APIError) as error:
            self.client.request("GET", "/../relayctl/server.py")
        self.assertIn(error.exception.status, (400, 404))
        with urllib.request.urlopen(self.url + "/") as response:
            self.assertIn(b"Relay", response.read())
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_conversation_retains_all_turns_beyond_global_job_limit(self):
        self.register()
        agent = self.create_agent()
        first = self.prompt(agent, "the first turn")
        for number in range(52):
            self.prompt(agent, "follow-up %s" % number)
        response = self.client.request("GET", "/api/agents/" + agent["id"] + "/messages")
        self.assertTrue(response["has_more"])
        self.assertEqual(len(response["messages"]), 100)
        oldest = response["messages"][0]
        earlier = self.client.request("GET", "/api/agents/" + agent["id"] + "/messages?before=" + quote(str(oldest["created_at"]) + "|" + oldest["id"], safe=""))
        self.assertEqual(earlier["messages"][0]["content"], "the first turn")
        self.assertEqual(earlier["messages"][0]["job_id"], first["id"])
        self.assertEqual(len(earlier["messages"]) + len(response["messages"]), 106)

    def test_native_session_sync_continuation_and_busy_queue(self):
        _, device = self.register()
        session = {"kind": "codex", "session_id": "native-session-1", "cwd": str(self.root),
                   "name": "local project", "busy": True, "updated_at": time.time(),
                   "messages": [{"id": "first", "role": "user", "content": "from the terminal", "created_at": time.time() - 20}]}
        device.request("POST", "/api/device/poll", {"capacity": 4, "sessions": [session]})
        agent = self.client.request("GET", "/api/state")["agents"][0]
        self.assertEqual(agent["origin"], "native")
        self.assertTrue(agent["busy"])
        job = self.prompt(agent, "continue from browser")
        self.assertEqual(device.request("POST", "/api/device/poll", {"capacity": 4, "sessions": [session]})["jobs"], [])
        session["busy"] = False
        session["messages"].append({"id": "second", "role": "assistant", "content": "terminal answer", "created_at": time.time() - 10})
        claimed = device.request("POST", "/api/device/poll", {"capacity": 4, "sessions": [session]})["jobs"]
        self.assertEqual(claimed[0]["agent"]["session_id"], "native-session-1")
        device.request("POST", "/api/device/jobs/" + job["id"] + "/complete", {"status": "succeeded", "output": "browser answer"})
        conversation = self.client.request("GET", "/api/agents/" + agent["id"] + "/messages")
        self.assertEqual([m["content"] for m in conversation["messages"]], ["from the terminal", "terminal answer", "continue from browser", "browser answer"])
        self.assertEqual(len(self.client.request("GET", "/api/state")["agents"]), 1)

    def test_native_activity_uses_session_time_instead_of_first_sync_time(self):
        _, device = self.register()
        older = time.time() - 45 * 86400
        session = {'kind': 'codex', 'session_id': 'old-imported-session', 'name': 'old project',
                   'cwd': str(self.root), 'busy': False, 'updated_at': older,
                   'messages': [{'id': 'old-message', 'role': 'user', 'content': 'old question', 'created_at': older}]}
        device.request('POST', '/api/device/poll', {'sessions': [session]})
        agent = self.client.request('GET', '/api/state')['agents'][0]
        self.assertGreater(agent['created_at'], older)
        self.assertEqual(agent['last_activity'], older)
        managed = self.create_agent(name='empty managed session')
        state = self.client.request('GET', '/api/state')
        empty = next(a for a in state['agents'] if a['id'] == managed['id'])
        self.assertEqual(empty['last_activity'], empty['created_at'])
        self.prompt(agent, 'continue this old conversation')
        resumed = next(a for a in self.client.request('GET', '/api/state')['agents'] if a['id'] == agent['id'])
        self.assertGreater(resumed['last_activity'], older + 44 * 86400)

    def test_live_progress_binds_native_session_without_duplicate_agent(self):
        _, device = self.register()
        agent = self.create_agent()
        job = self.prompt(agent)
        device.request("POST", "/api/device/poll", {"capacity": 1})
        device.request("POST", "/api/device/poll", {"capacity": 0,
            "active": [{"id": job["id"], "session_id": "session-now-started", "output": "first words"}],
            "sessions": [{"kind": "codex", "session_id": "session-now-started", "cwd": str(self.root), "name": "same session", "busy": True}]})
        conversation = self.client.request("GET", "/api/agents/" + agent["id"] + "/messages")
        self.assertEqual(conversation["messages"][-1]["content"], "first words")
        self.assertEqual(conversation["messages"][-1]["status"], "running")
        self.assertEqual(len(self.client.request("GET", "/api/state")["agents"]), 1)

    def test_native_history_pagination_with_equal_timestamps(self):
        _, device = self.register()
        session = {"kind": "claude", "session_id": "claude-session-1", "cwd": str(self.root), "name": "Claude", "busy": False,
                   "messages": [{"id": str(i), "role": "assistant", "content": "message %s" % i, "created_at": 1000} for i in range(105)]}
        device.request("POST", "/api/device/poll", {"capacity": 0, "sessions": [session]})
        agent = self.client.request("GET", "/api/state")["agents"][0]
        first = self.client.request("GET", "/api/agents/" + agent["id"] + "/messages")
        oldest = first["messages"][0]
        second = self.client.request("GET", "/api/agents/" + agent["id"] + "/messages?before=" + quote("1000|" + oldest["id"], safe=""))
        self.assertEqual(len(first["messages"]), 100)
        self.assertEqual(len(second["messages"]), 5)
        self.assertEqual(len({m["id"] for m in first["messages"] + second["messages"]}), 105)


FAKE_AGENT = r'''
import json, os, pathlib, sys, time
prompt = sys.stdin.read()
image_count = 0
if "--input-format" in sys.argv:
 message=json.loads(prompt)
 image_count=sum(part["type"]=="image" for part in message["message"]["content"])
 prompt=next(part["text"] for part in message["message"]["content"] if part["type"]=="text")
if "--image" in sys.argv:
 image_count=sys.argv.count("--image")
 for i,arg in enumerate(sys.argv):
  if arg=="--image":
   data=pathlib.Path(sys.argv[i+1]).read_bytes()
   assert data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")
if image_count:
 prompt="IMAGES:%d " % image_count + prompt
kind = pathlib.Path(sys.argv[0]).name
session = "fake-session-001"
if kind == "codex" and "resume" in sys.argv:
 session = sys.argv[-2]
elif kind == "claude" and "--resume" in sys.argv:
 session = sys.argv[sys.argv.index("--resume") + 1]
def event(value):
 print(json.dumps(value), flush=True)
if prompt == "writer-conflict" and kind == "codex":
 print("thread-store conflict: thread native-codex-1 already has an active writer", file=sys.stderr, flush=True)
 sys.exit(1)
if kind == "codex":
 event({"type":"thread.started","thread_id":session})
else:
 event({"type":"system","subtype":"init","session_id":session})
if prompt == "slow":
 pathlib.Path("slow.pid").write_text(str(os.getpid()))
 time.sleep(60)
if prompt == "stream" and kind == "codex":
 event({"type":"item.completed","item":{"type":"agent_message","text":"first live update"}})
 time.sleep(2.5)
if kind == "codex":
 event({"type":"item.completed","item":{"type":"agent_message","text":("RESUMED: " if "resume" in sys.argv else "NEW: ") + prompt}})
else:
 event({"type":"assistant","message":{"content":[{"type":"text","text":("RESUMED: " if "--resume" in sys.argv else "NEW: ") + prompt}]}})
 event({"type":"result","is_error":prompt == "model-error", "result":prompt,"session_id":session})
'''


class EndToEndCase(unittest.TestCase):
    setUp = ServerCase.setUp
    tearDown = ServerCase.tearDown

    def test_two_device_daemons_cli_file_transfer_sessions_and_cancel(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        for kind in ("codex", "claude"):
            executable = bindir / kind
            executable.write_text("#!" + sys.executable + "\n" + FAKE_AGENT)
            executable.chmod(0o755)
        home, work = self.root / "home", self.root / "work"
        home.mkdir(); work.mkdir()
        contents = b"\x00\xff" + "家里的 XX 文件\n".encode()
        (home / "XX.txt").write_bytes(contents)
        env = dict(os.environ, AM_SERVER=self.url, AM_TOKEN=TOKEN, AM_CONFIG_DIR=str(self.root / "config"),
                   PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        daemons = []
        try:
            for name, shared in (("home", home), ("work", work)):
                args = [binary(), "daemon", "run", "--virtual-device", "--name", name, "--root", str(shared),
                        "--state-dir", str(self.root / (name + "-state")), "--interval", "0.1"]
                daemons.append(subprocess.Popen(args, cwd=str(PROJECT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
            end = time.monotonic() + 10
            while len(self.client.request("GET", "/api/state")["devices"]) < 2:
                self.assertLess(time.monotonic(), end, "daemon registration timed out")
                time.sleep(0.1)
            def cli(*args, success=True):
                process = subprocess.run([binary(), *args], cwd=str(PROJECT), env=env,
                                         capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 0 if success else 1, process.stderr + process.stdout)
                return json.loads(process.stdout) if process.stdout else json.loads(process.stderr)
            cli("configure", "--server", self.url)
            self.assertEqual((self.root / "config/config.json").stat().st_mode & 0o777, 0o600)
            for kind in ("codex", "claude"):
                agent = cli("agents", "create", "--device", "home", "--kind", kind, "--name", kind, "--cwd", str(home))
                first = cli("send", agent["id"], "hello", "--wait")
                self.assertIn("NEW: hello", first["output"])
                second = cli("send", agent["id"], "again", "--wait")
                self.assertIn("RESUMED: again", second["output"])
                if kind == "claude":
                    failed = cli("send", agent["id"], "model-error", "--wait", success=False)
                    self.assertEqual(failed["status"], "failed")
            target = work / "received.txt"
            received = cli("files", "pull", "--device", "home", "--path", "XX.txt", "--output", str(target))
            self.assertEqual(target.read_bytes(), contents)
            self.assertEqual(received["sha256"], hashlib.sha256(contents).hexdigest())
            outside = cli("files", "pull", "--device", "home", "--path", "../work/received.txt", "--output", str(work / "outside.txt"))
            self.assertEqual((work / "outside.txt").read_bytes(),contents)
            oversized = home / "large.bin"
            with oversized.open("wb") as f:
                f.truncate(MAX_FILE + 1)
            too_large = cli("files", "pull", "--device", "home", "--path", "large.bin", "--output", str(work / "large.bin"), success=False)
            self.assertIn("16 MiB", too_large["error"])
            slow = cli("send", "codex", "slow")
            end = time.monotonic() + 10
            while not (home / "slow.pid").exists():
                self.assertLess(time.monotonic(), end)
                time.sleep(0.05)
            child_pid = int((home / "slow.pid").read_text())
            cli("jobs", "cancel", slow["id"])
            cancelled = self.client.wait(slow["id"], 10)
            self.assertEqual(cancelled["status"], "cancelled")
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            installed = cli("skill", "install", "--path", str(work / "skills"))
            self.assertTrue(Path(installed["installed"][0]).is_file())
            identity = read_json(self.root / "home-state/identity.json")
            self.assertNotEqual(identity["token"], TOKEN)
            self.assertEqual(len(cli("devices")), 2)
        finally:
            for proc in daemons:
                proc.send_signal(signal.SIGINT)
            for proc in daemons:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                errors = proc.stderr.read().decode()
                proc.stderr.close()
                self.assertNotIn("Traceback", errors, errors)


if __name__ == "__main__":
    unittest.main()
