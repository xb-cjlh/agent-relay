package relay

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func realApprovalClient(t *testing.T) (*Client, <-chan M, *int32) {
	t.Helper()
	requests := make(chan M, 4)
	released := new(int32)
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/expire") {
			json.NewEncoder(w).Encode(M{"ok": true})
			return
		}
		if r.Method == "POST" {
			var body M
			json.NewDecoder(r.Body).Decode(&body)
			requests <- body
		}
		decision := "pending"
		if atomic.LoadInt32(released) == 1 {
			decision = "accept"
		} else if atomic.LoadInt32(released) == 2 {
			decision = "decline"
		}
		json.NewEncoder(w).Encode(M{"decision": decision})
	}))
	t.Cleanup(api.Close)
	client, _ := NewClient(api.URL, "fixture-device")
	return client, requests, released
}

func waitRealPermissionTask(t *testing.T, task *Task) {
	t.Helper()
	select {
	case <-task.done:
	case <-time.After(45 * time.Second):
		task.cancel()
		<-task.done
		t.Fatal("real CLI did not finish", task.result)
	}
	if task.result["status"] != "succeeded" {
		t.Fatal(task.result)
	}
}

func TestClaudePermissionsAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CLAUDE_TEST") != "1" {
		t.Skip("set RELAY_CLAUDE_TEST=1 for loopback-only permission checks")
	}
	if _, err := exec.LookPath("claude"); err != nil {
		t.Fatal(err)
	}
	for _, decision := range []string{"accept", "decline", "full"} {
		t.Run(decision, func(t *testing.T) {
			dir := t.TempDir()
			config, cwd := filepath.Join(dir, "config"), filepath.Join(dir, "project")
			os.MkdirAll(config, 0700)
			os.MkdirAll(cwd, 0700)
			file := filepath.Join(cwd, "approved-file")
			var calls int32
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/messages/count_tokens") {
					fmt.Fprint(w, `{"input_tokens":100}`)
					return
				}
				if !strings.HasSuffix(r.URL.Path, "/messages") {
					http.Error(w, "loopback fixture only", 404)
					return
				}
				if atomic.AddInt32(&calls, 1) == 1 {
					claudeFixtureResponse(w, []M{{"type": "tool_use", "id": "tool_approval", "name": "Bash", "input": M{"command": "echo approved > approved-file", "description": "Write only a temporary fixture file"}}}, "tool_use")
				} else {
					claudeFixtureResponse(w, []M{{"type": "text", "text": "permission fixture complete"}}, "end_turn")
				}
			}))
			defer model.Close()
			settings := M{}
			if decision != "full" {
				settings["permissions"] = M{"ask": []string{"Bash"}}
			}
			writePrivate(filepath.Join(config, "settings.json"), settings)
			t.Setenv("CLAUDE_CONFIG_DIR", config)
			t.Setenv("ANTHROPIC_BASE_URL", model.URL)
			t.Setenv("ANTHROPIC_API_KEY", "loopback-fixture-key")
			t.Setenv("ANTHROPIC_AUTH_TOKEN", "")
			t.Setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
			t.Setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
			t.Setenv("DISABLE_TELEMETRY", "1")
			t.Setenv("DISABLE_ERROR_REPORTING", "1")
			mode := "ask"
			if decision == "full" {
				mode = "full"
			}
			task := newTask(M{"id": "claude-permission-fixture", "kind": "prompt", "agent": M{"kind": "claude", "cwd": cwd}, "payload": M{"prompt": "permission fixture", "timeout": 30, "permission_mode": mode}})
			defer task.cancel()
			client, pending, released := realApprovalClient(t)
			task.client = client
			go task.run(nil, true)
			if mode == "ask" {
				select {
				case request := <-pending:
					if request["method"] != "claude/tool/requestApproval" || obj(request["details"])["tool_name"] != "Bash" {
						t.Fatal("wrong native tool approval", request)
					}
				case <-task.done:
					t.Fatal("task ended before approval", task.result)
				case <-time.After(20 * time.Second):
					task.cancel()
					<-task.done
					t.Fatal("native Claude approval never arrived", task.result)
				}
				if _, err := os.Stat(file); !os.IsNotExist(err) {
					t.Fatal("tool ran before browser approval", err)
				}
				value := int32(1)
				if decision == "decline" {
					value = 2
				}
				atomic.StoreInt32(released, value)
			}
			waitRealPermissionTask(t, task)
			_, err := os.Stat(file)
			if (err == nil) != (decision != "decline") {
				t.Fatalf("native Claude ignored %s decision: %v", decision, err)
			}
			sid := str(task.result["session_id"])
			if sid == "" {
				t.Fatal("missing native session ID")
			}
			resumed := newTask(M{"id": "claude-permission-resume", "kind": "prompt", "agent": M{"kind": "claude", "cwd": cwd, "session_id": sid}, "payload": M{"prompt": "continue the same fixture", "timeout": 30, "permission_mode": mode}})
			defer resumed.cancel()
			resumed.client = client
			go resumed.run(nil, true)
			waitRealPermissionTask(t, resumed)
			if resumed.result["session_id"] != sid || atomic.LoadInt32(&calls) != 3 {
				t.Fatal("native conversation was not continued exactly once", resumed.result, calls)
			}
			if len(pending) != 0 {
				t.Fatal("unexpected extra browser approval")
			}
		})
	}
}

func TestCodexPermissionsAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CODEX_TEST") != "1" {
		t.Skip("set RELAY_CODEX_TEST=1 for loopback-only permission checks")
	}
	binary, err := exec.LookPath("codex")
	if override := os.Getenv("RELAY_CODEX_TEST_BINARY"); override != "" {
		binary, err = exec.LookPath(override)
	}
	if err != nil {
		t.Fatal(err)
	}
	for _, decision := range []string{"accept", "decline", "full"} {
		t.Run(decision, func(t *testing.T) {
			dir := t.TempDir()
			config, cwd, bin := filepath.Join(dir, "codex"), filepath.Join(dir, "project"), filepath.Join(dir, "bin")
			for _, path := range []string{config, cwd, bin} {
				os.MkdirAll(path, 0700)
			}
			os.Symlink(binary, filepath.Join(bin, "codex"))
			t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
			t.Setenv("CODEX_HOME", config)
			// An outside-project path distinguishes full access from workspace-write.
			file := filepath.Join(dir, "outside-project")
			var calls int32
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !strings.HasSuffix(r.URL.Path, "/responses") {
					http.Error(w, "loopback fixture only", 404)
					return
				}
				var input M
				json.NewDecoder(r.Body).Decode(&input)
				item := M{"type": "message", "id": "msg_permission", "role": "assistant", "status": "completed", "content": []M{{"type": "output_text", "text": "permission fixture complete", "annotations": []M{}}}}
				if atomic.AddInt32(&calls, 1) == 1 {
					name := "shell_command"
					for _, tool := range arr(input["tools"]) {
						if obj(tool)["name"] == "exec_command" {
							name = "exec_command"
						}
					}
					args := M{"command": "echo approved > '" + file + "'"}
					if name == "exec_command" {
						args = M{"cmd": args["command"]}
					}
					if decision != "full" {
						args["sandbox_permissions"], args["justification"] = "require_escalated", "Write only a temporary fixture file outside the project"
					}
					encoded, _ := json.Marshal(args)
					item = M{"type": "function_call", "id": "fc_permission", "call_id": "call_permission", "name": name, "arguments": string(encoded), "status": "completed"}
				}
				w.Header().Set("Content-Type", "text/event-stream")
				for _, event := range []M{
					{"type": "response.created", "response": M{"id": "resp_permission", "status": "in_progress", "output": []M{}}},
					{"type": "response.output_item.added", "output_index": 0, "item": item},
					{"type": "response.output_item.done", "output_index": 0, "item": item},
					{"type": "response.completed", "response": M{"id": "resp_permission", "status": "completed", "output": []M{item}, "usage": M{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
				} {
					encoded, _ := json.Marshal(event)
					fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event["type"], encoded)
				}
			}))
			defer model.Close()
			configText := `model = "fixture"
model_provider = "relay_fixture"
[model_providers.relay_fixture]
name = "Loopback fixture"
wire_api = "responses"
requires_openai_auth = false
base_url = "` + model.URL + `/v1"
`
			os.WriteFile(filepath.Join(config, "config.toml"), []byte(configText), 0600)
			mode := "ask"
			if decision == "full" {
				mode = "full"
			}
			task := newTask(M{"id": "codex-permission-fixture", "kind": "prompt", "agent": M{"kind": "codex", "cwd": cwd}, "payload": M{"prompt": "permission fixture", "timeout": 30, "permission_mode": mode}})
			defer task.cancel()
			client, pending, released := realApprovalClient(t)
			task.client = client
			go task.run(nil, true)
			if mode == "ask" {
				select {
				case request := <-pending:
					if request["method"] != "item/commandExecution/requestApproval" {
						t.Fatal("wrong native command approval", request)
					}
				case <-task.done:
					t.Fatal("task ended before approval", task.result)
				case <-time.After(20 * time.Second):
					task.cancel()
					<-task.done
					t.Fatal("native Codex approval never arrived", task.result)
				}
				if _, err := os.Stat(file); !os.IsNotExist(err) {
					t.Fatal("command ran before browser approval", err)
				}
				value := int32(1)
				if decision == "decline" {
					value = 2
				}
				atomic.StoreInt32(released, value)
			}
			waitRealPermissionTask(t, task)
			_, err := os.Stat(file)
			if (err == nil) != (decision != "decline") {
				t.Fatalf("native Codex ignored %s decision: %v", decision, err)
			}
			sid := str(task.result["session_id"])
			if sid == "" {
				t.Fatal("missing native session ID")
			}
			resumed := newTask(M{"id": "codex-permission-resume", "kind": "prompt", "agent": M{"kind": "codex", "cwd": cwd, "session_id": sid}, "payload": M{"prompt": "continue the same fixture", "timeout": 30, "permission_mode": mode}})
			defer resumed.cancel()
			resumed.client = client
			go resumed.run(nil, true)
			waitRealPermissionTask(t, resumed)
			if resumed.result["session_id"] != sid || atomic.LoadInt32(&calls) != 3 {
				t.Fatal("native conversation was not continued exactly once", resumed.result, calls)
			}
			if len(pending) != 0 {
				t.Fatal("unexpected extra browser approval")
			}
		})
	}
}
