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
	"sync"
	"testing"
)

// Opt-in protocol check with the installed Claude binary. All model requests
// use a loopback fixture and all file operations stay in a temporary project.
func TestClaudeAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CLAUDE_TEST") != "1" {
		t.Skip("set RELAY_CLAUDE_TEST=1 to check the installed CLI with a loopback model fixture")
	}
	if _, err := exec.LookPath("claude"); err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"read-only", "writable", "resume-with-writes", "local-deny"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			config := filepath.Join(dir, "config")
			cwd := filepath.Join(dir, "project")
			os.MkdirAll(config, 0700)
			os.MkdirAll(cwd, 0700)
			var mu sync.Mutex
			var requests []M
			model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/messages/count_tokens") {
					w.Header().Set("Content-Type", "application/json")
					fmt.Fprint(w, `{"input_tokens":100}`)
					return
				}
				if !strings.HasSuffix(r.URL.Path, "/messages") {
					http.Error(w, "loopback model fixture only", 404)
					return
				}
				var input M
				if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
					t.Error(err)
				}
				mu.Lock()
				requests = append(requests, input)
				step := len(requests)
				mu.Unlock()
				if (step == 1 && (mode == "writable" || mode == "local-deny")) || (step == 2 && mode == "resume-with-writes") {
					claudeFixtureResponse(w, []M{
						{"type": "tool_use", "id": "tool_bash", "name": "Bash", "input": M{"command": `python3 -c 'from pathlib import Path; Path("bash-ran").write_text("python works")'`, "description": "Run a temporary Python fixture"}},
						{"type": "tool_use", "id": "tool_write", "name": "Write", "input": M{"file_path": filepath.Join(cwd, "write-ran"), "content": "write works"}},
					}, "tool_use")
				} else {
					claudeFixtureResponse(w, []M{{"type": "text", "text": "loopback Claude fixture complete"}}, "end_turn")
				}
			}))
			defer model.Close()
			settings := M{"env": M{"ANTHROPIC_BASE_URL": model.URL, "ANTHROPIC_API_KEY": "loopback-fixture-key", "ANTHROPIC_MODEL": "claude-sonnet-4-6"}, "enableAllProjectMcpServers": true}
			if mode == "local-deny" {
				settings["permissions"] = M{"deny": []string{"Bash", "Write"}}
			}
			if err := writePrivate(filepath.Join(config, "settings.json"), settings); err != nil {
				t.Fatal(err)
			}
			// A real local MCP server proves writable launches no longer erase
			// the user's MCP configuration. It performs no external I/O.
			mcpScript := filepath.Join(dir, "mcp.py")
			os.WriteFile(mcpScript, []byte(`import json,sys
for line in sys.stdin:
 try:
  request=json.loads(line)
  if 'id' not in request: continue
  method=request.get('method')
  if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'relay-fixture','version':'1'}}
  elif method=='tools/list': result={'tools':[{'name':'fixture_echo','description':'Local fixture tool','inputSchema':{'type':'object','properties':{}}}]}
  elif method=='tools/call': result={'content':[{'type':'text','text':'fixture'}]}
  else: result={}
  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
 except (ValueError,KeyError): pass
`), 0600)
			python, err := exec.LookPath("python3")
			if err != nil {
				t.Fatal(err)
			}
			writePrivate(filepath.Join(cwd, ".mcp.json"), M{"mcpServers": M{"relay_fixture": M{"command": python, "args": []string{mcpScript}}}})
			t.Setenv("CLAUDE_CONFIG_DIR", config)
			t.Setenv("HOME", dir)
			t.Setenv("ANTHROPIC_BASE_URL", model.URL)
			t.Setenv("ANTHROPIC_API_KEY", "loopback-fixture-key")
			t.Setenv("ANTHROPIC_AUTH_TOKEN", "")
			t.Setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
			t.Setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
			t.Setenv("DISABLE_TELEMETRY", "1")
			t.Setenv("DISABLE_ERROR_REPORTING", "1")
			task := newTask(M{"id": "claude-fixture", "kind": "prompt", "agent": M{"kind": "claude", "cwd": cwd}, "payload": M{"prompt": "run the loopback fixture", "timeout": 30}})
			defer task.cancel()
			task.run(nil, mode != "read-only" && mode != "resume-with-writes")
			if task.result["status"] != "succeeded" || !strings.Contains(str(task.result["output"]), "loopback Claude fixture complete") {
				t.Fatal(task.result)
			}
			if mode == "resume-with-writes" {
				sid := str(task.result["session_id"])
				if sid == "" {
					t.Fatal("native session ID missing")
				}
				resumed := newTask(M{"id": "claude-resume-fixture", "kind": "prompt", "agent": M{"kind": "claude", "cwd": cwd, "session_id": sid}, "payload": M{"prompt": "run the Python fixture now that tools are enabled", "timeout": 30}})
				defer resumed.cancel()
				resumed.run(nil, true)
				if resumed.result["status"] != "succeeded" || resumed.result["session_id"] != sid {
					t.Fatal("original read-only session did not resume with execution tools", resumed.result)
				}
			}
			mu.Lock()
			inputs := append([]M(nil), requests...)
			mu.Unlock()
			if len(inputs) == 0 {
				t.Fatal("Claude did not call the loopback model")
			}
			tools := map[string]bool{}
			input := inputs[0]
			if mode == "resume-with-writes" {
				if len(inputs) != 3 {
					t.Fatalf("expected original turn and tool continuation: %d requests", len(inputs))
				}
				input = inputs[1]
			}
			for _, tool := range arr(input["tools"]) {
				tools[str(obj(tool)["name"])] = true
			}
			if mode == "writable" || mode == "resume-with-writes" {
				if !tools["Bash"] || !tools["Write"] || !tools["mcp__relay_fixture__fixture_echo"] {
					t.Fatalf("native tools or configured MCP missing: %v", tools)
				}
				for file, want := range map[string]string{"bash-ran": "python works", "write-ran": "write works"} {
					got, err := os.ReadFile(filepath.Join(cwd, file))
					if err != nil || string(got) != want {
						t.Fatalf("%s was not executed: %s %v", file, got, err)
					}
				}
			} else {
				for _, file := range []string{"bash-ran", "write-ran"} {
					if _, err := os.Stat(filepath.Join(cwd, file)); !os.IsNotExist(err) {
						t.Fatalf("%s ignored the %s policy", file, mode)
					}
				}
				if mode == "read-only" && (tools["Bash"] || tools["Write"] || tools["mcp__relay_fixture__fixture_echo"]) {
					t.Fatal("read-only mode exposed execution tools", tools)
				}
			}
			t.Logf("Installed Claude: %s tools and permissions passed with %d loopback model requests", mode, len(inputs))
		})
	}
}

