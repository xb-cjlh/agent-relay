package relay

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func permissionFixture(t *testing.T, task *Task, start func(*websocket.Conn), response func(*websocket.Conn, M)) {
	t.Helper()
	codexFixture(t, func(conn *websocket.Conn) {
		for {
			var req M
			if conn.ReadJSON(&req) != nil {
				return
			}
			if req["method"] == nil {
				response(conn, req)
				continue
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
			case "turn/start":
				params := obj(req["params"])
				mode := str(obj(task.job["payload"])["permission_mode"])
				if mode == "ask" && (params["approvalPolicy"] != "on-request" || obj(params["sandboxPolicy"])["type"] != "workspaceWrite") {
					t.Error("wrong ask policy", params)
				}
				if mode == "full" && (params["approvalPolicy"] != "never" || obj(params["sandboxPolicy"])["type"] != "dangerFullAccess") {
					t.Error("wrong full access policy", params)
				}
				rpcReply(conn, req, M{"turn": M{"id": "ours"}})
				start(conn)
			case "turn/interrupt":
				if obj(req["params"])["turnId"] != "ours" {
					t.Error("interrupted another turn")
				}
				rpcReply(conn, req, M{})
				notify(conn, "turn/completed", M{"threadId": "native-session", "turn": M{"id": "ours", "status": "interrupted"}})
			}
		}
	})
}

func TestCodexApprovalsRoundTripAndKeepStreaming(t *testing.T) {
	for _, decision := range []string{"accept", "decline"} {
		t.Run(decision, func(t *testing.T) {
			task := sharedTask(t)
			obj(task.job["payload"])["permission_mode"] = "ask"
			registered := make(chan struct{}, 3)
			responses := make(chan M, 3)
			var released int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer fixture-device" {
					t.Error("missing device auth")
				}
				if strings.HasSuffix(r.URL.Path, "/expire") {
					json.NewEncoder(w).Encode(M{"ok": true})
					return
				}
				if r.Method == "POST" {
					var body M
					json.NewDecoder(r.Body).Decode(&body)
					if obj(body["details"])["threadId"] != "native-session" {
						t.Error("wrong thread")
					}
					registered <- struct{}{}
				}
				value := "pending"
				if atomic.LoadInt32(&released) == 1 {
					value = decision
				}
				json.NewEncoder(w).Encode(M{"decision": value})
			}))
			defer api.Close()
			task.client, _ = NewClient(api.URL, "fixture-device")
			count := 0
			permissionFixture(t, task, func(conn *websocket.Conn) {
				for i, method := range []string{"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"} {
					conn.WriteJSON(M{"id": 900 + i, "method": method, "params": M{"threadId": "native-session", "turnId": "ours", "itemId": "item", "permissions": M{"network": M{"enabled": true}}}})
				}
				// Another client's request must not reach our webpage or receive a reply.
				conn.WriteJSON(M{"id": 999, "method": "item/commandExecution/requestApproval", "params": M{"threadId": "native-session", "turnId": "other"}})
				notify(conn, "item/agentMessage/delta", M{"threadId": "native-session", "turnId": "ours", "delta": "still streaming"})
			}, func(conn *websocket.Conn, reply M) {
				if num(reply["id"]) == 999 {
					t.Error("answered another client's request")
				}
				responses <- reply
				count++
				if count == 3 {
					notify(conn, "turn/completed", M{"threadId": "native-session", "turn": M{"id": "ours", "status": "completed"}})
				}
			})
			go task.run(nil, true)
			for i := 0; i < 3; i++ {
				select {
				case <-registered:
				case <-time.After(4 * time.Second):
					task.cancel()
					t.Fatal("approval was not registered concurrently")
				}
			}
			deadline := time.Now().Add(time.Second)
			for task.progress()["output"] != "still streaming" && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			if task.progress()["output"] != "still streaming" {
				task.cancel()
				t.Fatal("pending approval blocked streaming")
			}
			atomic.StoreInt32(&released, 1)
			waitTask(t, task)
			if task.result["status"] != "succeeded" {
				t.Fatal(task.result)
			}
			for i := 0; i < 3; i++ {
				reply := <-responses
				result := obj(reply["result"])
				if num(reply["id"]) == 902 {
					if result["scope"] != "turn" {
						t.Fatal("permission grant escaped the turn", result)
					}
					if (len(obj(result["permissions"])) > 0) != (decision == "accept") {
						t.Fatal("permission decision ignored", result)
					}
				} else if result["decision"] != decision {
					t.Fatal("browser decision ignored", reply)
				}
			}
		})
	}
}

func TestCodexPendingApprovalCanBeCancelled(t *testing.T) {
	task := sharedTask(t)
	obj(task.job["payload"])["permission_mode"] = "ask"
	registered := make(chan struct{}, 1)
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" && !strings.HasSuffix(r.URL.Path, "/expire") {
			registered <- struct{}{}
		}
		json.NewEncoder(w).Encode(M{"decision": "pending"})
	}))
	defer api.Close()
	task.client, _ = NewClient(api.URL, "fixture-device")
	permissionFixture(t, task, func(conn *websocket.Conn) {
		conn.WriteJSON(M{"id": 900, "method": "item/commandExecution/requestApproval", "params": M{"threadId": "native-session", "turnId": "ours"}})
	}, func(_ *websocket.Conn, reply M) { t.Error("responded after cancellation", reply) })
	go task.run(nil, true)
	select {
	case <-registered:
	case <-time.After(4 * time.Second):
		task.cancel()
		t.Fatal("approval did not arrive")
	}
	task.cancel()
	waitTask(t, task)
	if task.result["status"] != "cancelled" {
		t.Fatal(task.result)
	}
}

func TestCodexFullAccessAndLocalPermissionLimit(t *testing.T) {
	task := sharedTask(t)
	obj(task.job["payload"])["permission_mode"] = "full"
	permissionFixture(t, task, func(conn *websocket.Conn) {
		notify(conn, "turn/completed", M{"threadId": "native-session", "turn": M{"id": "ours", "status": "completed"}})
	}, func(_ *websocket.Conn, _ M) { t.Error("unexpected approval") })
	go task.run(nil, true)
	waitTask(t, task)
	if task.result["status"] != "succeeded" {
		t.Fatal(task.result)
	}
	for _, mode := range []string{"full", "ask"} {
		blocked := sharedTask(t)
		obj(blocked.job["payload"])["permission_mode"] = mode
		blocked.run(nil, false)
		if blocked.result["status"] != "failed" || !strings.Contains(str(blocked.result["error"]), "allow-agent-writes") {
			t.Fatal("browser bypassed local device limit", blocked.result)
		}
	}
}
