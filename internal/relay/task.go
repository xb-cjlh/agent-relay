package relay

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

func commandFor(agent M, writable bool, modes ...string) []string {
	codexMode := "device"
	if len(modes) > 0 {
		codexMode = modes[0]
	}
	sid := str(agent["session_id"])
	if agent["kind"] == "codex" {
		cmd := []string{"codex", "exec"}
		if sid != "" {
			cmd = append(cmd, "resume")
		}
		sandbox := "read-only"
		approval := "never"
		if writable {
			sandbox = "workspace-write"
		}
		if codexMode == "ask" {
			sandbox = "workspace-write"
			approval = "on-request"
		} else if codexMode == "full" {
			sandbox = "danger-full-access"
		}
		cmd = append(cmd, "--json", "--skip-git-repo-check", "-c", `approval_policy="`+approval+`"`, "-c", `sandbox_mode="`+sandbox+`"`)
		if sid != "" {
			cmd = append(cmd, sid)
		}
		return append(cmd, "-")
	}
	permission := "dontAsk"
	if codexMode == "ask" {
		permission = "default"
	} else if codexMode == "full" {
		permission = "bypassPermissions"
	}
	cmd := []string{"claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--permission-mode", permission}
	if codexMode == "ask" {
		cmd = append(cmd, "--tools", "default", "--input-format", "stream-json", "--permission-prompt-tool", "stdio")
	} else if codexMode == "full" {
		cmd = append(cmd, "--tools", "default")
	} else if writable {
		// Keep Claude's normal tools, settings, skills and configured MCP servers.
		// Approve the core coding tools because a background task cannot answer
		// terminal prompts; local deny rules and MCP permissions still apply.
		cmd = append(cmd, "--tools", "default", "--allowedTools", "Read,Glob,Grep,Edit,Write,Bash")
	} else {
		tools := "Read,Glob,Grep"
		cmd = append(cmd, "--tools", tools, "--allowedTools", tools, "--strict-mcp-config", "--mcp-config", `{"mcpServers":{}}`)
	}
	if sid != "" {
		cmd = append(cmd, "--resume", sid)
	}
	return cmd
}

type Task struct {
	job                                  M
	mu                                   sync.Mutex
	output, partial, session, modelError string
	diagnostics                          string
	result                               M
	cancel                               context.CancelFunc
	ctx                                  context.Context
	done                                 chan struct{}
	client                               *Client
	images                               []taskImage
	imageDir                             string
	attachments                          []M
	attachmentDir                        string
	codexPath, codexTurn                 string
	codexOverride                        string
	nativeTitle                          bool
}

func newTask(job M) *Task {
	ctx, cancel := context.WithCancel(context.Background())
	return &Task{job: job, cancel: cancel, ctx: ctx, done: make(chan struct{})}
}
func (t *Task) progress() M {
	t.mu.Lock()
	defer t.mu.Unlock()
	var sid interface{}
	if t.session != "" {
		sid = t.session
	}
	return M{"id": t.job["id"], "output": tail(t.output+t.partial, MaxOutput), "diagnostics": t.diagnostics, "session_id": sid, "steerable": t.codexPath != "" && t.codexTurn != ""}
}

func (t *Task) codexControl() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.codexTurn == "" {
		return ""
	}
	return t.codexPath
}

// Zero or an omitted timeout means a conversation runs until completion or
// cancellation. Connection probes retain their own short deadlines.
func (t *Task) executionContext() (context.Context, context.CancelFunc) {
	duration := time.Duration(num(obj(t.job["payload"])["timeout"])) * time.Second
	if duration <= 0 {
		return context.WithCancel(t.ctx)
	}
	return context.WithTimeout(t.ctx, duration)
}

type diagnosticWriter struct{ task *Task }

