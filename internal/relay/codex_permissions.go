package relay

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// A non-interactive exec cannot answer server-initiated approvals. Use a private
// app-server for ask mode when the thread has no existing shared owner.
func (t *Task) runOwnedCodex(cwd string, writable bool) (M, bool, error) {
	// Keep Unix socket paths below macOS's length limit.
	dir, err := os.MkdirTemp("/tmp", "relay-codex-")
	if err != nil {
		return nil, true, err
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "rpc.sock")
	cmd := exec.Command("codex", "app-server", "--listen", "unix://"+path)
	cmd.Dir = cwd
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	for _, env := range os.Environ() {
		if !strings.HasPrefix(env, "AM_TOKEN=") && !strings.HasPrefix(env, "AM_DEVICE_TOKEN=") {
			cmd.Env = append(cmd.Env, env)
		}
	}
	log, err := os.OpenFile(filepath.Join(dir, "server.log"), os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		return nil, true, err
	}
	defer log.Close()
	cmd.Stdout = log
	cmd.Stderr = log
	if err = cmd.Start(); err != nil {
		return nil, true, err
	}
	defer func() {
		syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		done := make(chan struct{})
		go func() { cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-done
		}
	}()
	duration := time.Duration(num(obj(t.job["payload"])["timeout"])) * time.Second
	if duration <= 0 {
		duration = 300 * time.Second
	}
	ctx, cancel := context.WithTimeout(t.ctx, duration)
	defer cancel()
	probe, stop := context.WithTimeout(ctx, 5*time.Second)
	defer stop()
	var rpc *codexRPC
	for {
		rpc, err = dialCodexPath(probe, path)
		if err == nil {
			break
		}
		select {
		case <-probe.Done():
			return nil, true, fmt.Errorf("无法启动 Codex 审批服务，请检查或升级目标设备 Codex：%w", err)
		case <-time.After(100 * time.Millisecond):
		}
	}
	defer rpc.close()
	sid := str(obj(t.job["agent"])["session_id"])
	var started []M
	if sid == "" {
		reply, startErr := rpc.call(ctx, "thread/start", M{"cwd": cwd, "approvalPolicy": "on-request", "sandbox": "workspace-write", "ephemeral": false})
		if startErr != nil {
			return nil, true, startErr
		}
		sid = str(obj(reply["thread"])["id"])
		if sid == "" {
			return nil, true, errors.New("Codex 未返回会话 ID，没有发送指令")
		}
		t.mu.Lock()
		t.session = sid
		t.mu.Unlock()
		// A new thread has no rollout to resume until its first turn is sent.
		started = append(started, reply)
	}
	return t.continueCodex(ctx, rpc, sid, cwd, writable, false, started...)
}

func (t *Task) answerCodexApproval(ctx context.Context, rpc *codexRPC, message, details M) {
	method := str(message["method"])
	decision, live := t.waitApproval(ctx, message, method, details)
	if !live {
		return
	}
	result := M{"decision": decision}
	if method == "item/permissions/requestApproval" {
		permissions := M{}
		if decision == "accept" {
			permissions = obj(obj(message["params"])["permissions"])
		}
		result = M{"permissions": permissions, "scope": "turn"}
	} else if decision == "accept" {
		// Do not synthesize session-wide or execpolicy grants. If this Codex
		// version offers a different decision shape, decline instead.
		if options := arr(obj(message["params"])["availableDecisions"]); len(options) > 0 {
			found := false
			for _, option := range options {
				if option == "accept" {
					found = true
				}
			}
			if !found {
				result = M{"decision": "decline"}
			}
		}
	}
	if err := rpc.write(M{"id": message["id"], "result": result}); err != nil {
		rpc.conn.Close()
	}
}
