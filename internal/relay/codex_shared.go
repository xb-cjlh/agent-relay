package relay

// Connect to the existing local Codex daemon. Starting a second app-server or
// resuming through exec would acquire a competing writer for a loaded thread.
import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

type codexRPC struct {
	conn    *websocket.Conn
	input   chan M
	stopped chan struct{}
	nextID  int
	pending []M
}

func codexSocket() string {
	home := os.Getenv("CODEX_HOME")
	if home == "" {
		home = expand("~/.codex")
	}
	return filepath.Join(expand(home), "app-server-control", "app-server-control.sock")
}

func dialCodex(ctx context.Context) (*codexRPC, error) {
	path := codexSocket()
	if _, err := os.Stat(path); err != nil {
		return nil, err
	}
	dialer := websocket.Dialer{HandshakeTimeout: 2 * time.Second, NetDialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", path)
	}}
	conn, _, err := dialer.DialContext(ctx, "ws://localhost/", nil)
	if err != nil {
		return nil, err
	}
	conn.SetReadLimit(16 * 1024 * 1024)
	rpc := &codexRPC{conn: conn, input: make(chan M, 64), stopped: make(chan struct{})}
	go func() {
		defer close(rpc.input)
		for {
			var message M
			if conn.ReadJSON(&message) != nil {
				return
			}
			select {
			case rpc.input <- message:
			case <-rpc.stopped:
				return
			}
		}
	}()
	if _, err = rpc.call(ctx, "initialize", M{"clientInfo": M{"name": "relay", "version": Version}, "capabilities": M{"experimentalApi": true}}); err != nil {
		rpc.close()
		return nil, err
	}
	if err = rpc.write(M{"method": "initialized", "params": M{}}); err != nil {
		rpc.close()
		return nil, err
	}
	return rpc, nil
}

func (r *codexRPC) close() { close(r.stopped); r.conn.Close() }
func (r *codexRPC) write(value M) error {
	r.conn.SetWriteDeadline(time.Now().Add(3 * time.Second))
	return r.conn.WriteJSON(value)
}
func (r *codexRPC) receive(ctx context.Context) (M, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case message, ok := <-r.input:
		if !ok {
			return nil, errors.New("本机 Codex 共享服务连接中断")
		}
		return message, nil
	}
}
func (r *codexRPC) call(ctx context.Context, method string, params M) (M, error) {
	r.nextID++
	id := r.nextID
	if err := r.write(M{"id": id, "method": method, "params": params}); err != nil {
		return nil, err
	}
	for {
		message, err := r.receive(ctx)
		if err != nil {
			return nil, err
		}
		if message["method"] != nil {
			// Approval requests may belong to the native client. Defer replies
			// until followCodexTurn can match our exact thread and turn.
			if len(r.pending) < 512 {
				r.pending = append(r.pending, message)
			} else {
				return nil, errors.New("Codex 通知过多，连接无法跟上；请核实会话状态")
			}
			continue
		}
		if int(num(message["id"])) != id {
			continue
		}
		if message["error"] != nil {
			return nil, fmt.Errorf("Codex %s: %s", method, str(obj(message["error"])["message"]))
		}
		return obj(message["result"]), nil
	}
}
func (r *codexRPC) rejectRequest(message M) error {
	// Never grant new tool permissions or fabricate answers on the user's behalf.
	method := str(message["method"])
	if strings.HasSuffix(method, "/requestApproval") {
		return r.write(M{"id": message["id"], "result": M{"decision": "decline"}})
	}
	if method == "item/tool/requestUserInput" {
		return r.write(M{"id": message["id"], "result": M{"answers": M{}}})
	}
	return r.write(M{"id": message["id"], "error": M{"code": -32601, "message": "Relay does not handle interactive tool requests"}})
}
func (r *codexRPC) loaded(ctx context.Context, sid string) (bool, error) {
	cursor := ""
	for page := 0; page < 100; page++ {
		params := M{"limit": 100}
		if cursor != "" {
			params["cursor"] = cursor
		}
		reply, err := r.call(ctx, "thread/loaded/list", params)
		if err != nil {
			return false, err
		}
		for _, id := range arr(reply["data"]) {
			if id == sid {
				return true, nil
			}
		}
		next := str(reply["nextCursor"])
		if next == "" || next == cursor {
			return false, nil
		}
		cursor = next
	}
	return false, errors.New("本机已加载的 Codex 会话过多")
}

