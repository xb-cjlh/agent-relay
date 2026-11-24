package relay

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestFileMonitorAtomicSavesSameTimestampAndLeaseExpiry(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "preview.txt")
	os.WriteFile(path, []byte("first"), 0600)
	var mu sync.Mutex
	reads := 0
	hashes := []string{}
	lastHash := hashBytes([]byte("first"))
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/api/device/files/refresh":
			mu.Lock()
			sha := lastHash
			mu.Unlock()
			json.NewEncoder(w).Encode(M{"job": M{"id": "job_watch", "kind": "file_read", "payload": M{"path": path, "if_sha256": sha}}})
		case r.URL.Path == "/api/device/jobs/job_watch/file-upload":
			json.NewEncoder(w).Encode(M{"supported": false})
		case r.URL.Path == "/api/device/jobs/job_watch/complete":
			var body M
			json.NewDecoder(r.Body).Decode(&body)
			file := obj(body["file"])
			sha := str(file["sha256"])
			if !yes(file["unchanged"]) {
				data, err := base64.StdEncoding.DecodeString(str(file["data"]))
				if err != nil || hashBytes(data) != sha {
					t.Error("file payload integrity mismatch")
				}
			}
			mu.Lock()
			reads++
			lastHash = sha
			hashes = append(hashes, sha)
			mu.Unlock()
			json.NewEncoder(w).Encode(M{"status": "succeeded"})
		default:
			t.Errorf("unexpected request %s", r.URL.Path)
			http.Error(w, "unknown", 404)
		}
	}))
	defer fixture.Close()
	client, _ := NewClient(fixture.URL, "fixture")
	monitor := newFileMonitor(context.Background(), client, root)
	defer monitor.close()
	monitor.update([]interface{}{M{"path": path, "ttl": 45}})
	waitFor := func(predicate func() bool) {
		t.Helper()
		end := time.Now().Add(4 * time.Second)
		for !predicate() {
			if time.Now().After(end) {
				t.Fatal("file monitor timed out")
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	waitFor(func() bool { mu.Lock(); defer mu.Unlock(); return reads == 1 })
	time.Sleep(400 * time.Millisecond)
	mu.Lock()
	count := reads
	mu.Unlock()
	if count != 1 {
		t.Fatalf("idle monitor made %d reads", count)
	}
	info, _ := os.Stat(path)
	os.WriteFile(path, []byte("other"), 0600)
	os.Chtimes(path, info.ModTime(), info.ModTime())
	waitFor(func() bool { mu.Lock(); defer mu.Unlock(); return lastHash == hashBytes([]byte("other")) })
	tmp := filepath.Join(root, "replacement")
	os.WriteFile(tmp, []byte("atomic"), 0600)
	os.Rename(tmp, path)
	waitFor(func() bool { mu.Lock(); defer mu.Unlock(); return lastHash == hashBytes([]byte("atomic")) })
	// A new symlink target must also be observed after atomic replacement.
	target := filepath.Join(root, "target")
	os.WriteFile(target, []byte("linked"), 0600)
	os.Remove(path)
	os.Symlink(target, path)
	waitFor(func() bool { mu.Lock(); defer mu.Unlock(); return lastHash == hashBytes([]byte("linked")) })
	os.WriteFile(target, []byte("target changed"), 0600)
	waitFor(func() bool { mu.Lock(); defer mu.Unlock(); return lastHash == hashBytes([]byte("target changed")) })
	monitor.update([]interface{}{M{"path": path, "ttl": .15}})
	time.Sleep(400 * time.Millisecond)
	mu.Lock()
	before := reads
	mu.Unlock()
	os.WriteFile(target, []byte("expired"), 0600)
	time.Sleep(500 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if reads != before {
		t.Fatal("expired watch still read file")
	}
}

func TestFileCompletionReportIsSerializedAndIdempotent(t *testing.T) {
	var mu sync.Mutex
	calls := 0
	fixture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls++
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"succeeded"}`))
	}))
	defer fixture.Close()
	client, _ := NewClient(fixture.URL, "fixture")
	task := newTask(M{"id": "job_file"})
	defer task.cancel()
	task.client = client
	task.result = M{"status": "succeeded"}
	var workers sync.WaitGroup
	for i := 0; i < 8; i++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			if err := task.report(); err != nil {
				t.Error(err)
			}
		}()
	}
	workers.Wait()
	if calls != 1 {
		t.Fatalf("completion submitted %d times", calls)
	}
}
