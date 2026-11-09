package relay

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestConversationContextHasNoImplicitDeadline(t *testing.T) {
	for _, payload := range []M{{}, {"timeout": 0}} {
		task := newTask(M{"payload": payload})
		ctx, cancel := task.executionContext()
		if deadline, limited := ctx.Deadline(); limited {
			t.Fatal("conversation still has an implicit runtime limit", deadline)
		}
		task.cancel()
		if ctx.Err() != context.Canceled {
			t.Fatal("unlimited task ignored manual cancellation")
		}
		cancel()
	}
	task := newTask(M{"payload": M{"timeout": 12}})
	defer task.cancel()
	ctx, cancel := task.executionContext()
	defer cancel()
	deadline, limited := ctx.Deadline()
	if !limited || time.Until(deadline) < 11*time.Second || time.Until(deadline) > 12*time.Second {
		t.Fatal("explicit timeout was not retained", deadline)
	}
}

func TestAgentDiagnosticsNeverBecomeAssistantReplies(t *testing.T) {
	bin := t.TempDir()
	for _, kind := range []string{"codex", "claude"} {
		path := filepath.Join(bin, kind)
		script := `#!/bin/sh
printf '%s\n' '2026-11-09T08:22:34Z ERROR codex_core::util: OutputTextDelta without active item' >&2
case "$RELAY_EXECUTION_FIXTURE" in
 timeout) exec sleep 60 ;;
 failed) printf '%s\n' 'CLI authentication fixture failure' >&2; exit 7 ;;
esac
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"Codex answer"}}'
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"Claude answer"}]}}'
`
		if err := os.WriteFile(path, []byte(script), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	for _, kind := range []string{"codex", "claude"} {
		for _, mode := range []string{"success", "timeout", "failed"} {
			t.Run(kind+"/"+mode, func(t *testing.T) {
				t.Setenv("RELAY_EXECUTION_FIXTURE", mode)
				timeout := 0
				if mode == "timeout" {
					timeout = 1
				}
				task := newTask(M{"kind": "prompt", "agent": M{"kind": kind, "cwd": t.TempDir()}, "payload": M{"prompt": "fixture", "timeout": timeout}})
				defer task.cancel()
				go task.run(nil, false)
				waitTask(t, task)
				result := task.result
				if strings.Contains(str(result["output"]), "OutputTextDelta") || !strings.Contains(str(result["diagnostics"]), "OutputTextDelta") {
					t.Fatal("stderr contaminated the assistant response", result)
				}
				if mode == "success" {
					if result["status"] != "succeeded" || !strings.Contains(str(result["output"]), "answer") {
						t.Fatal("non-fatal diagnostics failed a completed turn", result)
					}
				} else if result["status"] != "failed" || str(result["error"]) == "" {
					t.Fatal("actual failure was hidden", result)
				}
				if mode == "timeout" && !strings.Contains(str(result["error"]), "1 秒上限") {
					t.Fatal("explicit deadline lost its explanation", result)
				}
				if mode == "failed" && !strings.Contains(str(result["diagnostics"]), "authentication fixture failure") {
					t.Fatal("failed process lost its diagnostic", result)
				}
			})
		}
	}
}