func restoreCodexSettings(snapshot M, sid string) (M, error) {
	params := M{"threadId": sid, "approvalPolicy": snapshot["approvalPolicy"]}
	if reviewer := snapshot["approvalsReviewer"]; reviewer != nil {
		params["approvalsReviewer"] = reviewer
	}
	if profile := str(obj(snapshot["activePermissionProfile"])["id"]); profile != "" {
		params["permissions"] = profile
	} else if policy := obj(snapshot["sandbox"]); policy["type"] != nil {
		params["sandboxPolicy"] = policy
	} else {
		return nil, errors.New("无法读取本机 Codex 会话权限；没有发送指令，请先在本机检查")
	}
	return params, nil
}

func (t *Task) trySharedCodex(cwd string, writable bool) (result M, handled bool, err error) {
	agent := obj(t.job["agent"])
	sid := str(agent["session_id"])
	if agent["kind"] != "codex" || sid == "" {
		return nil, false, nil
	}
	duration := time.Duration(num(obj(t.job["payload"])["timeout"])) * time.Second
	if duration <= 0 {
		duration = 300 * time.Second
	}
	ctx, cancel := context.WithTimeout(t.ctx, duration)
	defer cancel()
	probe, stopProbe := context.WithTimeout(ctx, 5*time.Second)
	rpc, connectErr := dialCodex(probe)
	if connectErr != nil {
		stopProbe()
		return nil, false, nil // No prompt was submitted. Older/standalone CLIs use exec.
	}
	defer rpc.close()
	loaded, loadErr := rpc.loaded(probe, sid)
	stopProbe()
	if loadErr != nil || !loaded {
		return nil, false, nil
	}
	handled = true
	// Wait for the local turn without steering or cancelling work owned by another client.
	var snapshot M
	for {
		snapshot, err = rpc.call(ctx, "thread/resume", M{"threadId": sid, "excludeTurns": true})
		if err != nil {
			if t.ctx.Err() != nil {
				return M{"status": "cancelled"}, true, nil
			}
			return nil, true, err
		}
		status := str(obj(obj(snapshot["thread"])["status"])["type"])
		if status == "idle" {
			break
		}
		if status != "active" {
			return nil, true, fmt.Errorf("本机 Codex 会话当前状态为 %s，请先在本机检查", status)
		}
		timer := time.NewTimer(time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			if t.ctx.Err() != nil {
				return M{"status": "cancelled"}, true, nil
			}
			return nil, true, errors.New("等待本机 Codex 回复结束超时；没有发送指令")
		case <-timer.C:
		}
		rpc.pending = nil // The local turn's messages are mirrored separately.
	}
	restore, err := restoreCodexSettings(snapshot, sid)
	if err != nil {
		return nil, true, err
	}
	rpc.pending = nil
	if ctx.Err() != nil {
		if t.ctx.Err() != nil {
			return M{"status": "cancelled"}, true, nil
		}
		return nil, true, errors.New("任务已超时，没有发送指令")
	}
	policy := M{"type": "readOnly"}
	if writable {
		policy = M{"type": "workspaceWrite", "writableRoots": []string{cwd}, "networkAccess": false}
	}
	// Check that this daemon can restore future settings before submitting any input.
	if _, err = rpc.call(ctx, "thread/settings/update", M{"threadId": sid}); err != nil {
		return nil, true, fmt.Errorf("此 Codex 共享服务不支持独立设置后续轮次权限；请更新 Codex，或在本机退出此会话后再发送：%w", err)
	}
	// turn/start captures our sandbox for this turn. The settings update below
	// restores only future turns, without broadening the running Relay turn.
	restored := false
	restorePolicy := func() error {
		cleanup, done := context.WithTimeout(context.Background(), 4*time.Second)
		defer done()
		_, restoreErr := rpc.call(cleanup, "thread/settings/update", restore)
		if restoreErr == nil {
			restored = true
		}
		return restoreErr
	}
	defer func() {
		if restored {
			return
		}
		if restoreErr := restorePolicy(); restoreErr != nil {
			prefix := ""
			if err != nil {
				prefix = err.Error() + "；"
			}
			err = fmt.Errorf("%s恢复本机 Codex 后续轮次权限失败，请在本机检查：%s", prefix, restoreErr)
		}
	}()
	// Once sent, wait briefly for the acknowledgement even if cancellation arrives,
	// so cancellation can target the exact accepted turn instead of losing its ID.
	ackCtx, doneAck := context.WithTimeout(context.Background(), 5*time.Second)
	reply, err := rpc.call(ackCtx, "turn/start", M{"threadId": sid, "input": t.codexInput(), "approvalPolicy": "never", "sandboxPolicy": policy, "clientUserMessageId": "relay:" + str(t.job["id"])})
	doneAck()
	if err != nil {
		return nil, true, fmt.Errorf("向本机 Codex 提交指令时连接失败或请求被拒绝；未自动重试，请先核实原会话：%w", err)
	}
	turnID := str(obj(reply["turn"])["id"])
	if turnID == "" {
		return nil, true, errors.New("Codex 未返回本轮 ID；指令可能已接收，请先核实原会话，未自动重试")
	}
	// Restore future native turns immediately; this turn retains its captured policy.
	restorePolicy()
	t.mu.Lock()
	t.session = sid
	t.mu.Unlock()
	result, err = t.followCodexTurn(ctx, rpc, sid, turnID)
	if ctx.Err() != nil {
		// Interrupt only the exact turn created by this job, and keep the shared daemon alive.
		cleanup, done := context.WithTimeout(context.Background(), 4*time.Second)
		defer done()
		_, interruptErr := rpc.call(cleanup, "turn/interrupt", M{"threadId": sid, "turnId": turnID})
		if interruptErr != nil {
			return nil, true, fmt.Errorf("无法确认本轮已停止，请在本机核实：%w", interruptErr)
		}
		if _, stopErr := t.followCodexTurn(cleanup, rpc, sid, turnID); stopErr != nil {
			return nil, true, fmt.Errorf("已请求停止，但无法确认本轮已结束，请在本机核实：%w", stopErr)
		}
		if t.ctx.Err() != nil {
			return M{"status": "cancelled"}, true, nil
		}
		return nil, true, errors.New("执行超过任务时间限制，已请求停止本轮")
	}
	return result, true, err
}

