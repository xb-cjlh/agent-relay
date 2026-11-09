package relay

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"os"
	"path/filepath"
	"regexp"
)

type taskImage struct{ path, mime, data string }

var imageID = regexp.MustCompile(`^img_[a-zA-Z0-9]+$`)

func (t *Task) prepareImages() error {
	images := arr(obj(t.job["payload"])["images"])
	if len(images) == 0 {
		return nil
	}
	if len(images) > 4 || t.client == nil || t.imageDir == "" {
		return errors.New("图片任务缺少有效的设备连接或存储目录，或图片数量超限")
	}
	// Native conversation history retains these local paths across turns and
	// daemon restarts. Store originals with the device identity, never in /tmp.
	dir, err := filepath.Abs(t.imageDir)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	total := 0
	for _, value := range images {
		meta := obj(value)
		id := str(meta["id"])
		if !imageID.MatchString(id) {
			return errors.New("图片 ID 无效")
		}
		data, headers, err := t.client.requestContext(t.ctx, "GET", "/api/device/images/"+id, nil)
		total += len(data)
		if err != nil {
			return err
		}
		if len(data) == 0 || len(data) > 5*1024*1024 || total > 12*1024*1024 || hashBytes(data) != str(meta["sha256"]) || headers.Get("X-SHA256") != str(meta["sha256"]) {
			return errors.New("图片大小或校验失败")
		}
		config, format, err := image.DecodeConfig(bytes.NewReader(data))
		mime := "image/" + format
		if err != nil || (format != "png" && format != "jpeg") || mime != str(meta["mime"]) || config.Width <= 0 || config.Height <= 0 || int64(config.Width)*int64(config.Height) > 36_000_000 {
			return errors.New("图片格式或分辨率无效")
		}
		path := filepath.Join(dir, fmt.Sprintf("%s.%s", id, format))
		if err = saveImage(path, data); err != nil {
			return err
		}
		t.images = append(t.images, taskImage{path: path, mime: mime, data: base64.StdEncoding.EncodeToString(data)})
	}
	return nil
}

func saveImage(path string, data []byte) error {
	// An interrupted or repeated delivery must never leave a partial original
	// at a path already referenced by the agent's conversation history.
	f, err := os.CreateTemp(filepath.Dir(path), ".relay-image-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

func (t *Task) codexInput() []M {
	input := []M{{"type": "text", "text": str(obj(t.job["payload"])["prompt"])}}
	for _, img := range t.images {
		input = append(input, M{"type": "localImage", "path": img.path})
	}
	return input
}
func (t *Task) agentInput(kind string) string {
	prompt := str(obj(t.job["payload"])["prompt"])
	if kind != "claude" || len(t.images) == 0 {
		return prompt
	}
	b, _ := json.Marshal(t.claudeInput())
	return string(b) + "\n"
}
func (t *Task) claudeInput() M {
	prompt := str(obj(t.job["payload"])["prompt"])
	content := []M{}
	for _, img := range t.images {
		content = append(content, M{"type": "image", "source": M{"type": "base64", "media_type": img.mime, "data": img.data}})
	}
	content = append(content, M{"type": "text", "text": prompt})
	return M{"type": "user", "message": M{"role": "user", "content": content}}
}
func (t *Task) imageArgs(args []string, kind string) []string {
	if len(t.images) == 0 {
		return args
	}
	if kind == "codex" {
		// Insert before the session and stdin positional arguments.
		index := len(args) - 1
		if str(obj(t.job["agent"])["session_id"]) != "" {
			index--
		}
		result := append([]string{}, args[:index]...)
		for _, img := range t.images {
			result = append(result, "--image", img.path)
		}
		// Codex accepts multiple image values; terminate options so stdin's "-"
		// and the resumed session ID cannot be consumed as image filenames.
		result = append(result, "--")
		return append(result, args[index:]...)
	}
	return append(args, "--input-format", "stream-json")
}
