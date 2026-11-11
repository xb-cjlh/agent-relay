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
	"sync/atomic"
	"testing"
	"time"
)

// A real Codex holds a model response open while Relay delivers another input.
// Every model request targets loopback, with an isolated home and no API keys.
func TestCodexInFlightInputAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CODEX_TEST") != "1" {
		t.Skip("set RELAY_CODEX_TEST=1 for the loopback-only in-flight delivery check")
	}
	binary, err := exec.LookPath("codex")
	if override := os.Getenv("RELAY_CODEX_TEST_BINARY"); override != "" {
		binary, err = exec.LookPath(override)
	}
	if err != nil {
		t.Fatal(err)
	}
	bin := t.TempDir()
	if err = os.Symlink(binary, filepath.Join(bin, "codex")); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	dir, err := os.MkdirTemp("/tmp", "relay-steer-real-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	t.Setenv("CODEX_HOME", dir)
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	unlock := func() { once.Do(func() { close(release) }) }
	var calls int32
	var inputMu sync.Mutex
	var seenInputs []string
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/responses") {
			http.Error(w, "loopback fixture only", 404)
			return
		}
		var input M
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			t.Error(err)
			return
		}
		data, _ := json.Marshal(input)
		inputMu.Lock()
		seenInputs = append(seenInputs, string(data))
		inputMu.Unlock()
		n := atomic.AddInt32(&calls, 1)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "event: response.created\ndata: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_fixture\",\"status\":\"in_progress\",\"output\":[]}}\n\n")
		w.(http.Flusher).Flush()
		if n == 1 {
			close(started)
		}
		select {
		case <-release:
		case <-r.Context().Done():
			return
		}
		item := M{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed", "content": []M{{"type": "output_text", "text": "fixture completed", "annotations": []M{}}}}
		for _, event := range []M{
			{"type": "response.output_item.added", "output_index": 0, "item": M{"type": "message", "id": "msg_fixture", "role": "assistant", "content": []M{}}},
			{"type": "response.output_text.delta", "item_id": "msg_fixture", "output_index": 0, "content_index": 0, "delta": "fixture completed"},
			{"type": "response.output_item.done", "output_index": 0, "item": item},
			{"type": "response.completed", "response": M{"id": "resp_fixture", "status": "completed", "output": []M{item}, "usage": M{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
		} {
			data, _ := json.Marshal(event)
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event["type"], data)
		}
	}))
	defer model.Close()
	defer unlock()
	config := `model = "fixture"
model_provider = "relay_fixture"
approval_policy = "never"
sandbox_mode = "read-only"
[model_providers.relay_fixture]
name = "Relay fixture"
wire_api = "responses"
requires_openai_auth = false
base_url = "` + model.URL + `/v1"
`
	if err = os.WriteFile(filepath.Join(dir, "config.toml"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	original := newTask(M{"id": "original", "kind": "prompt", "agent": M{"kind": "codex", "id": "agent", "cwd": dir}, "payload": M{"prompt": "original fixture input", "timeout": 30}})
	defer func() {
		original.cancel()
		unlock()
		select {
		case <-original.done:
		case <-time.After(6 * time.Second):
		}
	}()
	go original.run(nil, false)
	select {
	case <-started:
	case <-original.done:
		t.Fatal(original.result)
	case <-time.After(15 * time.Second):
		t.Fatal("Codex did not reach loopback fixture")
	}
	deadline := time.Now().Add(3 * time.Second)
	for original.codexControl() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if original.codexControl() == "" {
		t.Fatal("running task did not advertise control transport")
	}
	progress := original.progress()
	extra := newTask(M{"id": "extra", "kind": "prompt", "steer_only": true, "agent": M{"kind": "codex", "id": "agent", "cwd": dir, "session_id": progress["session_id"]}, "payload": M{"prompt": "new in-flight fixture instruction", "permission_mode": "full", "timeout": 10}})
	extra.codexOverride = original.codexControl()
	go extra.run(nil, true)
	waitTask(t, extra)
	if extra.result["status"] != "succeeded" || !yes(extra.result["steered"]) || extra.result["output"] != "" {
		t.Fatal(extra.result)
	}
	select {
	case <-original.done:
		t.Fatal("delivery waited for the original turn to finish", original.result)
	default:
	}
	unlock()
	waitTask(t, original)
	if original.result["status"] != "succeeded" {
		t.Fatal(original.result)
	}
	inputMu.Lock()
	combined := strings.Join(seenInputs, "\n")
	inputMu.Unlock()
	if !strings.Contains(combined, "new in-flight fixture instruction") {
		t.Fatal("steered input was not consumed by the model fixture")
	}
	if !strings.Contains(combined, "read-only") {
		t.Fatal("in-flight input lost the running turn's permissions")
	}
	t.Log("Real Codex accepted and consumed additional input before the active turn completed; original read-only permissions retained, loopback model only")
}
