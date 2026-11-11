package relay

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestCodexNativeTitleAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CODEX_TEST") != "1" {
		t.Skip("loopback-only opt-in Codex title check")
	}
	dir := t.TempDir()
	t.Setenv("CODEX_HOME", dir)
	var calls int32
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/responses") {
			http.Error(w, "fixture only", 404)
			return
		}
		var input M
		json.NewDecoder(r.Body).Decode(&input)
		atomic.AddInt32(&calls, 1)
		text := "normal fixture reply"
		encoded, _ := json.Marshal(input)
		if strings.Contains(string(encoded), "descriptive conversation title") {
			text = `{"title":"修复登录页面"}`
		}
		w.Header().Set("Content-Type", "text/event-stream")
		item := M{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed", "content": []M{{"type": "output_text", "text": text, "annotations": []M{}}}}
		for _, event := range []M{
			{"type": "response.created", "response": M{"id": "resp_fixture", "status": "in_progress", "output": []M{}}},
			{"type": "response.output_item.added", "output_index": 0, "item": M{"type": "message", "id": "msg_fixture", "role": "assistant", "content": []M{}}},
			{"type": "response.output_text.delta", "item_id": "msg_fixture", "output_index": 0, "content_index": 0, "delta": text},
			{"type": "response.output_item.done", "output_index": 0, "item": item},
			{"type": "response.completed", "response": M{"id": "resp_fixture", "status": "completed", "output": []M{item}, "usage": M{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
		} {
			b, _ := json.Marshal(event)
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event["type"], b)
		}
	}))
	defer model.Close()
	config := `model="fixture"
model_provider="relay_fixture"
approval_policy="never"
sandbox_mode="read-only"
[model_providers.relay_fixture]
name="fixture"
wire_api="responses"
requires_openai_auth=false
base_url="` + model.URL + `/v1"
`
	os.WriteFile(filepath.Join(dir, "config.toml"), []byte(config), 0600)
	task := newTask(M{"id": "title-fixture", "kind": "prompt", "agent": M{"kind": "codex", "origin": "relay", "cwd": dir}, "payload": M{"prompt": "请检查并修复这个项目中登录页面的错误，说明造成错误的具体原因。", "timeout": 25}})
	task.nativeTitle = true
	go task.run(nil, false)
	waitTask(t, task)
	if task.result["status"] != "succeeded" || task.result["output"] != "normal fixture reply\n" {
		t.Fatal(task.result)
	}
	index, err := os.ReadFile(filepath.Join(dir, "session_index.jsonl"))
	if err != nil {
		t.Fatal(err, task.result)
	}
	if !strings.Contains(string(index), `"thread_name":"修复登录页面"`) || !strings.Contains(string(index), str(task.result["session_id"])) {
		t.Fatal("native title was not persisted", string(index))
	}
	files := 0
	filepath.WalkDir(filepath.Join(dir, "sessions"), func(path string, entry os.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && strings.HasSuffix(path, ".jsonl") {
			files++
		}
		return nil
	})
	if files != 1 {
		t.Fatal("title helper left a visible transcript", files)
	}
	if atomic.LoadInt32(&calls) != 2 {
		t.Fatal("unexpected title calls", calls)
	}
}

func TestClaudeNativeTitleAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CLAUDE_TEST") != "1" {
		t.Skip("loopback-only opt-in Claude native title check")
	}
	for _, mode := range []string{"device", "ask", "full"} {
		t.Run(mode, func(t *testing.T) { checkClaudeNativeTitle(t, mode) })
	}
}

func checkClaudeNativeTitle(t *testing.T, mode string) {
	dir := t.TempDir()
	config := filepath.Join(dir, "config")
	os.Mkdir(config, 0700)
	var calls int32
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/messages/count_tokens") {
			fmt.Fprint(w, `{"input_tokens":1}`)
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/messages") {
			http.Error(w, "fixture only", 404)
			return
		}
		atomic.AddInt32(&calls, 1)
		claudeFixtureResponse(w, []M{{"type": "text", "text": `{"title":"修复登录页面"}`}}, "end_turn")
	}))
	defer model.Close()
	t.Setenv("CLAUDE_CONFIG_DIR", config)
	t.Setenv("ANTHROPIC_BASE_URL", model.URL)
	t.Setenv("ANTHROPIC_API_KEY", "loopback-fixture-key")
	t.Setenv("ANTHROPIC_AUTH_TOKEN", "")
	t.Setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
	writePrivate(filepath.Join(config, "settings.json"), M{"env": M{"ANTHROPIC_BASE_URL": model.URL, "ANTHROPIC_API_KEY": "loopback-fixture-key"}, "disableAllHooks": true})
	task := newTask(M{"id": "title-fixture", "kind": "prompt", "agent": M{"kind": "claude", "origin": "relay", "cwd": dir}, "payload": M{"prompt": "请检查并修复这个项目中登录页面的错误，说明造成错误的具体原因。", "timeout": 30, "permission_mode": mode}})
	task.nativeTitle = true
	go task.run(nil, mode != "device")
	waitTask(t, task)
	if task.result["status"] != "succeeded" {
		t.Fatal(task.result)
	}
	found := false
	filepath.WalkDir(filepath.Join(config, "projects"), func(path string, entry os.DirEntry, err error) error {
		if err == nil && !entry.IsDir() && strings.HasSuffix(path, ".jsonl") {
			data, _ := os.ReadFile(path)
			for _, line := range strings.Split(string(data), "\n") {
				var item M
				if json.Unmarshal([]byte(line), &item) == nil && item["type"] == "ai-title" && item["aiTitle"] == "修复登录页面" {
					found = true
				}
			}
		}
		return nil
	})
	if !found {
		t.Fatal("Claude did not persist its native AI title", task.result, "model calls", calls)
	}
}
