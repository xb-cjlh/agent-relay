package relay

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func appendTitleRecords(t *testing.T, path string, events ...M) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	for _, event := range events {
		data, _ := json.Marshal(event)
		f.Write(append(data, '\n'))
	}
}

func TestCodexNativeTitlesRenameWithoutTranscriptChanges(t *testing.T) {
	m, cwd, _ := fixtureMirror(t)
	path := filepath.Join(m.bases["codex"], "session.jsonl")
	writeRecords(t, path, records(cwd, false))
	index := filepath.Join(filepath.Dir(m.bases["codex"]), "session_index.jsonl")
	writeRecords(t, index, []M{{"id": "native-codex-1", "thread_name": "  修复\n登录问题  "}})
	first := m.scan()
	if len(first) != 1 || first[0]["title"] != "修复 登录问题" {
		t.Fatalf("native title: %v", first)
	}
	activity := first[0]["updated_at"]
	m.acknowledge()
	appendTitleRecords(t, index, M{"id": "native-codex-1", "thread_name": "新的标题"})
	m.lastDiscovery = time.Time{}
	rename := m.scan()
	if len(rename) != 1 || rename[0]["title"] != "新的标题" || len(arr(rename[0]["messages"])) != 0 || rename[0]["updated_at"] != activity {
		t.Fatalf("rename without new messages/activity: %v", rename)
	}
	m.acknowledge()
	f, _ := os.OpenFile(index, os.O_APPEND|os.O_WRONLY, 0600)
	f.WriteString(`{"id":"native-codex-1","thread_name":"partial"}`)
	f.Close()
	m.lastDiscovery = time.Time{}
	if len(m.scan()) != 0 {
		t.Fatal("unfinished title index changed the visible title")
	}
	f, _ = os.OpenFile(index, os.O_APPEND|os.O_WRONLY, 0600)
	f.WriteString("\n")
	f.Close()
	m.lastDiscovery = time.Time{}
	if batch := m.scan(); len(batch) != 1 || batch[0]["title"] != "partial" {
		t.Fatalf("completed index record: %v", batch)
	}
}

func TestClaudeAITitlesCustomTitlesAndPartialUpdates(t *testing.T) {
	m, cwd, _ := fixtureMirror(t)
	path := filepath.Join(m.bases["claude"], "project/session.jsonl")
	writeRecords(t, path, []M{{"type": "user", "sessionId": "claude-1", "cwd": cwd, "uuid": "question", "message": M{"content": "question"}},
		{"type": "ai-title", "sessionId": "claude-1", "aiTitle": "自动生成的会话名"},
		{"type": "ai-title", "sessionId": "other", "aiTitle": "另一个会话"}})
	batch := m.scan()
	if len(batch) != 1 || batch[0]["title"] != "自动生成的会话名" || len(arr(batch[0]["messages"])) != 1 {
		t.Fatalf("AI title is metadata, never a message: %v", batch)
	}
	m.acknowledge()
	appendTitleRecords(t, path, M{"type": "custom-title", "sessionId": "claude-1", "customTitle": "手动命名"}, M{"type": "ai-title", "sessionId": "claude-1", "aiTitle": "更新的自动标题"})
	batch = m.scan()
	if len(batch) != 1 || batch[0]["title"] != "手动命名" || len(arr(batch[0]["messages"])) != 0 {
		t.Fatalf("custom title priority: %v", batch)
	}
	m.acknowledge()
	appendTitleRecords(t, path, M{"type": "custom-title", "sessionId": "claude-1", "customTitle": ""})
	if batch = m.scan(); len(batch) != 1 || batch[0]["title"] != "更新的自动标题" {
		t.Fatalf("clear custom title: %v", batch)
	}
	m.acknowledge()
	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	f.WriteString(`{"type":"ai-title","sessionId":"claude-1","aiTitle":"未完成"}`)
	f.Close()
	for _, b := range m.scan() {
		if b["title"] != "更新的自动标题" {
			t.Fatalf("partial record applied: %v", b)
		}
	}
	m.acknowledge()
	f, _ = os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	f.WriteString("\n")
	f.Close()
	if batch = m.scan(); len(batch) != 1 || batch[0]["title"] != "未完成" {
		t.Fatalf("completed title: %v", batch)
	}
	// Explicitly clearing a transcript's custom title wins over a stale old index.
	index := filepath.Join(filepath.Dir(path), "sessions-index.json")
	data, _ := json.Marshal(M{"entries": []M{{"sessionId": "claude-1", "customTitle": "旧索引中的名字"}}})
	os.WriteFile(index, data, 0600)
	m.acknowledge()
	m.lastDiscovery = time.Time{}
	for _, b := range m.scan() {
		if b["title"] != "未完成" {
			t.Fatalf("stale custom index restored cleared name: %v", b)
		}
	}
}