func (t *Task) followCodexTurn(ctx context.Context, rpc *codexRPC, sid, turnID string) (M, error) {
	for {
		var message M
		var err error
		if len(rpc.pending) > 0 {
			message, rpc.pending = rpc.pending[0], rpc.pending[1:]
		} else {
			message, err = rpc.receive(ctx)
			if err != nil {
				return nil, fmt.Errorf("Codex 回复连接中断；本轮可能仍在执行，请核实原会话，未自动重试：%w", err)
			}
		}
		params := obj(message["params"])
		if params["threadId"] != sid {
			continue
		}
		if message["id"] != nil && message["method"] != nil {
			if params["turnId"] == turnID {
				if err = rpc.rejectRequest(message); err != nil {
					return nil, err
				}
			}
			continue
		}
		if message["method"] == "turn/completed" && obj(params["turn"])["id"] == turnID {
			turn := obj(params["turn"])
			switch turn["status"] {
			case "completed":
				return M{"status": "succeeded"}, nil
			case "interrupted":
				return M{"status": "cancelled"}, nil
			default:
				msg := str(obj(turn["error"])["message"])
				if msg == "" {
					msg = "Codex 执行失败"
				}
				return nil, errors.New(msg)
			}
		}
		if params["turnId"] != turnID {
			continue
		}
		t.mu.Lock()
		switch message["method"] {
		case "item/agentMessage/delta":
			t.partial = tail(t.partial+str(params["delta"]), MaxOutput)
		case "item/completed":
			item := obj(params["item"])
			if item["type"] == "agentMessage" {
				t.partial = ""
				t.output = tail(t.output+str(item["text"])+"\n", MaxOutput)
			}
		}
		t.mu.Unlock()
	}
}
