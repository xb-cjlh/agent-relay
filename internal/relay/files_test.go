package relay

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFileUploadIsSignedDirectAndUnchangedSkipsTransfer(t *testing.T) {
	data := []byte("original contents")
	uploads := 0
	var base string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/device/jobs/job_1/file-upload" {
			if r.Header.Get("Authorization") != "Bearer device-secret" {
				t.Error("missing device authentication")
			}
			var body M
			json.NewDecoder(r.Body).Decode(&body)
			if body["sha256"] != hashBytes(data) || body["name"] != "report.md" {
				t.Error("invalid upload claims")
			}
			json.NewEncoder(w).Encode(M{"supported": true, "url": base + "/object?signature=private-signature", "upload_id": "fup_1", "headers": M{"Content-MD5": body["md5"]}})
		} else if r.URL.Path == "/object" {
			uploads++
			if r.Header.Get("Authorization") != "" {
				t.Error("bearer token forwarded to OSS")
			}
			if r.Method != "PUT" || r.ContentLength != int64(len(data)) || r.Header.Get("Content-MD5") == "" {
				t.Error("invalid direct upload")
			}
			w.WriteHeader(200)
		} else {
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	base = server.URL
	path := filepath.Join(t.TempDir(), "report.md")
	os.WriteFile(path, data, 0600)
	client, _ := NewClient(base, "device-secret")
	task := newTask(M{"id": "job_1", "kind": "file_read", "payload": M{"path": path}})
	task.client = client
	result, err := task.readFile(nil)
	if err != nil || obj(result["file"])["upload_id"] != "fup_1" || obj(result["file"])["data"] != nil || uploads != 1 {
		t.Fatalf("direct read failed: %v %v", result, err)
	}
	obj(task.job["payload"])["if_sha256"] = hashBytes(data)
	result, err = task.readFile(nil)
	if err != nil || !yes(obj(result["file"])["unchanged"]) || uploads != 1 {
		t.Fatal("unchanged read uploaded data")
	}
}
func TestSignedFileDownloadVerifiesBytesAndNeverForwardsBearer(t *testing.T) {
	data := []byte("download contents")
	corrupt := false
	var base string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/url") {
			json.NewEncoder(w).Encode(M{"storage": "oss", "url": base + "/object?signature=private", "sha256": hashBytes(data)})
		} else {
			if r.Header.Get("Authorization") != "" {
				t.Error("bearer token forwarded")
			}
			if corrupt {
				w.Write([]byte("bad"))
			} else {
				w.Write(data)
			}
		}
	}))
	defer server.Close()
	base = server.URL
	client, _ := NewClient(base, "account-secret")
	output := filepath.Join(t.TempDir(), "file")
	if _, err := Download(client, "file_1", output); err != nil {
		t.Fatal(err)
	}
	actual, _ := os.ReadFile(output)
	if string(actual) != string(data) {
		t.Fatal("wrong bytes")
	}
	corrupt = true
	output = filepath.Join(t.TempDir(), "corrupt")
	if _, err := Download(client, "file_1", output); err == nil {
		t.Fatal("corrupt file accepted")
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatal("corrupt output created")
	}
}
