package relay

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// An actual Unix WebSocket listener exercises the native transport without a
// running model, credentials, or changes to the user's Codex daemon.
func codexFixture(t *testing.T, serve func(*websocket.Conn)) {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "relay-codex-")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEX_HOME", dir)
	path := codexSocket()
	if err = os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	upgrader := websocket.Upgrader{}
	var group sync.WaitGroup
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		group.Add(1)
		defer group.Done()
		defer conn.Close()
		serve(conn)
	})}
	go server.Serve(listener)
	t.Cleanup(func() { server.Close(); group.Wait(); os.RemoveAll(dir) })
}
func sharedTask(t *testing.T) *Task {
	return newTask(M{"id": "test-job", "kind": "prompt", "agent": M{"kind": "codex", "session_id": "native-session", "cwd": t.TempDir()}, "payload": M{"prompt": "continue", "timeout": 5}})
}
func waitTask(t *testing.T, task *Task) {
	t.Helper()
	select {
	case <-task.done:
	case <-time.After(10 * time.Second):
		task.cancel()
		t.Fatal("task did not finish")
	}
}
func rpcReply(conn *websocket.Conn, req M, result M) {
	conn.WriteJSON(M{"id": req["id"], "result": result})
}
func notify(conn *websocket.Conn, method string, params M) {
	conn.WriteJSON(M{"method": method, "params": params})
}

func TestSharedCodexContinuesOriginalThreadAndRestoresPermissions(t *testing.T) {
	for _, writable := range []bool{false, true} {
		t.Run(map[bool]string{false: "readonly", true: "writable"}[writable], func(t *testing.T) {
			task := sharedTask(t)
			requests := make(chan M, 20)
			fixtureDone := make(chan struct{})
			codexFixture(t, func(conn *websocket.Conn) {
				defer close(fixtureDone)
				for {
					var req M
					if conn.ReadJSON(&req) != nil {
						return
					}
					requests <- req
					params := obj(req["params"])
					switch req["method"] {
					case "initialize":
						rpcReply(conn, req, M{})
					case "thread/loaded/list":
						rpcReply(conn, req, M{"data": []string{"native-session"}})
					case "thread/resume":
						rpcReply(conn, req, M{"thread": M{"id": "native-session", "status": M{"type": "idle"}}, "approvalPolicy": "on-request", "sandbox": M{"type": "dangerFullAccess"}})
					case "thread/settings/update":
						rpcReply(conn, req, M{})
						if params["sandboxPolicy"] != nil {
							deadline := time.Now().Add(2 * time.Second)
							for task.progress()["output"] != "live " && time.Now().Before(deadline) {
								time.Sleep(time.Millisecond)
							}
							if task.progress()["output"] != "live " {
								t.Error("streaming text was not visible before completion")
							}
							notify(conn, "item/completed", M{"threadId": "native-session", "turnId": "ours", "item": M{"type": "agentMessage", "text": "live answer"}})
							notify(conn, "turn/completed", M{"threadId": "native-session", "turn": M{"id": "ours", "status": "completed"}})
						}
					case "thread/read":
						rpcReply(conn, req, M{"thread": M{"status": M{"type": "idle"}}})
					case "turn/start":
						if params["threadId"] != "native-session" || params["approvalPolicy"] != "never" {
							t.Error("wrong thread or approval policy", params)
						}
						want := "readOnly"
						if writable {
							want = "workspaceWrite"
						}
						if obj(params["sandboxPolicy"])["type"] != want {
							t.Error("Relay permissions ignored", params)
						}
						if !strings.HasPrefix(str(params["clientUserMessageId"]), "relay:") {
							t.Error("missing stable submission id")
						}
						// Events may precede the response. Only our thread and turn belong in output.
						conn.WriteJSON(M{"id": "local-approval", "method": "item/commandExecution/requestApproval", "params": M{"threadId": "native-session", "turnId": "other-turn"}})
						conn.WriteJSON(M{"id": "remote-approval", "method": "item/commandExecution/requestApproval", "params": M{"threadId": "native-session", "turnId": "ours"}})
						notify(conn, "item/agentMessage/delta", M{"threadId": "other-thread", "turnId": "ours", "delta": "wrong thread"})
						notify(conn, "item/agentMessage/delta", M{"threadId": "native-session", "turnId": "other-turn", "delta": "wrong turn"})
						notify(conn, "item/agentMessage/delta", M{"threadId": "native-session", "turnId": "ours", "delta": "live "})
						rpcReply(conn, req, M{"turn": M{"id": "ours", "status": "inProgress"}})
					}
				}
			})
			go task.run(nil, writable)
			waitTask(t, task)
			select {
			case <-fixtureDone:
			case <-time.After(2 * time.Second):
				t.Fatal("fixture did not consume the client's replies")
			}
			if task.result["status"] != "succeeded" || task.result["output"] != "live answer\n" || task.result["session_id"] != "native-session" {
				t.Fatalf("original session failed: %v", task.result)
			}
			var restored bool
			var declinedOwn bool
			for len(requests) > 0 {
				req := <-requests
				if req["id"] == "local-approval" {
					t.Fatal("answered an approval owned by the native client")
				}
				if req["id"] == "remote-approval" && obj(req["result"])["decision"] == "decline" {
					declinedOwn = true
				}
				params := obj(req["params"])
				if req["method"] == "thread/settings/update" && obj(params["sandboxPolicy"])["type"] == "dangerFullAccess" && params["approvalPolicy"] == "on-request" {
					restored = true
				}
			}
			if !restored {
				t.Fatal("native permission settings were not restored")
			}
			if !declinedOwn {
				t.Fatal("Relay must decline interactive approvals for its own turn")
			}
		})
	}
}

