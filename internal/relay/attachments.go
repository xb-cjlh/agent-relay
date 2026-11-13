package relay

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var attachmentID = regexp.MustCompile(`^att_[a-zA-Z0-9]+$`)

func (t *Task) prepareInputs() error {
	if err := t.prepareImages(); err != nil {
		return err
	}
	return t.prepareAttachments()
}

func (t *Task) prepareAttachments() error {
	files := arr(obj(t.job["payload"])["files"])
	if len(files) == 0 {
		return nil
	}
	if len(files)+len(t.images) > 8 || t.client == nil || t.attachmentDir == "" {
		return errors.New("附件任务缺少设备连接或存储目录，或附件数量超限")
	}
	dir, err := filepath.Abs(t.attachmentDir)
	if err != nil {
		return err
	}
	total := 0
	for _, img := range t.images {
		total += img.size
	}
	for _, value := range files {
		meta := obj(value)
		id, name := str(meta["id"]), str(meta["name"])
		if !attachmentID.MatchString(id) || name == "" || name == "." || name == ".." || strings.ContainsAny(name, "/\\\x00\r\n") || len(name) > 200 {
			return errors.New("附件 ID 或名称无效")
		}
		raw, _, err := t.client.requestContext(t.ctx, "GET", "/api/device/attachments/"+id+"/url", nil)
		if err != nil {
			return err
		}
		var grant M
		if err = json.Unmarshal(raw, &grant); err != nil {
			return errors.New("附件下载凭证无效")
		}
		data, err := signedFileRequest(t.ctx, "GET", grant, nil)
		if err != nil {
			return err
		}
		total += len(data)
		if len(data) != int(num(meta["size"])) || len(data) > MaxFile || total > 32*1024*1024 || hashBytes(data) != str(meta["sha256"]) {
			return errors.New("附件大小或 SHA-256 校验失败")
		}
		// Each immutable upload has its own directory; equal filenames cannot collide.
		for _, folder := range []string{dir, filepath.Join(dir, id)} {
			if err = os.MkdirAll(folder, 0700); err != nil {
				return err
			}
			info, statErr := os.Lstat(folder)
			if statErr != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
				return errors.New("附件存储目录无效")
			}
		}
		path := filepath.Join(dir, id, name)
		if err = saveImage(path, data); err != nil {
			return err
		}
		t.attachments = append(t.attachments, M{"id": id, "name": name, "path": path})
	}
	return nil
}

func (t *Task) agentPrompt() string {
	prompt := str(obj(t.job["payload"])["prompt"])
	if len(t.attachments) == 0 {
		return prompt
	}
	manifest, _ := json.Marshal(t.attachments)
	return prompt + "\n\n[Relay attachments]\n" + string(manifest) + "\n[/Relay attachments]"
}