func TestClaudeTitlesBeforeTruncatedHistoryAndIndexFallback(t *testing.T) {
	m, cwd, _ := fixtureMirror(t)
	path := filepath.Join(m.bases["claude"], "project/session.jsonl")
	writeRecords(t, path, []M{{"type": "user", "sessionId": "large-claude", "cwd": cwd, "uuid": "question", "message": M{"content": "question"}},
		{"type": "ai-title", "sessionId": "large-claude", "aiTitle": "保留较早的标题"},
		{"type": "file-history-snapshot", "data": strings.Repeat("x", 3*1024*1024)},
		{"type": "assistant", "sessionId": "large-claude", "cwd": cwd, "uuid": "answer", "message": M{"content": "answer"}}})
	batch := m.scan()
	if len(batch) != 1 || batch[0]["title"] != "保留较早的标题" {
		t.Fatalf("title outside last 2 MiB: %v", batch)
	}
	m.acknowledge()
	appendTitleRecords(t, path, M{"type": "ai-title", "sessionId": "large-claude", "aiTitle": "最新标题"})
	for i := 0; i < 4; i++ {
		batch = m.scan()
		if len(batch) > 0 && batch[0]["title"] == "最新标题" {
			break
		}
		m.acknowledge()
	}
	if len(batch) != 1 || batch[0]["title"] != "最新标题" {
		t.Fatalf("budgeted scan never reached newest title: %v", batch)
	}
	index := filepath.Join(filepath.Dir(path), "sessions-index.json")
	data, _ := json.Marshal(M{"entries": []M{{"sessionId": "large-claude", "customTitle": "索引中的手动命名", "summary": "索引摘要"}}})
	os.WriteFile(index, data, 0600)
	m.acknowledge()
	m.lastDiscovery = time.Time{}
	if batch = m.scan(); len(batch) != 1 || batch[0]["title"] != "索引中的手动命名" {
		t.Fatalf("legacy Claude index: %v", batch)
	}
	if len([]rune(cleanSessionTitle(strings.Repeat("名", 300)))) != 256 {
		t.Fatal("title length must be bounded in Unicode code points")
	}
}

func TestClaudeTitleScanOnlyReadsAppendedHistory(t *testing.T) {
	m, cwd, _ := fixtureMirror(t)
	path := filepath.Join(m.bases["claude"], "project/session.jsonl")
	writeRecords(t, path, []M{{"type": "user", "sessionId": "append-title", "cwd": cwd, "uuid": "first", "message": M{"content": "question"}},
		{"type": "file-history-snapshot", "data": strings.Repeat("x", 3*1024*1024)},
		{"type": "ai-title", "sessionId": "append-title", "aiTitle": "原始标题"}})
	for i := 0; i < 2; i++ {
		m.scan()
		m.acknowledge()
	}
	appendTitleRecords(t, path, M{"type": "file-history-snapshot", "data": strings.Repeat("y", 3*1024*1024)},
		M{"type": "ai-title", "sessionId": "append-title", "aiTitle": "追加后的标题"})
	m.scan()
	m.acknowledge()
	batch := m.scan()
	if len(batch) != 1 || batch[0]["title"] != "追加后的标题" {
		t.Fatalf("appended records should finish within two polls without rescanning older history: %v", batch)
	}
}