func TestSharedCodexCancellationInterruptsOnlyOwnedTurn(t *testing.T) {
	task := sharedTask(t)
	started := make(chan struct{})
	interrupted := make(chan M, 1)
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"native-session"}})
			case "thread/resume":
				rpcReply(conn, req, M{"thread": M{"status": M{"type": "idle"}}, "approvalPolicy": "never", "sandbox": M{"type": "readOnly"}})
			case "thread/settings/update":
				rpcReply(conn, req, M{})
			case "thread/read":
				rpcReply(conn, req, M{"thread": M{"status": M{"type": "idle"}}})
			case "turn/start":
				rpcReply(conn, req, M{"turn": M{"id": "owned-turn"}})
				close(started)
			case "turn/interrupt":
				interrupted <- obj(req["params"])
				rpcReply(conn, req, M{})
				notify(conn, "turn/completed", M{"threadId": "native-session", "turn": M{"id": "owned-turn", "status": "interrupted"}})
			}
		}
	})
	go task.run(nil, false)
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("turn did not start")
	}
	task.cancel()
	waitTask(t, task)
	if task.result["status"] != "cancelled" {
		t.Fatal(task.result)
	}
	select {
	case params := <-interrupted:
		if params["threadId"] != "native-session" || params["turnId"] != "owned-turn" {
			t.Fatal(params)
		}
	default:
		t.Fatal("no exact-turn interrupt")
	}
}

func TestSharedCodexCancellationBeforeSteeringDoesNotInterruptNativeTurn(t *testing.T) {
	task := sharedTask(t)
	busy := make(chan struct{}, 1)
	mutations := make(chan string, 10)
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"native-session"}})
			case "thread/resume":
				rpcReply(conn, req, M{"thread": M{"status": M{"type": "active"}}})
				select {
				case busy <- struct{}{}:
				default:
				}
			case "thread/turns/list":
				rpcReply(conn, req, M{"data": []M{}})
			case "turn/start", "turn/interrupt":
				mutations <- str(req["method"])
				rpcReply(conn, req, M{})
			}
		}
	})
	go task.run(nil, false)
	select {
	case <-busy:
	case <-time.After(5 * time.Second):
		t.Fatal("did not inspect native turn")
	}
	task.cancel()
	waitTask(t, task)
	if task.result["status"] != "cancelled" || len(mutations) != 0 {
		t.Fatalf("changed a local turn: %v", task.result)
	}
}

func TestSharedCodexSteersActiveTurnWithoutWaitingOrChangingPermissions(t *testing.T) {
	task := sharedTask(t)
	obj(task.job["payload"])["permission_mode"] = "full"
	requests := make(chan M, 20)
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			requests <- req
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"native-session"}})
			case "thread/resume":
				rpcReply(conn, req, M{"thread": M{"status": M{"type": "active"}}})
			case "thread/turns/list":
				if num(obj(req["params"])["limit"]) != 1 {
					t.Error("loaded full transcript to find active turn")
				}
				rpcReply(conn, req, M{"data": []M{{"id": "native-turn", "status": "inProgress"}}})
			case "turn/steer":
				params := obj(req["params"])
				if params["threadId"] != "native-session" || params["expectedTurnId"] != "native-turn" || params["clientUserMessageId"] != "relay:test-job" || obj(arr(params["input"])[0])["text"] != "continue" {
					t.Error("wrong steer target or input", params)
				}
				// The native turn deliberately never completes.
				rpcReply(conn, req, M{"turnId": "native-turn"})
			default:
				if req["id"] != nil {
					t.Error("unexpected mutation", req)
					rpcReply(conn, req, M{})
				}
			}
		}
	})
	go task.run(nil, true)
	waitTask(t, task)
	if task.result["status"] != "succeeded" || !yes(task.result["steered"]) || task.result["output"] != "" || task.result["session_id"] != "native-session" {
		t.Fatal(task.result)
	}
	for len(requests) > 0 {
		method := str((<-requests)["method"])
		if method == "turn/start" || method == "turn/interrupt" || method == "thread/settings/update" {
			t.Fatal("changed native task or permissions", method)
		}
	}
}

