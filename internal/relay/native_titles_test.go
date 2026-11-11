package relay

import (
	"context"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestCodexNativeTitlePreservesRenamesAndConversation(t *testing.T) {
	for _, test := range []struct {
		name, initial, renamed, output, want string
	}{
		{"generated", "", "", `{"title":"修复登录页面"}`, "修复登录页面"},
		{"already-named", "custom title", "", "", ""},
		{"renamed-during-generation", "", "user rename", `{"title":"generated"}`, ""},
		{"invalid-output", "", "", "not a title object", ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			var saved string
			reads, generations := 0, 0
			done := make(chan struct{})
			codexFixture(t, func(conn *websocket.Conn) {
				defer close(done)
				for {
					var req M
					if conn.ReadJSON(&req) != nil {
						return
					}
					params := obj(req["params"])
					switch req["method"] {
					case "initialize":
						rpcReply(conn, req, M{})
					case "thread/read":
						reads++
						title := test.initial
						if reads > 1 {
							title = test.renamed
						}
						rpcReply(conn, req, M{"thread": M{"name": title}})
					case "thread/start":
						if params["ephemeral"] != true || params["sandbox"] != "read-only" {
							t.Error("title helper is not ephemeral and read-only", params)
						}
						rpcReply(conn, req, M{"thread": M{"id": "helper"}})
					case "turn/start":
						generations++
						if params["threadId"] != "helper" {
							t.Error("title prompt sent to original conversation")
						}
						rpcReply(conn, req, M{"turn": M{"id": "helper-turn"}})
						notify(conn, "item/completed", M{"threadId": "helper", "turnId": "helper-turn", "item": M{"type": "agentMessage", "text": test.output}})
						notify(conn, "turn/completed", M{"threadId": "helper", "turn": M{"id": "helper-turn", "status": "completed"}})
					case "thread/name/set":
						if params["threadId"] != "original" {
							t.Error("renamed the helper instead of the original session")
						}
						saved = str(params["name"])
						rpcReply(conn, req, M{})
					}
				}
			})
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			rpc, err := dialCodexPath(ctx, codexSocket())
			if err != nil {
				t.Fatal(err)
			}
			task := newTask(M{"payload": M{"prompt": "fix login"}})
			defer task.cancel()
			task.nameCodexSession(ctx, rpc, "original", t.TempDir())
			rpc.close()
			<-done
			if saved != test.want || (test.initial != "" && generations != 0) {
				t.Fatal("title overwrote an existing name or generated unnecessarily", saved, generations)
			}
			if task.progress()["output"] != "" {
				t.Fatal("title output leaked into the conversation")
			}
		})
	}
}
