package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image"
	"image/png"
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

// Optional compatibility smoke against the installed Codex binary. Every model
// request goes to this loopback fixture; no OpenAI credentials or paid calls.
func TestSharedCodexAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CODEX_TEST") != "1" {
		t.Skip("set RELAY_CODEX_TEST=1 to check the installed CLI with a loopback model fixture")
	}
	binary, err := exec.LookPath("codex")
	if override := os.Getenv("RELAY_CODEX_TEST_BINARY"); override != "" {
		binary, err = exec.LookPath(override)
	}
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp("/tmp", "relay-real-codex-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	t.Setenv("CODEX_HOME", dir)
	var calls int32
	inputs := make(chan M, 4)
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/responses") {
			http.Error(w, "fixture endpoint only", 404)
			return
		}
		atomic.AddInt32(&calls, 1)
		var input M
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			t.Error(err)
		}
		inputs <- input
		w.Header().Set("Content-Type", "text/event-stream")
		item := M{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed", "content": []M{{"type": "output_text", "text": "shared daemon fixture answer", "annotations": []M{}}}}
		for _, event := range []M{
			{"type": "response.created", "response": M{"id": "resp_fixture", "status": "in_progress", "output": []M{}}},
			{"type": "response.output_item.added", "output_index": 0, "item": M{"type": "message", "id": "msg_fixture", "role": "assistant", "content": []M{}}},
			{"type": "response.output_text.delta", "item_id": "msg_fixture", "output_index": 0, "content_index": 0, "delta": "shared daemon fixture answer"},
			{"type": "response.output_item.done", "output_index": 0, "item": item},
			{"type": "response.completed", "response": M{"id": "resp_fixture", "status": "completed", "output": []M{item}, "usage": M{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
		} {
			b, _ := json.Marshal(event)
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event["type"], b)
		}
	}))
	defer model.Close()
	config := `model = "fixture"
model_provider = "relay_fixture"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[model_providers.relay_fixture]
name = "Relay fixture"
wire_api = "responses"
requires_openai_auth = false
base_url = "` + model.URL + `/v1"
`
	os.WriteFile(filepath.Join(dir, "config.toml"), []byte(config), 0600)
	path := codexSocket()
	os.MkdirAll(filepath.Dir(path), 0700)
	log, err := os.Create(filepath.Join(dir, "app-server.log"))
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	process := exec.Command(binary, "app-server", "--listen", "unix://"+path)
	process.Stdout = log
	process.Stderr = log
	if err = process.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		process.Process.Kill()
		process.Wait()
		if t.Failed() {
			b, _ := os.ReadFile(log.Name())
			t.Log(string(b))
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	var rpc *codexRPC
	for {
		rpc, err = dialCodex(ctx)
		if err == nil {
			break
		}
		if ctx.Err() != nil {
			t.Fatal(err)
		}
		time.Sleep(100 * time.Millisecond)
	}
	defer rpc.close()
	thread, err := rpc.call(ctx, "thread/start", M{"cwd": dir, "approvalPolicy": "never", "sandbox": "danger-full-access"})
	if err != nil {
		t.Fatal(err)
	}
	sid := str(obj(thread["thread"])["id"])
	if sid == "" {
		t.Fatal(thread)
	}
	seed, err := rpc.call(ctx, "turn/start", M{"threadId": sid, "input": []M{{"type": "text", "text": "create fixture history"}}})
	if err != nil {
		t.Fatal(err)
	}
	seedTask := newTask(M{})
	if _, err = seedTask.followCodexTurn(ctx, rpc, sid, str(obj(seed["turn"])["id"])); err != nil {
		t.Fatal(err)
	}
	// Reproduce the reported failure before testing the shared-owner path.
	conflict := exec.CommandContext(ctx, binary, "exec", "resume", "--json", "--skip-git-repo-check", sid, "-")
	conflict.Stdin = strings.NewReader("fixture continuation")
	conflict.Dir = dir
	output, execErr := conflict.CombinedOutput()
	if execErr == nil || !strings.Contains(string(output), "active writer") {
		t.Fatalf("expected second-writer conflict: %v %s", execErr, output)
	}
	if atomic.LoadInt32(&calls) != 1 {
		t.Fatal("standalone resume unexpectedly called the model")
	}
	task := newTask(M{"id": "real-fixture-job", "kind": "prompt", "agent": M{"kind": "codex", "session_id": sid, "cwd": dir}, "payload": M{"prompt": "fixture continuation", "timeout": 15}})
	var imageBytes bytes.Buffer
	png.Encode(&imageBytes, image.NewRGBA(image.Rect(0, 0, 2, 2)))
	imagePath := filepath.Join(dir, "fixture.png")
	os.WriteFile(imagePath, imageBytes.Bytes(), 0600)
	task.images = []taskImage{{path: imagePath, mime: "image/png"}}
	go task.run(nil, false)
	waitTask(t, task)
	if task.result["status"] != "succeeded" || !strings.Contains(str(task.result["output"]), "shared daemon fixture answer") {
		t.Fatal(task.result)
	}
	if atomic.LoadInt32(&calls) != 2 {
		t.Fatal("model request was repeated", atomic.LoadInt32(&calls))
	}
	<-inputs // Original native turn has full access.
	remoteInput, _ := json.Marshal(<-inputs)
	if !strings.Contains(string(remoteInput), "read-only") {
		t.Fatalf("remote turn did not receive read-only permission instructions: %s", remoteInput)
	}
	if !strings.Contains(string(remoteInput), "input_image") || !strings.Contains(string(remoteInput), "data:image/png;base64,") {
		t.Fatal("native image input did not reach the loopback model")
	}
	settings, err := rpc.call(ctx, "thread/resume", M{"threadId": sid, "excludeTurns": true})
	if err != nil {
		t.Fatal(err)
	}
	if obj(settings["sandbox"])["type"] != "dangerFullAccess" {
		t.Fatal("native sandbox was not restored", settings["sandbox"])
	}
	t.Log("Real Codex: reproduced active-writer failure, continued original loaded thread once via shared service, restored native permissions; model endpoint was loopback-only")
}