func (w diagnosticWriter) Write(data []byte) (int, error) {
	w.task.mu.Lock()
	w.task.diagnostics = tail(w.task.diagnostics+string(data), 8000)
	w.task.mu.Unlock()
	return len(data), nil
}
func (t *Task) consume(line []byte, kind string) {
	var e M
	t.mu.Lock()
	defer t.mu.Unlock()
	appendText := func(s string) { t.output = tail(t.output+s, MaxOutput) }
	if json.Unmarshal(line, &e) != nil {
		t.diagnostics = tail(t.diagnostics+string(line), 8000)
		return
	}
	if kind == "codex" {
		if e["type"] == "thread.started" {
			t.session = str(e["thread_id"])
		}
		item := obj(e["item"])
		if item["type"] == "agent_message" {
			if e["type"] == "item.updated" {
				t.partial = tail(str(item["text"]), MaxOutput)
			}
			if e["type"] == "item.completed" {
				t.partial = ""
				appendText(str(item["text"]) + "\n")
			}
		}
		if e["type"] == "error" || e["type"] == "turn.failed" {
			msg := str(e["message"])
			if msg == "" {
				msg = str(obj(e["error"])["message"])
			}
			if msg == "" {
				msg = "Agent 执行失败"
			}
			t.modelError = msg
			appendText(msg + "\n")
		}
	} else {
		if sid := str(e["session_id"]); sid != "" {
			t.session = sid
		}
		if e["type"] == "stream_event" {
			delta := obj(obj(e["event"])["delta"])
			if delta["type"] == "text_delta" {
				t.partial = tail(t.partial+str(delta["text"]), MaxOutput)
			}
		}
		if e["type"] == "assistant" {
			t.partial = ""
			for _, part := range arr(obj(e["message"])["content"]) {
				p := obj(part)
				if p["type"] == "text" {
					appendText(str(p["text"]) + "\n")
				}
			}
		}
		if e["type"] == "result" {
			if t.output == "" && str(e["result"]) != "" {
				t.partial = ""
				appendText(str(e["result"]) + "\n")
			}
			if yes(e["is_error"]) {
				t.modelError = str(e["result"])
				if t.modelError == "" {
					t.modelError = "Claude 执行失败"
				}
			}
		}
	}
}

// Retain only bounded lines; long tool events are discarded until the next newline.
type eventWriter struct {
	task    *Task
	kind    string
	buffer  []byte
	discard bool
	line    func([]byte)
}