func claudeFixtureResponse(w http.ResponseWriter, blocks []M, stop string) {
	w.Header().Set("Content-Type", "text/event-stream")
	send := func(event M) {
		data, _ := json.Marshal(event)
		fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event["type"], data)
	}
	send(M{"type": "message_start", "message": M{"id": "msg_relay_fixture", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6", "content": []M{}, "stop_reason": nil, "stop_sequence": nil, "usage": M{"input_tokens": 100, "output_tokens": 0}}})
	for index, block := range blocks {
		if block["type"] == "text" {
			send(M{"type": "content_block_start", "index": index, "content_block": M{"type": "text", "text": ""}})
			send(M{"type": "content_block_delta", "index": index, "delta": M{"type": "text_delta", "text": block["text"]}})
		} else {
			send(M{"type": "content_block_start", "index": index, "content_block": M{"type": "tool_use", "id": block["id"], "name": block["name"], "input": M{}}})
			input, _ := json.Marshal(block["input"])
			send(M{"type": "content_block_delta", "index": index, "delta": M{"type": "input_json_delta", "partial_json": string(input)}})
		}
		send(M{"type": "content_block_stop", "index": index})
	}
	send(M{"type": "message_delta", "delta": M{"stop_reason": stop, "stop_sequence": nil}, "usage": M{"output_tokens": 20}})
	send(M{"type": "message_stop"})
}
