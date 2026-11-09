package relay

import (
	"context"
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

// Claude's stdio control protocol keeps native tools, settings and transcripts.
// Only individual can_use_tool decisions are bridged; permission suggestions
// never become persistent allow rules.
type claudeApprovals struct {
	task        *Task
	ctx         context.Context
	cancel      context.CancelFunc
	input       io.WriteCloser
	initialized chan error
	writeMu     sync.Mutex
	mu          sync.Mutex
	requests    map[string]context.CancelFunc
	workers     sync.WaitGroup
	closed      bool
	result      bool
}

func (b *claudeApprovals) write(message M) error {
	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	if b.ctx.Err() != nil {
		return b.ctx.Err()
	}
	return json.NewEncoder(b.input).Encode(message)
}
func (b *claudeApprovals) stop() {
	b.mu.Lock()
	b.closed = true
	b.cancel()
	b.mu.Unlock()
	b.input.Close()
}
func (b *claudeApprovals) consume(line []byte) {
	var message M
	if json.Unmarshal(line, &message) != nil {
		b.task.consume(line, "claude")
		return
	}
	switch message["type"] {
	case "control_response":
		response := obj(message["response"])
		if response["request_id"] == "relay_initialize" {
			var err error
			if response["subtype"] != "success" {
				err = errors.New("Claude 审批协议初始化失败：" + str(response["error"]))
			}
			select {
			case b.initialized <- err:
			default:
			}
		}
	case "control_cancel_request":
		b.mu.Lock()
		if cancel := b.requests[str(message["request_id"])]; cancel != nil {
			cancel()
		}
		b.mu.Unlock()
	case "control_request":
		id, request := str(message["request_id"]), obj(message["request"])
		b.mu.Lock()
		if b.closed || b.requests[id] != nil {
			b.mu.Unlock()
			return
		}
		ctx, cancel := context.WithCancel(b.ctx)
		b.requests[id] = cancel
		tooMany := len(b.requests) > 200
		b.workers.Add(1)
		b.mu.Unlock()
		go func() {
			defer b.workers.Done()
			defer cancel()
			response := M{"subtype": "success", "request_id": id}
			if request["subtype"] != "can_use_tool" {
				response["subtype"], response["error"] = "error", "Relay does not handle this interactive request"
			} else {
				decision, live := "decline", ctx.Err() == nil
				if !tooMany && request["tool_name"] != "AskUserQuestion" {
					details := M{}
					for k, v := range request {
						details[k] = v
					}
					details["cwd"] = obj(b.task.job["agent"])["cwd"]
					details["reason"] = request["decision_reason"]
					decision, live = b.task.waitApproval(ctx, message, "claude/tool/requestApproval", details)
				}
				if !live || ctx.Err() != nil {
					return
				}
				result := M{"behavior": "deny", "message": "此次工具调用未获授权；请根据用户后续消息继续。"}
				if decision == "accept" {
					result = M{"behavior": "allow", "updatedInput": obj(request["input"])}
				}
				response["response"] = result
			}
			if ctx.Err() == nil {
				if err := b.write(M{"type": "control_response", "response": response}); err != nil {
					b.stop()
				}
			}
		}()
	default:
		b.task.consume(line, "claude")
		if message["type"] == "result" {
			b.mu.Lock()
			b.result = true
			b.mu.Unlock()
			b.stop() // One Relay job owns one turn, including any pending approvals.
		}
	}
}

func (t *Task) runClaudeApprovals(cwd string) (M, error) {
	ctx, cancel := t.executionContext()
	defer cancel()
	args := commandFor(obj(t.job["agent"]), true, "ask")
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Dir, cmd.SysProcAttr = cwd, &syscall.SysProcAttr{Setpgid: true}
	for _, env := range os.Environ() {
		if !strings.HasPrefix(env, "AM_TOKEN=") && !strings.HasPrefix(env, "AM_DEVICE_TOKEN=") {
			cmd.Env = append(cmd.Env, env)
		}
	}
	input, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	approvalCtx, stopApprovals := context.WithCancel(ctx)
	b := &claudeApprovals{task: t, ctx: approvalCtx, cancel: stopApprovals, input: input, initialized: make(chan error, 1), requests: map[string]context.CancelFunc{}}
	cmd.Stdout = &eventWriter{task: t, kind: "claude", line: b.consume}
	cmd.Stderr = diagnosticWriter{task: t}
	if err = cmd.Start(); err != nil {
		b.stop()
		return nil, err
	}
	wait := make(chan error, 1)
	go func() { wait <- cmd.Wait() }()
	exited := false
	defer func() {
		b.stop()
		if !exited {
			syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
			select {
			case <-wait:
			case <-time.After(2 * time.Second):
				syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				<-wait
			}
		}
		b.workers.Wait()
	}()
	go func() {
		if err := b.write(M{"type": "control_request", "request_id": "relay_initialize", "request": M{"subtype": "initialize"}}); err != nil {
			select {
			case b.initialized <- err:
			default:
			}
		}
	}()
	probe := time.NewTimer(10 * time.Second)
	defer probe.Stop()
	select {
	case err = <-b.initialized:
		if err != nil {
			return nil, err
		}
	case err = <-wait:
		exited = true
		return nil, fmt.Errorf("Claude 审批服务退出，请检查或升级 Claude Code；没有发送指令：%v", err)
	case <-probe.C:
		return nil, errors.New("Claude 审批协议未响应，请检查或升级 Claude Code；没有发送指令")
	case <-ctx.Done():
		if t.ctx.Err() != nil {
			return M{"status": "cancelled"}, nil
		}
		return nil, errors.New("任务已超时，没有发送指令")
	}
	go func() {
		if err := b.write(t.claudeInput()); err != nil {
			b.stop()
		}
	}()
	select {
	case err = <-wait:
		exited = true
	case <-ctx.Done():
		if t.ctx.Err() != nil {
			return M{"status": "cancelled"}, nil
		}
		return nil, errors.New("执行超过任务时间限制，已停止 Claude 本轮")
	}
	t.mu.Lock()
	modelError := t.modelError
	t.mu.Unlock()
	if modelError != "" {
		return nil, errors.New(modelError)
	}
	if err != nil {
		return nil, fmt.Errorf("Claude 执行失败: %w", err)
	}
	b.mu.Lock()
	finished := b.result
	b.mu.Unlock()
	if !finished {
		return nil, errors.New("Claude 未返回本轮结果，请检查原会话；没有自动重试")
	}
	return M{"status": "succeeded"}, nil
}
