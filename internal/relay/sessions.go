package relay

import (
	"bufio"
	"encoding/json"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const initialBytes = 2 * 1024 * 1024

type trackedFile struct {
	meta                          M
	offset, mtime, size, released int64
}
type Mirror struct {
	roots         []string
	bases         map[string]string
	files         map[string]*trackedFile
	pending       map[string]M
	lastDiscovery time.Time
	scoped        bool
	ignored       map[string]int64
}

func newMirror(roots []string) *Mirror {
	codex := os.Getenv("CODEX_HOME")
	if codex == "" {
		codex = "~/.codex"
	}
	claude := os.Getenv("CLAUDE_CONFIG_DIR")
	if claude == "" {
		claude = "~/.claude"
	}
	return &Mirror{roots: roots, bases: map[string]string{"codex": filepath.Join(expand(codex), "sessions"), "claude": filepath.Join(expand(claude), "projects")}, files: map[string]*trackedFile{}, pending: map[string]M{}, ignored: map[string]int64{}}
}
func copyMap(m M) M {
	n := M{}
	for k, v := range m {
		n[k] = v
	}
	return n
}
func messageText(content interface{}) string {
	if s, ok := content.(string); ok {
		return s
	}
	var parts []string
	for _, v := range arr(content) {
		p := obj(v)
		kind := str(p["type"])
		if kind == "text" || kind == "input_text" || kind == "output_text" {
			parts = append(parts, str(p["text"]))
		}
	}
	return strings.Join(parts, "\n")
}
func timestamp(v interface{}, fallback float64) float64 {
	t, e := time.Parse(time.RFC3339Nano, str(v))
	if e != nil {
		return fallback
	}
	return float64(t.UnixNano()) / 1e9
}
func (m *Mirror) metadata(path, kind string) M {
	f, e := os.Open(path)
	if e != nil {
		return nil
	}
	defer f.Close()
	reader := bufio.NewReaderSize(f, 512*1024)
	for i := 0; i < 80; i++ {
		line, _, e := reader.ReadLine()
		if e != nil {
			break
		}
		var event M
		if json.Unmarshal(line, &event) != nil {
			continue
		}
		meta := event
		if kind == "codex" {
			meta = obj(event["payload"])
		}
		if kind == "claude" && yes(event["isSidechain"]) {
			return nil
		}
		cwd := str(meta["cwd"])
		sid := str(meta["sessionId"])
		if kind == "codex" {
			sid = str(meta["id"])
			if sid == "" {
				sid = str(meta["session_id"])
			}
		}
		if cwd == "" || sid == "" {
			continue
		}
		dir, e := filepath.Abs(expand(cwd))
		if e != nil {
			return nil
		}
		if resolved, e := filepath.EvalSymlinks(dir); e == nil {
			dir = resolved
		}
		if m.scoped {
			if _, e = virtualDiscoveryPath(dir, m.roots); e != nil {
				return nil
			}
		}
		stat, e := f.Stat()
		if e != nil {
			return nil
		}
		result := M{"session_id": sid, "kind": kind, "cwd": dir, "name": head(filepath.Base(dir), 40) + " · " + head(sid, 8), "busy": true, "updated_at": float64(stat.ModTime().UnixNano()) / 1e9}
		m.controls(result)
		return result
	}
	return nil
}
func (m *Mirror) controls(meta M) {
	meta["controllable"] = false
	meta["control_reason"] = "cwd_missing"
	info, err := os.Stat(str(meta["cwd"]))
	if err != nil || !info.IsDir() {
		meta["control_reason"] = "cwd_missing"
		return
	}
	meta["controllable"] = true
	meta["control_reason"] = ""
}
func (m *Mirror) scan() []M {
	if time.Since(m.lastDiscovery) >= 5*time.Second {
		m.lastDiscovery = time.Now()
		for kind, base := range m.bases {
			type candidate struct {
				path  string
				mtime int64
			}
			paths := []candidate{}
			filepath.WalkDir(base, func(path string, d fs.DirEntry, e error) error {
				if e != nil {
					return nil
				}
				if d.IsDir() {
					if d.Name() == "subagents" {
						return filepath.SkipDir
					}
					return nil
				}
				if !strings.HasSuffix(path, ".jsonl") {
					return nil
				}
				info, e := d.Info()
				if e == nil {
					paths = append(paths, candidate{path, info.ModTime().UnixNano()})
				}
				return nil
			})
			sort.Slice(paths, func(i, j int) bool { return paths[i].mtime > paths[j].mtime })
			attempts := 0
			for _, p := range paths {
				if m.files[p.path] != nil {
					continue
				}
				if stamp, ok := m.ignored[p.path]; ok && stamp == p.mtime {
					continue
				}
				if attempts >= 100 {
					break
				}
				attempts++
				if meta := m.metadata(p.path, kind); meta != nil {
					m.files[p.path] = &trackedFile{meta: meta, mtime: -1, size: -1, released: -1}
				} else {
					m.ignored[p.path] = p.mtime
				}
			}
		}
	}
	for path, tracked := range m.files {
		size := 0
		for _, batch := range m.pending {
			for _, v := range arr(batch["messages"]) {
				size += len(str(obj(v)["content"]))
			}
		}
		if len(m.pending) >= 50 || size >= 2*1024*1024 {
			break
		}
		stat, e := os.Stat(path)
		if e != nil {
			continue
		}
		previousControl, previousReason := tracked.meta["controllable"], tracked.meta["control_reason"]
		m.controls(tracked.meta)
		if tracked.meta["controllable"] != previousControl || tracked.meta["control_reason"] != previousReason {
			key := str(tracked.meta["kind"]) + ":" + str(tracked.meta["session_id"])
			batch := m.pending[key]
			if batch == nil {
				batch = M{"messages": []interface{}{}}
				m.pending[key] = batch
			}
			for k, v := range tracked.meta {
				batch[k] = v
			}
		}
		if stat.ModTime().UnixNano() == tracked.mtime && stat.Size() == tracked.size {
			continue
		}
		m.readChanges(path, tracked, stat)
	}
	result := []M{}
	for _, batch := range m.pending {
		result = append(result, batch)
	}
	return result
}
func (m *Mirror) readChanges(path string, t *trackedFile, stat os.FileInfo) {
	f, e := os.Open(path)
	if e != nil {
		return
	}
	defer f.Close()
	meta := t.meta
	key := str(meta["kind"]) + ":" + str(meta["session_id"])
	batch := m.pending[key]
	if batch == nil {
		batch = copyMap(meta)
		batch["messages"] = []interface{}{}
		m.pending[key] = batch
	}
	offset := t.offset
	if offset > stat.Size() {
		offset = 0
	}
	if offset == 0 && stat.Size() > initialBytes {
		offset = stat.Size() - initialBytes
		f.Seek(offset, io.SeekStart)
		r := bufio.NewReader(f)
		skip, _ := r.ReadBytes('\n')
		offset += int64(len(skip))
	}
	f.Seek(offset, io.SeekStart)
	reader := bufio.NewReaderSize(f, 64*1024)
	messages := arr(batch["messages"])
	contentSize := 0
	for _, v := range messages {
		contentSize += len(str(obj(v)["content"]))
	}
	for len(messages) < 200 && contentSize < 256*1024 {
		start := offset
		var line []byte
		oversized := false
		complete := false
		for {
			chunk, err := reader.ReadSlice('\n')
			offset += int64(len(chunk))
			if len(line)+len(chunk) > 1024*1024 {
				oversized = true
				line = nil
			}
			if !oversized {
				line = append(line, chunk...)
			}
			if err == nil {
				complete = true
				break
			}
			if err == bufio.ErrBufferFull {
				continue
			}
			break
		}
		if !complete {
			offset = start
			break
		}
		if oversized {
			continue
		}
		var event M
		if json.Unmarshal(line, &event) != nil {
			continue
		}
		msg := parseMessage(event, meta, float64(stat.ModTime().UnixNano())/1e9)
		if msg == nil {
			continue
		}
		source := str(event["uuid"])
		if source == "" {
			source = strconv.FormatInt(start, 10)
		}
		msg["id"] = "native_" + hash(key + ":" + source)[:32]
		messages = append(messages, msg)
		contentSize += len(str(msg["content"]))
	}
	t.offset = offset
	t.mtime = -1
	if offset == stat.Size() {
		t.mtime = stat.ModTime().UnixNano()
	}
	t.size = stat.Size()
	meta["updated_at"] = float64(stat.ModTime().UnixNano()) / 1e9
	if t.released == stat.ModTime().UnixNano() {
		meta["busy"] = false
	}
	for k, v := range meta {
		batch[k] = v
	}
	batch["messages"] = messages
	if offset < stat.Size() {
		batch["busy"] = true
	}
}
func parseMessage(event, meta M, fallback float64) M {
	role, text := "", ""
	// Compaction output can look exactly like a final assistant answer. Keep
	// only its stable ID/classification so a prior sync can also be retracted.
	if yes(obj(event["metadata"])["compaction_output"]) || yes(event["isCompactSummary"]) {
		role = str(event["type"])
		if meta["kind"] == "codex" {
			role = str(obj(event["payload"])["role"])
		}
		if role == "user" || role == "assistant" {
			return M{"role": role, "content": "", "internal": true, "created_at": timestamp(event["timestamp"], fallback)}
		}
		return nil
	}
	if meta["kind"] == "codex" {
		p := obj(event["payload"])
		if event["type"] == "event_msg" {
			switch p["type"] {
			case "task_started":
				meta["busy"] = true
			case "task_complete", "task_cancelled", "turn_aborted":
				meta["busy"] = false
			}
		}
		if event["type"] == "response_item" && p["type"] == "message" {
			role = str(p["role"])
			text = messageText(p["content"])
			if p["phase"] == "analysis" {
				return nil
			}
			if role == "user" {
				s := strings.TrimSpace(text)
				for _, prefix := range []string{"<environment_context>", "<user_instructions>", "<permissions instructions>"} {
					if strings.HasPrefix(s, prefix) {
						return nil
					}
				}
			}
		}
	} else {
		if yes(event["isSidechain"]) || yes(event["isMeta"]) {
			return nil
		}
		message := obj(event["message"])
		if event["type"] == "user" || event["type"] == "assistant" {
			role = str(event["type"])
			text = messageText(message["content"])
			if role == "user" && text != "" {
				meta["busy"] = true
			}
			if role == "assistant" {
				switch message["stop_reason"] {
				case "end_turn", "stop_sequence", "max_tokens":
					meta["busy"] = false
				}
			}
		}
		if event["type"] == "system" && event["subtype"] == "turn_duration" {
			meta["busy"] = false
		}
	}
	if (role != "user" && role != "assistant") || strings.TrimSpace(text) == "" {
		return nil
	}
	return M{"role": role, "content": tail(text, MaxOutput), "created_at": timestamp(event["timestamp"], fallback)}
}
func (m *Mirror) acknowledge() { m.pending = map[string]M{} }
func (m *Mirror) release(kind, sid string) {
	for path, t := range m.files {
		if t.meta["kind"] != kind || t.meta["session_id"] != sid {
			continue
		}
		stat, e := os.Stat(path)
		if e != nil {
			continue
		}
		t.released = stat.ModTime().UnixNano()
		t.meta["busy"] = false
		key := kind + ":" + sid
		batch := m.pending[key]
		if batch == nil {
			batch = M{"messages": []interface{}{}}
			m.pending[key] = batch
		}
		for k, v := range t.meta {
			batch[k] = v
		}
	}
}
