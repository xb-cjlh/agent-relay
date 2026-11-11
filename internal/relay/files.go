package relay

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"time"
)

// Signed OSS requests deliberately use a separate client and no Relay bearer token.
func signedFileRequest(ctx context.Context, method string, grant M, data []byte) ([]byte, error) {
	u, err := url.Parse(str(grant["url"]))
	if err != nil || u.User != nil || u.Host == "" || (u.Scheme != "https" && !(u.Scheme == "http" && (u.Hostname() == "127.0.0.1" || u.Hostname() == "localhost"))) {
		return nil, errors.New("文件存储地址无效")
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), bytes.NewReader(data))
	if err != nil {
		return nil, errors.New("无法创建文件传输请求")
	}
	for name, value := range obj(grant["headers"]) {
		req.Header.Set(name, str(value))
	}
	client := http.Client{Timeout: 60 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return nil, errors.New("文件存储连接失败，请重试")
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, errors.New("文件存储传输失败，请重试")
	}
	content, err := io.ReadAll(io.LimitReader(resp.Body, MaxFile+1))
	if err != nil || len(content) > MaxFile {
		return nil, errors.New("文件读取失败或超过 16 MiB 限制")
	}
	return content, nil
}
func (t *Task) uploadFile(name string, data []byte, sha string) (M, error) {
	sum := md5.Sum(data)
	body := M{"name": name, "size": len(data), "sha256": sha, "md5": base64.StdEncoding.EncodeToString(sum[:])}
	raw, _, err := t.client.requestContext(t.ctx, "POST", "/api/device/jobs/"+url.PathEscape(str(t.job["id"]))+"/file-upload", body)
	if err != nil {
		var api *APIError
		if errors.As(err, &api) && api.Status == 404 {
			return nil, nil
		} // Compatible with earlier servers.
		return nil, err
	}
	var grant M
	if err = json.Unmarshal(raw, &grant); err != nil {
		return nil, errors.New("文件上传凭证无效")
	}
	if !yes(grant["supported"]) {
		return nil, nil
	}
	if _, err = signedFileRequest(t.ctx, "PUT", grant, data); err != nil {
		return nil, err
	}
	return M{"status": "succeeded", "file": M{"upload_id": grant["upload_id"]}}, nil
}
