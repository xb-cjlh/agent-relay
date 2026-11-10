package relay

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func fixtureMirror(t *testing.T) (*Mirror, string, string) {
	t.Helper()
	root := t.TempDir()
	shared := filepath.Join(root, "shared")
	if e := os.MkdirAll(shared, 0700); e != nil {
		t.Fatal(e)
	}
	t.Setenv("CODEX_HOME", filepath.Join(root, "codex"))
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(root, "claude"))
	return newMirror([]string{shared}), shared, root
}
func records(cwd string, busy bool) []M {
	r := []M{{"type": "session_meta", "payload": M{"id": "native-codex-1", "cwd": cwd}}, {"type": "event_msg", "payload": M{"type": "task_started"}}, {"type": "response_item", "payload": M{"type": "message", "role": "user", "content": []M{{"type": "input_text", "text": "terminal question"}}}}, {"type": "response_item", "payload": M{"type": "message", "role": "assistant", "content": []M{{"type": "output_text", "text": "terminal answer"}}}}}
	if !busy {
		r = append(r, M{"type": "event_msg", "payload": M{"type": "task_complete"}})
	}
	return r
}
func writeRecords(t *testing.T, path string, records []M) {
	t.Helper()
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		t.Fatal(e)
	}
	var b strings.Builder
	for _, r := range records {
		v, e := json.Marshal(r)
		if e != nil {
			t.Fatal(e)
		}
		b.Write(v)
		b.WriteByte('\n')
	}
	if e := os.WriteFile(path, []byte(b.String()), 0600); e != nil {
		t.Fatal(e)
	}
}
func TestMirrorIncrementalPartialRecordsAndScope(t *testing.T) {
	m, shared, root := fixtureMirror(t)
	m.scoped = true // Explicit virtual devices retain directory-scoped discovery.
	path := filepath.Join(m.bases["codex"], "2026/session.jsonl")
	writeRecords(t, path, records(shared, false))
	writeRecords(t, filepath.Join(m.bases["codex"], "outside.jsonl"), records(root, false))
	b := m.scan()
	if len(b) != 1 {
		t.Fatalf("sessions: %v", b)
	}
	messages := arr(b[0]["messages"])
	if len(messages) != 2 || obj(messages[0])["content"] != "terminal question" || yes(b[0]["busy"]) {
		t.Fatalf("history: %v", b)
	}
	first := str(obj(messages[0])["id"])
	if str(obj(arr(m.scan()[0]["messages"])[0])["id"]) != first {
		t.Fatal("unacknowledged messages must retain IDs")
	}
	m.acknowledge()
	if len(m.scan()) != 0 {
		t.Fatal("acknowledged messages replayed")
	}
	event := records(shared, false)[2]
	obj(event["payload"])["content"] = []M{{"type": "input_text", "text": "new terminal message"}}
	data, _ := json.Marshal(event)
	f, e := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if e != nil {
		t.Fatal(e)
	}
	f.Write(data[:25])
	f.Close()
	if len(arr(m.scan()[0]["messages"])) != 0 {
		t.Fatal("partial record consumed")
	}
	m.acknowledge()
	f, _ = os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	f.Write(data[25:])
	f.WriteString("\n")
	f.Close()
	if obj(arr(m.scan()[0]["messages"])[0])["content"] != "new terminal message" {
		t.Fatal("completed record was not read")
	}
}
func TestMachineMirrorDiscoversAllDirectoriesAndUpdatesPermissions(t *testing.T) {
	m, shared, root := fixtureMirror(t)
	for i, cwd := range []string{shared, root, filepath.Join(root, "deleted")} {
		r := records(cwd, false)
		obj(r[0]["payload"])["id"] = fmt.Sprint("session-", i)
		writeRecords(t, filepath.Join(m.bases["codex"], fmt.Sprint(i, ".jsonl")), r)
	}
	batches := m.scan()
	if len(batches) != 3 {
		t.Fatalf("want all three directories: %v", batches)
	}
	controls := map[string]string{}
	for _, b := range batches {
		controls[str(b["session_id"])] = str(b["control_reason"])
	}
	if controls["session-0"] != "" || controls["session-1"] != "" || controls["session-2"] != "cwd_missing" {
		t.Fatalf("controls: %v", controls)
	}
	m.acknowledge()
	if len(m.scan()) != 0 {
		t.Fatal("unchanged sessions replayed")
	}
	if e := os.Remove(shared); e != nil {
		t.Fatal(e)
	}
	batches = m.scan()
	if len(batches) != 1 || batches[0]["control_reason"] != "cwd_missing" {
		t.Fatalf("deleted cwd: %v", batches)
	}
}
func TestMachineMirrorDoesNotTruncateDiscoveryAtFifty(t *testing.T) {
	m, shared, _ := fixtureMirror(t)
	for i := 0; i < 125; i++ {
		r := records(shared, false)
		obj(r[0]["payload"])["id"] = fmt.Sprint("session-", i)
		writeRecords(t, filepath.Join(m.bases["codex"], fmt.Sprint(i, ".jsonl")), r)
	}
	seen := map[string]bool{}
	for round := 0; round < 8; round++ {
		m.lastDiscovery = time.Time{}
		for _, b := range m.scan() {
			seen[str(b["session_id"])] = true
		}
		m.acknowledge()
	}
	if len(seen) != 125 {
		t.Fatalf("discovered %d of 125", len(seen))
	}
}
func TestClaudeMirrorFiltersThinkingToolsAndSubagents(t *testing.T) {
	m, shared, _ := fixtureMirror(t)
	event := func(role string, content interface{}, stop string) M {
		return M{"type": role, "sessionId": "claude-session-1", "cwd": shared, "uuid": role, "message": M{"content": content, "stop_reason": stop}}
	}
	path := filepath.Join(m.bases["claude"], "project/session.jsonl")
	writeRecords(t, path, []M{event("user", "hello", ""), event("assistant", []M{{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "hi"}}, "end_turn"), event("user", []M{{"type": "tool_result", "content": "private tool data"}}, "")})
	writeRecords(t, filepath.Join(m.bases["claude"], "project/subagents/agent.jsonl"), []M{event("user", "internal", "")})
	b := m.scan()
	if len(b) != 1 || yes(b[0]["busy"]) || len(arr(b[0]["messages"])) != 2 {
		t.Fatalf("unexpected Claude transcript: %v", b)
	}
	if obj(arr(b[0]["messages"])[1])["content"] != "hi" {
		t.Fatal("assistant text missing")
	}
}
func TestMirrorMarksCompactionSummariesWithoutUploadingTheirText(t *testing.T) {
	for _, kind := range []string{"codex", "claude"} {
		t.Run(kind, func(t *testing.T) {
			m, shared, _ := fixtureMirror(t)
			text := "## Task and authorization\n\nPublic text may use this heading too."
			var public, internal M
			if kind == "codex" {
				public = M{"type": "response_item", "payload": M{"type": "message", "role": "assistant", "phase": "final_answer", "content": []M{{"type": "output_text", "text": text}}}}
				internal = copyMap(public)
				internal["metadata"] = M{"compaction_output": true, "client_authored": false}
			} else {
				public = M{"type": "assistant", "sessionId": "summary-session", "cwd": shared, "uuid": "public", "message": M{"content": []M{{"type": "text", "text": text}}, "stop_reason": "end_turn"}}
				internal = M{"type": "user", "sessionId": "summary-session", "cwd": shared, "uuid": "internal", "isCompactSummary": true, "isMeta": true, "message": M{"content": text}}
			}
			decode := func(event M) M {
				data, _ := json.Marshal(event)
				var parsed M
				if err := json.Unmarshal(data, &parsed); err != nil {
					t.Fatal(err)
				}
				return parsed
			}
			public, internal = decode(public), decode(internal)
			meta := M{"kind": kind, "busy": false}
			if got := parseMessage(public, meta, 1); got == nil || got["content"] != text || yes(got["internal"]) {
				t.Fatalf("public message changed: %v", got)
			}
			got := parseMessage(internal, meta, 1)
			if got == nil || !yes(got["internal"]) || got["content"] != "" || yes(meta["busy"]) {
				t.Fatalf("internal summary must be a text-free tombstone: %v", got)
			}
			events := []M{public, internal}
			if kind == "codex" {
				events = append([]M{{"type": "session_meta", "payload": M{"id": "summary-session", "cwd": shared}}}, events...)
			}
			writeRecords(t, filepath.Join(m.bases[kind], "summary.jsonl"), events)
			batch := m.scan()
			if len(batch) != 1 || len(arr(batch[0]["messages"])) != 2 {
				t.Fatalf("missing classification: %v", batch)
			}
			messages := arr(batch[0]["messages"])
			if str(obj(messages[0])["id"]) == str(obj(messages[1])["id"]) || !yes(obj(messages[1])["internal"]) {
				t.Fatalf("distinct stable IDs required: %v", messages)
			}
		})
	}
}
func TestStoppedRelayTurnReleasesBusyUntilNewNativeTurn(t *testing.T) {
	m, shared, _ := fixtureMirror(t)
	path := filepath.Join(m.bases["codex"], "session.jsonl")
	writeRecords(t, path, records(shared, true))
	if !yes(m.scan()[0]["busy"]) {
		t.Fatal("active turn not busy")
	}
	m.acknowledge()
	m.release("codex", "native-codex-1")
	if yes(m.scan()[0]["busy"]) {
		t.Fatal("released turn still busy")
	}
	m.acknowledge()
	f, _ := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	f.WriteString(`{"type":"event_msg","payload":{"type":"task_started"}}` + "\n")
	f.Close()
	if !yes(m.scan()[0]["busy"]) {
		t.Fatal("new turn must become busy")
	}
}
func TestStreamingTextFinalMessageDoesNotDuplicate(t *testing.T) {
	for _, kind := range []string{"codex", "claude"} {
		t.Run(kind, func(t *testing.T) {
			task := newTask(M{"id": "test"})
			consume := func(e M) { b, _ := json.Marshal(e); task.consume(b, kind) }
			if kind == "claude" {
				consume(M{"type": "stream_event", "event": M{"delta": M{"type": "text_delta", "text": "Hello "}}})
				consume(M{"type": "stream_event", "event": M{"delta": M{"type": "text_delta", "text": "world"}}})
			} else {
				consume(M{"type": "item.updated", "item": M{"type": "agent_message", "text": "Hello world"}})
			}
			if task.progress()["output"] != "Hello world" {
				t.Fatalf("stream: %v", task.progress())
			}
			if kind == "claude" {
				consume(M{"type": "assistant", "message": M{"content": []M{{"type": "text", "text": "Hello world"}}}})
			} else {
				consume(M{"type": "item.completed", "item": M{"type": "agent_message", "text": "Hello world"}})
			}
			if task.progress()["output"] != "Hello world\n" {
				t.Fatalf("final: %v", task.progress())
			}
		})
	}
}
func TestProgressWhileOutputChanges(t *testing.T) {
	task := newTask(M{"id": "test"})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			task.consume([]byte(`{"type":"item.updated","item":{"type":"agent_message","text":"live text"}}`), "codex")
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			task.progress()
		}
	}()
	wg.Wait()
}
func TestVirtualDiscoveryScopeRejectsParentTraversalAndSymlinks(t *testing.T) {
	root := t.TempDir()
	shared := filepath.Join(root, "shared")
	os.Mkdir(shared, 0700)
	outside := filepath.Join(root, "secret")
	os.WriteFile(outside, []byte("secret"), 0600)
	os.Symlink(outside, filepath.Join(shared, "link"))
	for _, p := range []string{"../secret", "link", outside} {
		if _, e := virtualDiscoveryPath(p, []string{shared}); e == nil {
			t.Fatalf("escaped through %s", p)
		}
	}
	inside := filepath.Join(shared, "inside")
	os.WriteFile(inside, []byte("ok"), 0600)
	got, e := virtualDiscoveryPath("inside", []string{shared})
	canonical, _ := filepath.EvalSymlinks(inside)
	if e != nil || got != canonical {
		t.Fatalf("inside: %s %v", got, e)
	}
}
func TestRemoteFilesAllowOtherDirectoriesAndSymlinks(t *testing.T) {
	base := t.TempDir()
	outside := filepath.Join(t.TempDir(), "outside.txt")
	if e := os.WriteFile(outside, []byte("outside project"), 0600); e != nil {
		t.Fatal(e)
	}
	link := filepath.Join(base, "link")
	if e := os.Symlink(outside, link); e != nil {
		t.Fatal(e)
	}
	for _, path := range []string{outside, link} {
		task := newTask(M{"kind": "file_read", "payload": M{"path": path}})
		result, e := task.readFile([]string{base})
		if e != nil || obj(result["file"])["sha256"] != hash("outside project") {
			t.Fatalf("accessible file rejected: %v %v", result, e)
		}
	}
}
func TestResumePreservesPermissionsAndSession(t *testing.T) {
	for _, writable := range []bool{false, true} {
		cmd := commandFor(M{"kind": "codex", "session_id": "session-1"}, writable)
		sandbox := "read-only"
		if writable {
			sandbox = "workspace-write"
		}
		all := strings.Join(cmd, " ")
		if !strings.Contains(all, `sandbox_mode="`+sandbox+`"`) || !strings.Contains(all, `approval_policy="never"`) || !strings.Contains(all, "exec resume") || cmd[len(cmd)-2] != "session-1" || cmd[len(cmd)-1] != "-" {
			t.Fatalf("unsafe resume: %v", cmd)
		}
	}
}
