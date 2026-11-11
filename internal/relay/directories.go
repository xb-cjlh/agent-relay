package relay

import (
	"encoding/json"
	"os"
	"path/filepath"
	"unicode/utf8"
)

func (t *Task) listDirectory() (M, error) {
	payload := obj(t.job["payload"])
	path, err := accessiblePath(str(payload["path"]), "")
	if err != nil {
		return nil, err
	}
	path, err = filepath.EvalSymlinks(path)
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	names := []string{}
	result := M{"path": path, "parent": filepath.Dir(path), "entries": names, "next_cursor": ""}
	for _, entry := range entries {
		if t.ctx.Err() != nil {
			return M{"status": "cancelled"}, nil
		}
		name := entry.Name()
		if name <= str(payload["after"]) || !utf8.ValidString(name) {
			continue
		}
		directory := entry.IsDir()
		if entry.Type()&os.ModeSymlink != 0 {
			if info, err := os.Stat(filepath.Join(path, name)); err == nil {
				directory = info.IsDir()
			}
		}
		if !directory {
			continue
		}
		candidate := append(names, name)
		result["entries"] = candidate
		data, _ := json.Marshal(result)
		if len(names) >= 200 || len(data) > 48000 {
			result["entries"] = names
			result["next_cursor"] = names[len(names)-1]
			break
		}
		names = candidate
	}
	return M{"status": "succeeded", "directory": result}, nil
}