func TestCodexExecImageAcrossTurnsAgainstRealCLI(t *testing.T) {
	if os.Getenv("RELAY_CODEX_TEST") != "1" {
		t.Skip("set RELAY_CODEX_TEST=1 for the loopback-only Codex image continuation check")
	}
	binary, err := exec.LookPath("codex")
	if override := os.Getenv("RELAY_CODEX_TEST_BINARY"); override != "" {
		binary, err = exec.LookPath(override)
	}
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	t.Setenv("CODEX_HOME", filepath.Join(dir, "codex"))
	bin := filepath.Join(dir, "bin")
	if err = os.Mkdir(bin, 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.Symlink(binary, filepath.Join(bin, "codex")); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	requests := make(chan []byte, 4)
	var calls int32
	model := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/responses") {
			http.Error(w, "loopback fixture only", 404)
			return
		}
		atomic.AddInt32(&calls, 1)
		var input M
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			t.Error(err)
		}
		encoded, _ := json.Marshal(input)
		requests <- encoded
		w.Header().Set("Content-Type", "text/event-stream")
		item := M{"type": "message", "id": "msg_image_fixture", "role": "assistant", "status": "completed", "content": []M{{"type": "output_text", "text": "image fixture complete", "annotations": []M{}}}}
		for _, event := range []M{
			{"type": "response.created", "response": M{"id": "resp_image_fixture", "status": "in_progress", "output": []M{}}},
			{"type": "response.output_item.added", "output_index": 0, "item": M{"type": "message", "id": "msg_image_fixture", "role": "assistant", "content": []M{}}},
			{"type": "response.output_text.delta", "item_id": "msg_image_fixture", "output_index": 0, "content_index": 0, "delta": "image fixture complete"},
			{"type": "response.output_item.done", "output_index": 0, "item": item},
			{"type": "response.completed", "response": M{"id": "resp_image_fixture", "status": "completed", "output": []M{item}, "usage": M{"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
		} {
			encoded, _ := json.Marshal(event)
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event["type"], encoded)
		}
	}))
	defer model.Close()
	config := `model = "fixture"
model_provider = "relay_fixture"
approval_policy = "never"
sandbox_mode = "read-only"
[model_providers.relay_fixture]
name = "Relay image fixture"
wire_api = "responses"
requires_openai_auth = false
base_url = "` + model.URL + `/v1"
`
	if err = os.MkdirAll(os.Getenv("CODEX_HOME"), 0700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(os.Getenv("CODEX_HOME"), "config.toml"), []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	var encoded bytes.Buffer
	png.Encode(&encoded, image.NewRGBA(image.Rect(0, 0, 2, 2)))
	data, checksum := encoded.Bytes(), hashBytes(encoded.Bytes())
	client := imageFixtureClient("fixture-device", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-SHA256", checksum)
		w.Write(data)
	})
	first := newTask(M{"kind": "prompt", "agent": M{"kind": "codex", "cwd": dir}, "payload": M{"prompt": "fixture first image", "timeout": 15, "images": []interface{}{M{"id": "img_fixture", "sha256": checksum, "mime": "image/png"}}}})
	first.client, first.imageDir = client, filepath.Join(dir, "device", "images")
	wait := func(task *Task) {
		t.Helper()
		select {
		case <-task.done:
		case <-time.After(25 * time.Second):
			task.cancel()
			t.Fatal("real CLI task did not finish", task.progress(), "fixture requests", atomic.LoadInt32(&calls))
		}
	}
	defer first.cancel()
	go first.run(nil, false)
	wait(first)
	if first.result["status"] != "succeeded" || str(first.result["session_id"]) == "" {
		t.Fatal("first image turn failed", first.result)
	}
	path := first.images[0].path
	if saved, err := os.ReadFile(path); err != nil || !bytes.Equal(saved, data) {
		t.Fatal("original lost after first turn", err)
	}
	second := newTask(M{"kind": "prompt", "agent": M{"kind": "codex", "cwd": dir, "session_id": first.result["session_id"]}, "payload": M{"prompt": "fixture text-only continuation", "timeout": 15}})
	defer second.cancel()
	go second.run(nil, false)
	wait(second)
	if second.result["status"] != "succeeded" || second.result["session_id"] != first.result["session_id"] {
		t.Fatal("image session continuation failed", second.result)
	}
	if atomic.LoadInt32(&calls) != 2 {
		t.Fatal("unexpected model request count", atomic.LoadInt32(&calls))
	}
	for index := 0; index < 2; index++ {
		input := string(<-requests)
		if !strings.Contains(input, "input_image") || !strings.Contains(input, "data:image/png;base64,") || !strings.Contains(input, path) {
			t.Fatal("native image or retained path absent from model context on turn", index+1)
		}
		if strings.Contains(input, "Codex could not read the local image") {
			t.Fatal("stdin marker was parsed as another image")
		}
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("original lost after text-only continuation", err)
	}
	t.Log("Real Codex exec: native image and stable path survive standalone text-only resume; stdin marker is not parsed as an image; loopback model only")
}
