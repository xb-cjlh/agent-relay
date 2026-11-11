package relay

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestDirectoryBrowserPaginationAndSymlinks(t *testing.T) {
	root := t.TempDir()
	for i := 0; i < 205; i++ {
		if err := os.Mkdir(filepath.Join(root, fmt.Sprintf("folder-%03d", i)), 0700); err != nil {
			t.Fatal(err)
		}
	}
	os.WriteFile(filepath.Join(root, "a-file"), []byte("not a directory"), 0600)
	os.Symlink(filepath.Join(root, "folder-000"), filepath.Join(root, "z-symlink"))
	task := newTask(M{"kind": "directory_list", "payload": M{"path": root}})
	go task.run(nil, false)
	waitTask(t, task)
	if task.result["status"] != "succeeded" {
		t.Fatal(task.result)
	}
	page := obj(task.result["directory"])
	names := page["entries"].([]string)
	if len(names) != 200 || names[0] != "folder-000" || str(page["next_cursor"]) != "folder-199" {
		t.Fatal(page)
	}
	task = newTask(M{"kind": "directory_list", "payload": M{"path": root, "after": page["next_cursor"]}})
	go task.run(nil, false)
	waitTask(t, task)
	last := obj(task.result["directory"])
	entries := last["entries"].([]string)
	if len(entries) != 6 || entries[5] != "z-symlink" || last["next_cursor"] != "" {
		t.Fatal(last)
	}
	missing := newTask(M{"kind": "directory_list", "payload": M{"path": filepath.Join(root, "missing")}})
	go missing.run(nil, false)
	waitTask(t, missing)
	if missing.result["status"] != "failed" {
		t.Fatal(missing.result)
	}
}