func (w *eventWriter) Write(p []byte) (int, error) {
	n := len(p)
	for len(p) > 0 {
		i := bytes.IndexByte(p, '\n')
		end := i + 1
		if i < 0 {
			end = len(p)
		}
		if !w.discard {
			if len(w.buffer)+end > 1024*1024 {
				w.buffer = nil
				w.discard = true
			} else {
				w.buffer = append(w.buffer, p[:end]...)
			}
		}
		if i >= 0 {
			if !w.discard {
				if w.line != nil {
					w.line(w.buffer)
				} else {
					w.task.consume(w.buffer, w.kind)
				}
			}
			w.buffer = nil
			w.discard = false
		}
		p = p[end:]
	}
	return n, nil
}
func (t *Task) run(roots []string, writable bool) {
	defer close(t.done)
	var result M
	var err error
	if t.ctx.Err() != nil {
		result = M{"status": "cancelled"}
	} else if t.job["kind"] == "file_read" {
		result, err = t.readFile(roots)
	} else if t.job["kind"] == "directory_list" {
		result, err = t.listDirectory()
	} else {
		result, err = t.runAgent(roots, writable)
	}
	if err != nil {
		result = M{"status": "failed", "error": err.Error()}
	}
	if result == nil {
		result = M{"status": "failed", "error": "任务意外终止"}
	}
	progress := t.progress()
	result["output"] = progress["output"]
	result["diagnostics"] = progress["diagnostics"]
	if progress["session_id"] != nil {
		result["session_id"] = progress["session_id"]
	}
	if yes(t.job["steer_only"]) {
		result["steering"] = true
	}
	t.mu.Lock()
	t.result = result
	t.mu.Unlock()
}
func (t *Task) readFile(roots []string) (M, error) {
	base := ""
	if len(roots) > 0 {
		base = roots[0]
	}
	path, err := accessiblePath(str(obj(t.job["payload"])["path"]), base)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("指定路径不是普通文件")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err = f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("指定路径不是普通文件")
	}
	data, err := io.ReadAll(io.LimitReader(f, MaxFile+1))
	if err != nil {
		return nil, err
	}
	if len(data) > MaxFile {
		return nil, errors.New("文件超过 16 MiB 限制")
	}
	sha := hashBytes(data)
	if sha == str(obj(t.job["payload"])["if_sha256"]) {
		return M{"status": "succeeded", "file": M{"unchanged": true, "sha256": sha}}, nil
	}
	if t.client != nil {
		result, err := t.uploadFile(info.Name(), data, sha)
		if err != nil || result != nil {
			return result, err
		}
	}
	return M{"status": "succeeded", "file": M{"name": info.Name(), "data": base64.StdEncoding.EncodeToString(data), "sha256": sha}}, nil
}
func (t *Task) runAgent(roots []string, writable bool) (M, error) {
	agent := obj(t.job["agent"])
	mode := str(obj(t.job["payload"])["permission_mode"])
	if mode != "" && mode != "device" {
		if (agent["kind"] != "codex" && agent["kind"] != "claude") || (mode != "ask" && mode != "full") {
			return nil, errors.New("不支持的 Agent 权限模式")
		}
		if !writable {
			return nil, errors.New("设备未启用 --allow-agent-writes，不能变更 Agent 执行权限")
		}
	}
	cwd, err := accessiblePath(str(agent["cwd"]), "")
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(cwd)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return nil, errors.New("会话工作目录不存在")
	}
	if err = t.prepareInputs(); err != nil {
		if t.ctx.Err() != nil {
			return M{"status": "cancelled"}, nil
		}
		return nil, err
	}
	if agent["kind"] == "claude" && (mode == "ask" || t.nativeTitle) {
		return t.runClaudeApprovals(cwd, writable)
	}
	if result, handled, err := t.trySharedCodex(cwd, writable); handled {
		return result, err
	}
	if t.ctx.Err() != nil {
		return M{"status": "cancelled"}, nil
	}
	codexMode := str(obj(t.job["payload"])["permission_mode"])
	if codexMode == "" || codexMode == "device" {
		codexMode = "device"
	}
	args := commandFor(agent, writable, codexMode)
	args = t.imageArgs(args, str(agent["kind"]))
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Dir = cwd
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	env := []string{}
	for _, v := range os.Environ() {
		if !strings.HasPrefix(v, "AM_TOKEN=") && !strings.HasPrefix(v, "AM_DEVICE_TOKEN=") && !strings.HasPrefix(v, "NO_COLOR=") {
			env = append(env, v)
		}
	}
	cmd.Env = append(env, "NO_COLOR=1")
	cmd.Stdin = strings.NewReader(t.agentInput(str(agent["kind"])))
	writer := &eventWriter{task: t, kind: str(agent["kind"])}
	cmd.Stdout = writer
	cmd.Stderr = diagnosticWriter{task: t}
	if err = cmd.Start(); err != nil {
		return nil, err
	}
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	ctx, cancel := t.executionContext()
	defer cancel()
	var result M
	select {
	case err = <-wait:
	case <-ctx.Done():
		if t.ctx.Err() != nil {
			result = M{"status": "cancelled"}
		} else {
			result = M{"status": "failed", "error": fmt.Sprintf("执行达到设定的 %v 秒上限，已停止本轮", obj(t.job["payload"])["timeout"])}
		}
	}
	if result != nil {
		syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-wait:
		case <-time.After(2 * time.Second):
			syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-wait
		}
		return result, nil
	}
	if len(writer.buffer) > 0 {
		t.consume(writer.buffer, str(agent["kind"]))
	}
	t.mu.Lock()
	modelError := t.modelError
	t.mu.Unlock()
	if modelError != "" {
		return nil, errors.New(modelError)
	}
	if err != nil {
		if agent["kind"] == "codex" && strings.Contains(str(t.progress()["diagnostics"]), "already has an active writer") {
			return nil, errors.New("此 Codex 会话仍被本机进程占用，且无法连接持有它的共享服务；请在目标设备退出该会话后再发送，或使用支持共享服务的 Codex。无需删除会话或锁文件")
		}
		return nil, fmt.Errorf("Agent 执行失败: %w", err)
	}
	return M{"status": "succeeded"}, nil
}
