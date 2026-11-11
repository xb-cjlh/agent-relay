package relay

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Keep a private app-server available for approvals and in-flight input when
// the thread has no existing shared owner. Legacy CLIs can still use exec.
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
	defer func() {
		log.Close()
		if source, err := os.Open(log.Name()); err == nil {
			defer source.Close()
			if stat, err := source.Stat(); err == nil && stat.Size() > 8000 {
				source.Seek(stat.Size()-8000, io.SeekStart)
			}
			if data, err := io.ReadAll(io.LimitReader(source, 8000)); err == nil {
				diagnosticWriter{task: t}.Write(data)
			}
		}
	}()
	cmd.Stdout = log
	cmd.Stderr = log
	if err = cmd.Start(); err != nil {
		return nil, true, err
	}
	stopped := make(chan struct{})
	go func() { cmd.Wait(); close(stopped) }()
	defer func() {
		select {
		case <-stopped:
			return
		default:
		}
		syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-stopped:
		case <-time.After(2 * time.Second):
			syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			<-stopped
		}
	}()
	ctx, cancel := t.executionContext()
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
		case <-stopped:
			if str(obj(t.job["payload"])["permission_mode"]) != "ask" && !yes(t.job["steer_only"]) {
				return nil, false, nil // No thread/input submitted; retain legacy exec.
			}
			return nil, true, errors.New("Codex 共享服务未能启动，请检查目标设备 Codex")
		case <-probe.Done():
			if t.ctx.Err() != nil {
				return M{"status": "cancelled"}, true, nil
			}
			if ctx.Err() != nil {
				return nil, true, fmt.Errorf("执行达到设定的 %v 秒上限，已停止本轮", obj(t.job["payload"])["timeout"])
			}
			return nil, true, fmt.Errorf("无法启动 Codex 共享服务，请检查或升级目标设备 Codex：%w", err)
		case <-time.After(100 * time.Millisecond):
		}
	}
	defer rpc.close()
	sid := str(obj(t.job["agent"])["session_id"])
	var started []M
	if sid == "" {
		// The actual turn receives its requested permissions in continueCodex.
		reply, startErr := rpc.call(ctx, "thread/start", M{"cwd": cwd, "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": false})
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
