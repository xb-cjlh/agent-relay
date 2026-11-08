package relay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type M = map[string]interface{}

const MaxFile = 16 * 1024 * 1024
const MaxOutput = 64000

var terminal = map[string]bool{"succeeded": true, "failed": true, "cancelled": true}

type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string { return e.Message }
func str(v interface{}) string    { s, _ := v.(string); return s }
func obj(v interface{}) M {
	m, _ := v.(map[string]interface{})
	if m == nil {
		return M{}
	}
	return m
}
func arr(v interface{}) []interface{} { a, _ := v.([]interface{}); return a }
func num(v interface{}) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	case int64:
		return float64(n)
	}
	return 0
}
func yes(v interface{}) bool    { b, _ := v.(bool); return b || num(v) != 0 }
func hash(s string) string      { return hashBytes([]byte(s)) }
func hashBytes(b []byte) string { v := sha256.Sum256(b); return hex.EncodeToString(v[:]) }
func tail(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[len(r)-n:])
	}
	return s
}
func head(s string, n int) string {
	r := []rune(s)
	if len(r) > n {
		return string(r[:n])
	}
	return s
}
func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }
func expand(s string) string {
	if s == "~" || strings.HasPrefix(s, "~/") {
		h, _ := os.UserHomeDir()
		return filepath.Join(h, strings.TrimPrefix(s, "~"))
	}
	return s
}
func configDir() string {
	if s := os.Getenv("AM_CONFIG_DIR"); s != "" {
		return expand(s)
	}
	return expand("~/.config/agent-manager")
}
func readJSON(path string) (M, error) {
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return M{}, nil
	}
	if err != nil {
		return nil, err
	}
	var value M
	err = json.Unmarshal(b, &value)
	if value == nil && err == nil {
		err = errors.New("配置文件必须是 JSON 对象")
	}
	return value, err
}
func writePrivate(path string, v interface{}) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".relay-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(b); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}
func virtualDiscoveryPath(value string, roots []string) (string, error) {
	if len(roots) == 0 {
		return "", errors.New("至少需要一个共享目录")
	}
	p := expand(value)
	if !filepath.IsAbs(p) {
		p = filepath.Join(roots[0], p)
	}
	p, err := filepath.EvalSymlinks(p)
	if err != nil {
		return "", err
	}
	p, err = filepath.Abs(p)
	if err != nil {
		return "", err
	}
	for _, root := range roots {
		r, e := filepath.EvalSymlinks(expand(root))
		if e != nil {
			return "", e
		}
		r, e = filepath.Abs(r)
		if e != nil {
			return "", e
		}
		rel, e := filepath.Rel(r, p)
		if e == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return p, nil
		}
	}
	return "", errors.New("路径超出此设备的共享目录")
}

// Remote access follows the daemon's OS account permissions, including symlinks.
// The base only resolves relative file names; it is not an access boundary.
func accessiblePath(value string, base string) (string, error) {
	p := expand(value)
	if !filepath.IsAbs(p) {
		if base == "" {
			var err error
			base, err = os.UserHomeDir()
			if err != nil {
				return "", err
			}
		}
		p = filepath.Join(base, p)
	}
	p, err := filepath.Abs(p)
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(p)
}

type Client struct {
	Server, Token string
	HTTP          *http.Client
}

func NewClient(server, token string) (*Client, error) {
	u, err := url.Parse(server)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("服务端地址必须是 http(s) URL，且不能包含账号、密码、查询或片段")
	}
	return &Client{strings.TrimRight(server, "/"), token, &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(r *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func (c *Client) request(method, path string, body interface{}) ([]byte, http.Header, error) {
	return c.requestContext(context.Background(), method, path, body)
}
func (c *Client) requestContext(ctx context.Context, method, path string, body interface{}) ([]byte, http.Header, error) {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, nil, err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Server+path, reader)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, nil, &APIError{503, "无法连接服务端: " + err.Error()}
	}
	defer resp.Body.Close()
	limit := int64(23 * 1024 * 1024)
	if strings.HasPrefix(path, "/api/files/") {
		limit = MaxFile
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, nil, err
	}
	if int64(len(b)) > limit {
		return nil, nil, errors.New("响应超过大小限制")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var v M
		json.Unmarshal(b, &v)
		msg := str(v["error"])
		if msg == "" {
			msg = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
		return nil, nil, &APIError{resp.StatusCode, msg}
	}
	return b, resp.Header, nil
}
func (c *Client) Request(method, path string, body interface{}) (M, error) {
	b, _, err := c.request(method, path, body)
	if err != nil {
		return nil, err
	}
	var v M
	err = json.Unmarshal(b, &v)
	return v, err
}
func (c *Client) Wait(id string, timeout time.Duration) (M, error) {
	end := time.Now().Add(timeout)
	for time.Now().Before(end) {
		j, err := c.Request("GET", "/api/jobs/"+url.PathEscape(id), nil)
		if err != nil {
			return nil, err
		}
		if terminal[str(j["status"])] {
			return j, nil
		}
		time.Sleep(400 * time.Millisecond)
	}
	return nil, errors.New("等待超时，任务仍可通过 relayctl jobs show " + id + " 查看")
}
func Download(c *Client, id, output string) (M, error) {
	b, h, err := c.request("GET", "/api/files/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	sha := hashBytes(b)
	if sha != h.Get("X-SHA256") {
		return nil, errors.New("下载文件的 SHA-256 校验失败")
	}
	target, err := filepath.Abs(expand(output))
	if err != nil {
		return nil, err
	}
	if err = os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return nil, err
	}
	_, err = f.Write(b)
	closeErr := f.Close()
	if err != nil {
		return nil, err
	}
	if closeErr != nil {
		return nil, closeErr
	}
	return M{"path": target, "size": len(b), "sha256": sha, "artifact_id": id}, nil
}
