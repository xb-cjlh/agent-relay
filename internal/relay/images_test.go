package relay

import (
	"bytes"
	"encoding/json"
	"image"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Exercise the HTTP client and headers without depending on socket access.
type imageFixtureTransport struct{ handler http.HandlerFunc }

func (transport imageFixtureTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	response := httptest.NewRecorder()
	transport.handler(response, request)
	return response.Result(), nil
}

func imageFixtureClient(token string, handler http.HandlerFunc) *Client {
	client, _ := NewClient("https://image-fixture.invalid", token)
	client.HTTP.Transport = imageFixtureTransport{handler}
	return client
}

func TestImageTransferVerifiesBytesUsesNativeInputsAndRetainsOriginals(t *testing.T) {
	var encoded bytes.Buffer
	png.Encode(&encoded, image.NewRGBA(image.Rect(0, 0, 2, 2)))
	data := encoded.Bytes()
	checksum := hashBytes(data)
	client := imageFixtureClient("device-only-token", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/device/images/img_fixture" || r.Header.Get("Authorization") != "Bearer device-only-token" {
			t.Error("wrong image transport credentials or resource")
		}
		w.Header().Set("X-SHA256", checksum)
		w.Write(data)
	})
	task := newTask(M{"agent": M{"kind": "codex", "session_id": "existing"}, "payload": M{"prompt": "look", "images": []interface{}{M{"id": "img_fixture", "sha256": checksum, "mime": "image/png"}}}})
	task.client = client
	task.imageDir = filepath.Join(t.TempDir(), "device", "images")
	err := task.prepareImages()
	if err != nil {
		t.Fatal(err)
	}
	path := task.images[0].path
	input := task.codexInput()
	if len(input) != 2 || input[1]["type"] != "localImage" || input[1]["path"] != path {
		t.Fatal(input)
	}
	args := task.imageArgs(commandFor(obj(task.job["agent"]), false), "codex")
	if strings.Join(args[len(args)-5:], "|") != "--image|"+path+"|--|existing|-" {
		t.Fatal(args)
	}
	var claude M
	if json.Unmarshal([]byte(task.agentInput("claude")), &claude) != nil {
		t.Fatal("missing structured image input")
	}
	source := obj(arr(obj(claude["message"])["content"])[0])["source"]
	if obj(source)["media_type"] != "image/png" || obj(source)["type"] != "base64" {
		t.Fatal(source)
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0600 {
		t.Fatal("retained image is not private")
	}
	if filepath.Dir(path) != task.imageDir || filepath.Base(path) != "img_fixture.png" {
		t.Fatal("image path is not stable within device storage", path)
	}
	if dir, err := os.Stat(task.imageDir); err != nil || dir.Mode().Perm() != 0700 {
		t.Fatal("image directory is not private", err)
	}
	task.job["payload"] = M{"images": []interface{}{M{"id": "img_fixture", "sha256": "invalid", "mime": "image/png"}}}
	if err = task.prepareImages(); err == nil {
		t.Fatal("accepted a corrupted transfer")
	}
	if original, err := os.ReadFile(path); err != nil || !bytes.Equal(original, data) {
		t.Fatal("failed transfer damaged an earlier conversation image", err)
	}
}

func TestCancellationDuringImageDownloadNeverStartsAgent(t *testing.T) {
	started := make(chan struct{})
	client := imageFixtureClient("device-token", func(w http.ResponseWriter, r *http.Request) { close(started); <-r.Context().Done() })
	task := newTask(M{"kind": "prompt", "agent": M{"kind": "codex", "cwd": t.TempDir()}, "payload": M{"prompt": "must not execute", "images": []interface{}{M{"id": "img_fixture"}}}})
	task.client = client
	task.imageDir = filepath.Join(t.TempDir(), "images")
	marker := t.TempDir() + "/agent-called"
	bin := t.TempDir()
	os.WriteFile(bin+"/codex", []byte("#!/bin/sh\ntouch '"+marker+"'\n"), 0700)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	go task.run(nil, false)
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("download did not start")
	}
	task.cancel()
	waitTask(t, task)
	if task.result["status"] != "cancelled" {
		t.Fatal(task.result)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("cancelled image request started a model")
	}
}

func TestImageRemainsReadableOnTextOnlyContinuationAfterTaskRestart(t *testing.T) {
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, image.NewRGBA(image.Rect(0, 0, 2, 2))); err != nil {
		t.Fatal(err)
	}
	data := encoded.Bytes()
	checksum := hashBytes(data)
	requests := 0
	client := imageFixtureClient("device-token", func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.Header().Set("X-SHA256", checksum)
		w.Write(data)
	})
	dir := t.TempDir()
	bin := filepath.Join(dir, "bin")
	if err := os.Mkdir(bin, 0700); err != nil {
		t.Fatal(err)
	}
	script := `#!/bin/sh
set -eu
image=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--image" ]; then shift; image="$1"; fi
  shift
done
cat >/dev/null
if [ -n "$image" ]; then
  printf '%s' "$image" > "$RELAY_IMAGE_FIXTURE"
else
  image="$(cat "$RELAY_IMAGE_FIXTURE")"
fi
test -f "$image"
cmp "$image" "$RELAY_IMAGE_EXPECTED"
printf '%s\n' '{"type":"thread.started","thread_id":"fixture-session"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"original remains readable"}}'
`
	if err := os.WriteFile(filepath.Join(bin, "codex"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	expected := filepath.Join(dir, "expected.png")
	if err := os.WriteFile(expected, data, 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("CODEX_HOME", filepath.Join(dir, "isolated-codex"))
	t.Setenv("RELAY_IMAGE_FIXTURE", filepath.Join(dir, "original-path"))
	t.Setenv("RELAY_IMAGE_EXPECTED", expected)
	first := newTask(M{"kind": "prompt", "agent": M{"kind": "codex", "cwd": dir}, "payload": M{"prompt": "look", "images": []interface{}{M{"id": "img_fixture", "sha256": checksum, "mime": "image/png"}}}})
	first.client = client
	first.imageDir = filepath.Join(dir, "device", "images")
	go first.run(nil, false)
	waitTask(t, first)
	if first.result["status"] != "succeeded" {
		t.Fatal("first image turn failed", first.result)
	}
	// Discard all task state. A restarted daemon/agent must still be able to
	// read the path retained by native history without reattaching the image.
	second := newTask(M{"kind": "prompt", "agent": M{"kind": "codex", "cwd": dir, "session_id": "fixture-session"}, "payload": M{"prompt": "look again"}})
	go second.run(nil, false)
	waitTask(t, second)
	if second.result["status"] != "succeeded" || !strings.Contains(str(second.result["output"]), "original remains readable") {
		t.Fatal("text-only continuation lost the original", second.result)
	}
	if requests != 1 {
		t.Fatal("continuation needed a second download", requests)
	}
	// Redelivery preserves the same filename and leaves no staging files.
	retry := newTask(first.job)
	retry.client, retry.imageDir = first.client, first.imageDir
	if err := retry.prepareImages(); err != nil {
		t.Fatal(err)
	}
	if retry.images[0].path != first.images[0].path {
		t.Fatal("redelivery changed the original path")
	}
	entries, err := os.ReadDir(first.imageDir)
	if err != nil || len(entries) != 1 {
		t.Fatal("unexpected staging files", entries, err)
	}
}