func TestSharedCodexSteerDisconnectDoesNotReplayOrStartAnotherWriter(t *testing.T) {
	task := sharedTask(t)
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"native-session"}})
			case "thread/resume":
				rpcReply(conn, req, M{"thread": M{"status": M{"type": "active"}}})
			case "thread/turns/list":
				rpcReply(conn, req, M{"data": []M{{"id": "native-turn", "status": "inProgress"}}})
			case "turn/steer":
				return // Input may have been accepted, but its acknowledgement was lost.
			default:
				if req["id"] != nil {
					t.Error("unexpected request after ambiguous delivery", req)
				}
			}
		}
	})
	go task.run(nil, false)
	waitTask(t, task)
	if task.result["status"] != "failed" || !strings.Contains(str(task.result["error"]), "未自动重试") {
		t.Fatal(task.result)
	}
}

func TestCodexSteerOnlyDoesNotStartACompetingProcess(t *testing.T) {
	task := sharedTask(t)
	task.job["steer_only"] = true
	t.Setenv("CODEX_HOME", t.TempDir())
	marker := filepath.Join(t.TempDir(), "unexpected-writer")
	cli := filepath.Join(t.TempDir(), "codex")
	if err := os.WriteFile(cli, []byte("#!/bin/sh\ntouch '"+marker+"'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", filepath.Dir(cli)+string(os.PathListSeparator)+os.Getenv("PATH"))
	go task.run(nil, false)
	waitTask(t, task)
	if task.result["status"] != "failed" || !yes(task.result["steering"]) {
		t.Fatal(task.result)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("started another process for an active session")
	}
}

func TestSharedCodexDisconnectNeverResubmitsThroughExec(t *testing.T) {
	task := sharedTask(t)
	marker := filepath.Join(t.TempDir(), "exec-called")
	binary := filepath.Join(t.TempDir(), "codex")
	os.WriteFile(binary, []byte("#!/bin/sh\ntouch '"+marker+"'\n"), 0700)
	t.Setenv("PATH", filepath.Dir(binary)+string(os.PathListSeparator)+os.Getenv("PATH"))
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"native-session"}})
			case "thread/resume":
				rpcReply(conn, req, M{"thread": M{"status": M{"type": "idle"}}, "approvalPolicy": "never", "sandbox": M{"type": "dangerFullAccess"}})
			case "thread/settings/update":
				rpcReply(conn, req, M{})
			case "turn/start":
				return // Accepted on the wire, but no response reached Relay.
			}
		}
	})
	go task.run(nil, false)
	waitTask(t, task)
	if task.result["status"] != "failed" || !strings.Contains(str(task.result["error"]), "未自动重试") {
		t.Fatal(task.result)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("an ambiguous native submission was replayed")
	}
}

func TestSharedCodexUnloadedSessionKeepsExecFallback(t *testing.T) {
	task := sharedTask(t)
	legacy := filepath.Join(t.TempDir(), "codex")
	if err := os.WriteFile(legacy, []byte("#!/bin/sh\nexit 2\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", filepath.Dir(legacy)+string(os.PathListSeparator)+os.Getenv("PATH"))
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"different-session"}})
			default:
				if req["id"] != nil {
					t.Error("attempted to resume an unloaded native thread", req)
				}
			}
		}
	})
	if _, handled, err := task.trySharedCodex(str(obj(task.job["agent"])["cwd"]), false); handled || err != nil {
		t.Fatalf("old CLI fallback unavailable: %v %v", handled, err)
	}
}

func TestRestoreCodexPermissionsPreservesNamedProfilesAndWorkspaceLimits(t *testing.T) {
	profile := M{"approvalPolicy": "on-request", "activePermissionProfile": M{"id": "custom"}}
	params, err := restoreCodexSettings(profile, "native-session")
	if err != nil || params["permissions"] != "custom" {
		t.Fatal(params, err)
	}
	workspace := M{"approvalPolicy": "on-request", "sandbox": M{"type": "workspaceWrite", "networkAccess": true, "writableRoots": []string{"/project"}, "excludeSlashTmp": true, "excludeTmpdirEnvVar": true}}
	params, err = restoreCodexSettings(workspace, "native-session")
	if err != nil {
		t.Fatal(err)
	}
	got, _ := json.Marshal(params["sandboxPolicy"])
	want, _ := json.Marshal(workspace["sandbox"])
	if string(got) != string(want) {
		t.Fatal("workspace sandbox changed", params)
	}
	if _, err = restoreCodexSettings(M{}, "native-session"); err == nil {
		t.Fatal("unknown policies must not be changed")
	}

}

func TestCodexProbeReadsOnlyExistingLocalDaemon(t *testing.T) {
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			switch req["method"] {
			case "initialize":
				rpcReply(conn, req, M{})
			case "thread/loaded/list":
				rpcReply(conn, req, M{"data": []string{"native-session"}})
			}
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	rpc, err := dialCodex(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer rpc.close()
	loaded, err := rpc.loaded(ctx, "native-session")
	if !loaded || err != nil {
		t.Fatal(loaded, err)
	}
}
