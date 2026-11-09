package relay

import (
	"context"
	"encoding/json"
	"io"
	"sync/atomic"
	"testing"
	"time"
)

func TestClaudeBrowserDecisionsKeepStreamingAndIgnoreCancelledRequests(t *testing.T) {
	for _, decision := range []string{"accept", "decline"} {
		t.Run(decision, func(t *testing.T) {
			task := newTask(M{"id": "claude-bridge", "agent": M{"cwd": t.TempDir()}})
			client, pending, released := realApprovalClient(t)
			task.client = client
			ctx, cancel := context.WithCancel(context.Background())
			reader, writer := io.Pipe()
			b := &claudeApprovals{task: task, ctx: ctx, cancel: cancel, input: writer, requests: map[string]context.CancelFunc{}}
			replies := make(chan M, 3)
			go func() {
				defer close(replies)
				decoder := json.NewDecoder(reader)
				for {
					var reply M
					if decoder.Decode(&reply) != nil {
						return
					}
					replies <- reply
				}
			}()
			defer reader.Close()
			defer func() { b.stop(); b.workers.Wait() }()
			emit := func(message M) { data, _ := json.Marshal(message); b.consume(data) }
			original := M{"command": "fixture command", "description": "temporary fixture"}
			for _, id := range []string{"live", "cancelled"} {
				emit(M{"type": "control_request", "request_id": id, "request": M{"subtype": "can_use_tool", "tool_name": "Bash", "input": original, "permission_suggestions": []M{{"type": "setMode", "mode": "bypassPermissions"}}}})
			}
			for i := 0; i < 2; i++ {
				select {
				case <-pending:
				case <-time.After(time.Second):
					t.Fatal("Claude approvals were not bridged concurrently")
				}
			}
			emit(M{"type": "stream_event", "event": M{"delta": M{"type": "text_delta", "text": "streaming while pending"}}})
			if task.progress()["output"] != "streaming while pending" {
				t.Fatal("waiting for permission blocked streaming")
			}
			emit(M{"type": "control_cancel_request", "request_id": "cancelled"})
			value := int32(1)
			if decision == "decline" {
				value = 2
			}
			atomic.StoreInt32(released, value)
			select {
			case reply := <-replies:
				response := obj(reply["response"])
				if response["request_id"] != "live" {
					t.Fatal("replied to an abandoned request", reply)
				}
				result := obj(response["response"])
				if (result["behavior"] == "allow") != (decision == "accept") {
					t.Fatal("ignored browser decision", reply)
				}
				if result["updatedPermissions"] != nil {
					t.Fatal("single approval became a saved permission", reply)
				}
				if decision == "accept" && obj(result["updatedInput"])["command"] != original["command"] {
					t.Fatal("tool input changed after approval", reply)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("browser decision did not reach Claude")
			}
			b.stop()
			b.workers.Wait()
			for reply := range replies {
				t.Fatal("sent a response after cancellation", reply)
			}
		})
	}
}

func TestClaudeModesCannotBypassLocalWriteLimit(t *testing.T) {
	for _, mode := range []string{"ask", "full"} {
		task := newTask(M{"kind": "prompt", "agent": M{"kind": "claude", "cwd": t.TempDir()}, "payload": M{"permission_mode": mode}})
		task.run(nil, false)
		if task.result["status"] != "failed" {
			t.Fatal("browser overrode device permissions", task.result)
		}
	}
}
