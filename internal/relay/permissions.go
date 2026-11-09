package relay

import (
	"context"
	"encoding/json"
	"errors"
	"time"
)

func (t *Task) approvalHTTP(ctx context.Context, method, path string, body interface{}) (M, error) {
	bytes, _, err := t.client.requestContext(ctx, method, path, body)
	if err != nil {
		return nil, err
	}
	var reply M
	err = json.Unmarshal(bytes, &reply)
	return reply, err
}

func (t *Task) waitApproval(ctx context.Context, message M, method string, details M) (string, bool) {
	encoded, _ := json.Marshal(message)
	id := "apr_" + hash(str(t.job["id"]) + "\x00" + string(encoded))[:48]
	base := "/api/device/jobs/" + str(t.job["id"]) + "/approvals"
	decision := "decline"
	if t.client != nil {
		defer func() {
			cleanup, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			t.approvalHTTP(cleanup, "POST", base+"/"+id+"/expire", M{})
		}()
		registered := false
		for ctx.Err() == nil {
			var reply M
			var err error
			if !registered {
				reply, err = t.approvalHTTP(ctx, "POST", base, M{"id": id, "method": method, "details": details})
				registered = err == nil
			} else {
				reply, err = t.approvalHTTP(ctx, "GET", base+"/"+id, nil)
			}
			if err == nil {
				value := str(reply["decision"])
				if value == "accept" || value == "decline" {
					decision = value
					break
				}
				if value == "expired" {
					return "", false
				}
			} else {
				var api *APIError
				if errors.As(err, &api) && api.Status < 500 {
					break
				}
			}
			select {
			case <-ctx.Done():
				return "", false
			case <-time.After(500 * time.Millisecond):
			}
		}
	}
	return decision, ctx.Err() == nil
}
