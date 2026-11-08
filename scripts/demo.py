"""Run the server, this workspace's device, and two simulated devices."""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devtools import Client, read_json, Server, binary


def main():
    project = Path(__file__).resolve().parents[1]
    root = project / ".demo"
    root.mkdir(exist_ok=True)
    secret = os.environ.get("AM_TOKEN", "") or read_json(root / "config/config.json").get("token", "")
    port = int(os.environ.get("AM_DEMO_PORT", "8765"))
    for candidate in range(port, port + (1 if "AM_DEMO_PORT" in os.environ else 10)):
        try:
            instance = root / ("mysql-port-" + str(candidate))
            server = Server(("127.0.0.1", candidate), instance / "server", secret, isolated=False)
            port = candidate
            break
        except RuntimeError:
            if "AM_DEMO_PORT" in os.environ or candidate == port + 9:
                raise
    url = "http://127.0.0.1:" + str(port)
    processes = []
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        print("Relay: " + url,flush=True)
        print('请在网页注册/登录，创建 SK，再在另一个终端配置：',flush=True)
        print('AM_CONFIG_DIR="'+str(root/'config')+'" bin/relayctl configure --server '+url,flush=True)
        while not stopped.is_set():
            secret=os.environ.get('AM_TOKEN','') or read_json(root/'config/config.json').get('token','')
            if secret:
                try:Client(url,secret).request('GET','/api/state');break
                except Exception:pass
            stopped.wait(1)
        if stopped.is_set():return
        env = dict(os.environ, AM_SERVER=url, AM_TOKEN=secret, AM_CONFIG_DIR=str(root / "config"))
        for name in ("home", "work", "local"):
            shared = project if name == "local" else root / name / "shared"
            shared.mkdir(parents=True, exist_ok=True)
            sample = shared / "XX.txt"
            if name == "home" and not sample.exists():
                sample.write_text("这是家里电脑上的 XX 文件。\nHello from home!\n", encoding="utf-8")
            proc = subprocess.Popen([binary(), "daemon", "run", "--name", name,
                                     "--root", str(shared), "--state-dir", str(instance / name / "state")] + ([] if name == "local" else ["--virtual-device"]),
                                    cwd=str(project), env=env)
            processes.append(proc)
        client = Client(url, secret)
        deadline = time.monotonic() + 15
        while len(client.request("GET", "/api/state")["devices"]) < 3:
            if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                raise RuntimeError("演示设备启动失败")
            time.sleep(0.1)
        state = client.request("GET", "/api/state")
        for device in state["devices"]:
            if device["name"] == "local":
                continue
            kind = "codex" if device["capabilities"].get("codex") else "claude" if device["capabilities"].get("claude") else None
            if kind and not any(a["device_id"] == device["id"] for a in state["agents"]):
                client.request("POST", "/api/agents", {"device": device["id"], "name": device["name"] + "-" + kind,
                                                       "kind": kind, "cwd": device["roots"][0]})
        print("\nRelay demo: " + url, flush=True)
        print("home/work 为模拟设备；local 发现当前用户的所有会话，授权操作当前项目目录。", flush=True)
        print("发送指令会使用本机已登录的 Agent CLI；本脚本不会自动调用模型。", flush=True)
        print("可在「文件中转」取回 home 上的 XX.txt；Ctrl-C 结束。\n", flush=True)
        while not stopped.wait(0.5):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("演示后台进程退出")
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        server.shutdown()


if __name__ == "__main__":
    main()
