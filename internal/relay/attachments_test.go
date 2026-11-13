package relay

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAttachmentsDirectDownloadNativeInputsIntegrityAndPersistence(t *testing.T) {
	data := []byte("%PDF-1.7\x00binary attachment")
	oss := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.Header.Get("Cookie") != "" {
			t.Error("Relay credentials leaked to object storage")
		}
		w.Write(data)
	}))
	defer oss.Close()
	client := imageFixtureClient("device-token", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/device/attachments/att_fixture/url" || r.Header.Get("Authorization") != "Bearer device-token" {
			t.Error("incorrect device authorization")
		}
		json.NewEncoder(w).Encode(M{"url": oss.URL + "/object", "headers": M{}})
	})
	meta := M{"id": "att_fixture", "name": "中文 report.pdf", "size": len(data), "sha256": hashBytes(data)}
	job := M{"payload": M{"prompt": "read the file", "files": []interface{}{meta}}}
	dir := filepath.Join(t.TempDir(), "attachments")
	var path string
	for attempt := 0; attempt < 2; attempt++ {
		task := newTask(job)
		task.client, task.attachmentDir = client, dir
		if err := task.prepareAttachments(); err != nil {
			t.Fatal(err)
		}
		path = str(task.attachments[0]["path"])
		if filepath.Base(path) != meta["name"] || filepath.Dir(filepath.Dir(path)) != dir {
			t.Fatal("unstable filename or directory", path)
		}
		for _, input := range []string{str(task.codexInput()[0]["text"]), task.agentInput("codex"), task.agentInput("claude"), str(obj(task.claudeInput()["message"])["content"].([]M)[0]["text"])} {
			if !strings.HasPrefix(input, "read the file\n\n[Relay attachments]\n") || !strings.Contains(input, path) {
				t.Fatal("native agent is missing attachment path", input)
			}
		}
		if info, _ := os.Stat(path); info.Mode().Perm() != 0600 {
			t.Fatal("attachment is not private")
		}
		task.cancel()
	}
	meta["sha256"] = strings.Repeat("0", 64)
	task := newTask(job)
	task.client, task.attachmentDir = client, dir
	if err := task.prepareAttachments(); err == nil {
		t.Fatal("corrupted attachment was accepted")
	}
	if original, _ := os.ReadFile(path); !bytes.Equal(original, data) {
		t.Fatal("a failed retry damaged an earlier file")
	}
}

func TestAttachmentPathTraversalAndCancelledTransfer(t *testing.T) {
	for _, name := range []string{"../outside", "a/b", "a\\b", "..", "\x00file"} {
		task := newTask(M{"payload": M{"files": []interface{}{M{"id": "att_fixture", "name": name}}}})
		task.client, _ = NewClient("https://unused.invalid", "device-token")
		task.attachmentDir = t.TempDir()
		if err := task.prepareAttachments(); err == nil {
			t.Fatal("accepted unsafe name", fmt.Sprintf("%q", name))
		}
	}
	task := newTask(M{"payload": M{"files": []interface{}{M{"id": "att_fixture", "name": "file.txt"}}}})
	task.client, _ = NewClient("https://unused.invalid", "device-token")
	task.attachmentDir = t.TempDir()
	task.cancel()
	if err := task.prepareAttachments(); err == nil || len(task.attachments) != 0 {
		t.Fatal("cancelled transfer produced input")
	}
}
