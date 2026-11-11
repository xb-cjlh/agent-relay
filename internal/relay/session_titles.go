package relay

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode"
)

type titleValues struct{ custom, auto, summary string }
type sessionTitleIndex struct {
	mtime, size int64
	names       map[string]titleValues
}
type transcriptTitles struct {
	titleValues
	offset, mtime, size int64
	skipping            bool
	customSeen          bool
}

func cleanSessionTitle(value string) string {
	value = strings.Join(strings.Fields(value), " ")
	value = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return -1
		}
		return r
	}, value)
	return head(value, 256)
}

// Native title indexes are separate from transcripts and can change on rename
// without any new messages. Read them only when their file metadata changes.
func (m *Mirror) loadTitleIndex(path, kind string) {
	info, err := os.Stat(path)
	if err != nil || info.Size() > 8*1024*1024 {
		return
	}
	old := m.titleIndexes[path]
	if old != nil && old.mtime == info.ModTime().UnixNano() && old.size == info.Size() {
		return
	}
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, 8*1024*1024+1))
	if err != nil || len(data) > 8*1024*1024 {
		return
	}
	if kind == "codex" && len(data) > 0 && data[len(data)-1] != '\n' {
		return
	}
	names := map[string]titleValues{}
	if kind == "codex" {
		reader := bufio.NewReader(bytes.NewReader(data))
		for {
			line, err := reader.ReadBytes('\n')
			if err != nil {
				break
			} // A writer may still be appending its final record.
			var entry M
			if json.Unmarshal(line, &entry) == nil && str(entry["id"]) != "" {
				names[str(entry["id"])] = titleValues{auto: cleanSessionTitle(str(entry["thread_name"]))}
			}
		}
	} else {
		var index struct {
			Entries []struct {
				SessionID   string `json:"sessionId"`
				Summary     string `json:"summary"`
				CustomTitle string `json:"customTitle"`
			} `json:"entries"`
		}
		if json.Unmarshal(data, &index) != nil {
			return
		}
		for _, entry := range index.Entries {
			if entry.SessionID != "" {
				names[entry.SessionID] = titleValues{custom: cleanSessionTitle(entry.CustomTitle), summary: cleanSessionTitle(entry.Summary)}
			}
		}
	}
	m.titleIndexes[path] = &sessionTitleIndex{mtime: info.ModTime().UnixNano(), size: info.Size(), names: names}
}

// Claude keeps ai-title/custom-title records anywhere in a transcript. Scan
// incrementally from its start, independently of the truncated message window.
// A shared budget bounds work per daemon poll, including very large records.
func readClaudeTitles(path, sid string, t *transcriptTitles, stat os.FileInfo, budget *int) {
	if t.mtime == stat.ModTime().UnixNano() && t.size == stat.Size() {
		return
	}
	if stat.Size() < t.size || (t.mtime != 0 && t.mtime != stat.ModTime().UnixNano() && stat.Size() <= t.size) {
		*t = transcriptTitles{}
	}
	if *budget <= 0 {
		return
	}
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	if _, err = f.Seek(t.offset, io.SeekStart); err != nil {
		return
	}
	reader := bufio.NewReaderSize(f, 64*1024)
	for *budget > 0 {
		line, err := reader.ReadSlice('\n')
		if err == io.EOF {
			// Retry an unfinished small JSON record when its newline is written.
			if t.skipping {
				t.offset += int64(len(line))
			}
			break
		}
		if err != nil && err != bufio.ErrBufferFull {
			break
		}
		t.offset += int64(len(line))
		*budget -= len(line)
		if err == bufio.ErrBufferFull {
			t.skipping = true
			continue
		}
		if t.skipping {
			t.skipping = false
			continue
		}
		var event M
		if json.Unmarshal(line, &event) != nil || yes(event["isSidechain"]) || yes(event["isCompactSummary"]) {
			continue
		}
		if id := str(event["sessionId"]); id != "" && id != sid {
			continue
		}
		switch event["type"] {
		case "custom-title":
			t.customSeen = true
			t.custom = cleanSessionTitle(str(event["customTitle"]))
		case "ai-title":
			t.auto = cleanSessionTitle(str(event["aiTitle"]))
		case "summary":
			if str(event["leafUuid"]) != "" && !yes(event["isMeta"]) {
				t.summary = cleanSessionTitle(str(event["summary"]))
			}
		}
	}
	t.size = stat.Size()
	t.mtime = 0
	if t.offset == stat.Size() {
		t.mtime = stat.ModTime().UnixNano()
	}
}

func (m *Mirror) readSessionTitle(path string, t *trackedFile, stat os.FileInfo, budget *int) {
	kind, sid := str(t.meta["kind"]), str(t.meta["session_id"])
	indexPath := filepath.Join(filepath.Dir(m.bases["codex"]), "session_index.jsonl")
	if kind == "claude" {
		indexPath = filepath.Join(filepath.Dir(path), "sessions-index.json")
		readClaudeTitles(path, sid, &t.titles, stat, budget)
	}
	indexed := titleValues{}
	if index := m.titleIndexes[indexPath]; index != nil {
		indexed = index.names[sid]
	}
	custom := t.titles.custom
	if !t.titles.customSeen {
		custom = indexed.custom
	}
	for _, title := range []string{custom, t.titles.auto, indexed.auto, t.titles.summary, indexed.summary} {
		if title != "" {
			t.meta["title"] = title
			return
		}
	}
	t.meta["title"] = ""
}
