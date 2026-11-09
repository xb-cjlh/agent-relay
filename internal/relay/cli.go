package relay

import (
	"bufio"
	assets "dev.relay/agent-manager/relayctl"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const usage = `Relay · 跨设备 Agent 管理（Go）
用法: relayctl [--server URL] [--token TOKEN] <命令>
  configure --server URL [--token-file PATH]    保存连接配置（默认隐藏输入）
  devices                                       列出设备
  devices merge --into ID --from ID [--dry-run] 合并旧设备并保留历史
  daemon run|install [--name NAME]              整机守护进程，按当前用户权限访问
  daemon uninstall                              卸载本机后台服务
  daemon status|restart                         查看状态或重启已安装的后台服务
  serve [--host HOST] [--port 8765] [--data data] 启动 Java 服务端
  agents list|create                            列出或创建会话
  send AGENT PROMPT [--wait] [--timeout SECONDS]  发送指令（默认无执行时限，0 表示不限）
  jobs list|show ID|wait ID|cancel ID             查询或取消任务
  files list|pull|download                       远程文件传输
  skill install [--target both|codex|claude] [--force] 安装或更新 skill
  version                                       显示版本
`

type options struct {
	vals map[string][]string
	pos  []string
}

func parse(args []string, allowed string, booleans string) (options, error) {
	o := options{vals: map[string][]string{}}
	known := strings.Fields(allowed)
	bools := strings.Fields(booleans)
	contains := func(a []string, s string) bool {
		for _, v := range a {
			if v == s {
				return true
			}
		}
		return false
	}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			o.pos = append(o.pos, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(a, "--") {
			o.pos = append(o.pos, a)
			continue
		}
		key := strings.TrimPrefix(a, "--")
		value := ""
		eq := strings.Index(key, "=")
		if eq >= 0 {
			value = key[eq+1:]
			key = key[:eq]
		}
		if contains(bools, key) {
			if eq >= 0 {
				return o, fmt.Errorf("--%s 不接受参数", key)
			}
			value = "true"
		} else if contains(known, key) {
			if eq < 0 {
				i++
				if i >= len(args) {
					return o, fmt.Errorf("--%s 需要参数", key)
				}
				value = args[i]
			}
		} else {
			return o, fmt.Errorf("未知选项: --%s", key)
		}
		o.vals[key] = append(o.vals[key], value)
	}
	return o, nil
}
func (o options) get(key, def string) string {
	a := o.vals[key]
	if len(a) > 0 {
		return a[len(a)-1]
	}
	return def
}
func (o options) required(key string) (string, error) {
	s := o.get(key, "")
	if s == "" {
		return "", fmt.Errorf("需要 --%s", key)
	}
	return s, nil
}
func (o options) integer(key string, def, min, max int) (int, error) {
	s := o.get(key, strconv.Itoa(def))
	n, e := strconv.Atoi(s)
	if e != nil || n < min || n > max {
		return 0, fmt.Errorf("--%s 必须介于 %d 和 %d", key, min, max)
	}
	return n, nil
}
func emit(value interface{}) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	return enc.Encode(value)
}
func readSecret() (string, error) {
	fmt.Fprint(os.Stderr, "Relay SK: ")
	cmd := exec.Command("stty", "-echo")
	cmd.Stdin = os.Stdin
	hidden := cmd.Run() == nil
	restore := func() {
		if hidden {
			cmd := exec.Command("stty", "echo")
			cmd.Stdin = os.Stdin
			cmd.Run()
			fmt.Fprintln(os.Stderr)
		}
	}
	defer restore()
	interrupts := make(chan os.Signal, 1)
	signal.Notify(interrupts, os.Interrupt)
	defer signal.Stop(interrupts)
	type answer struct {
		s string
		e error
	}
	done := make(chan answer, 1)
	go func() { s, e := bufio.NewReader(os.Stdin).ReadString('\n'); done <- answer{strings.TrimSpace(s), e} }()
	select {
	case <-interrupts:
		return "", errors.New("输入已取消")
	case a := <-done:
		if a.e != nil && a.s == "" {
			return "", a.e
		}
		return a.s, nil
	}
}
func Run(args []string) error {
	if len(args) == 0 || args[0] == "--help" || args[0] == "-h" {
		fmt.Print(usage)
		return nil
	}
	// Global credentials precede the command; configure has its own --server/--token.
	global := []string{}
	for len(args) > 0 && strings.HasPrefix(args[0], "--") {
		a := args[0]
		global = append(global, a)
		args = args[1:]
		if !strings.Contains(a, "=") {
			if len(args) == 0 {
				return errors.New("全局选项缺少参数")
			}
			global = append(global, args[0])
			args = args[1:]
		}
	}
	g, err := parse(global, "server token", "")
	if err != nil {
		return err
	}
	if len(args) == 0 {
		return errors.New("缺少命令")
	}
	command := args[0]
	args = args[1:]
	for _, a := range args {
		if a == "--help" || a == "-h" {
			fmt.Print(usage)
			return nil
		}
	}
	config, err := readJSON(filepath.Join(configDir(), "config.json"))
	if err != nil {
		return err
	}
	server := g.get("server", os.Getenv("AM_SERVER"))
	if server == "" {
		server = str(config["server"])
	}
	if server == "" {
		server = "http://127.0.0.1:8765"
	}
	token := g.get("token", os.Getenv("AM_TOKEN"))
	if token == "" {
		token = str(config["token"])
	}
	if command == "version" {
		fmt.Println("relayctl " + Version + " (Go)")
		return nil
	}
	if command == "serve" {
		o, e := parse(args, "host port data", "")
		if e != nil {
			return e
		}
		port, e := o.integer("port", 8765, 0, 65535)
		if e != nil {
			return e
		}
		return Serve(o.get("host", "127.0.0.1"), port, o.get("data", "data"))
	}
	if command == "configure" {
		o, e := parse(args, "server token token-file", "")
		if e != nil {
			return e
		}
		s, e := o.required("server")
		if e != nil {
			return e
		}
		secret := o.get("token", os.Getenv("AM_TOKEN"))
		if path := o.get("token-file", ""); path != "" {
			f, err := os.Open(expand(path))
			if err != nil {
				return err
			}
			b, err := io.ReadAll(io.LimitReader(f, 4097))
			f.Close()
			if err != nil {
				return err
			}
			if len(b) > 4096 {
				return errors.New("令牌文件过大")
			}
			secret = strings.TrimSpace(string(b))
			if secret == "" {
				return errors.New("令牌文件为空")
			}
		}
		if secret == "" && strings.TrimRight(s, "/") == str(config["server"]) {
			secret = str(config["token"])
		}
		if secret == "" {
			secret, e = readSecret()
			if e != nil {
				return e
			}
		}
		c, e := NewClient(s, secret)
		if e != nil {
			return e
		}
		if _, e = c.Request("GET", "/api/state", nil); e != nil {
			return e
		}
		if e = writePrivate(filepath.Join(configDir(), "config.json"), M{"server": c.Server, "token": secret}); e != nil {
			return e
		}
		return emit(M{"configured": true, "server": c.Server})
	}
	if command == "skill" {
		return installSkill(args)
	}
	c, err := NewClient(server, token)
	if err != nil {
		return err
	}
	if command == "daemon" {
		return daemonCLI(c, args)
	}
	if command == "devices" {
		if len(args) > 0 && args[0] == "merge" {
			o, e := parse(args[1:], "into from", "dry-run")
			if e != nil {
				return e
			}
			if len(o.pos) > 0 || o.get("into", "") == "" || len(o.vals["from"]) == 0 {
				return errors.New("用法: relayctl devices merge --into TARGET --from SOURCE [--from SOURCE] [--dry-run]")
			}
			result, e := c.Request("POST", "/api/devices/merge", M{"target": o.get("into", ""), "sources": o.vals["from"], "dry_run": o.get("dry-run", "") == "true"})
			if e != nil {
				return e
			}
			return emit(result)
		}
		if len(args) > 0 {
			return errors.New("devices 不接受参数")
		}
		s, e := c.Request("GET", "/api/state", nil)
		if e != nil {
			return e
		}
		return emit(s["devices"])
	}
	if command == "send" {
		o, e := parse(args, "timeout", "wait")
		if e != nil {
			return e
		}
		if len(o.pos) != 2 {
			return errors.New("用法: relayctl send AGENT PROMPT [--wait]")
		}
		timeout, e := o.integer("timeout", 0, 0, 1800)
		if e != nil {
			return e
		}
		prompt := o.pos[1]
		if prompt == "-" {
			b, e := io.ReadAll(io.LimitReader(os.Stdin, 128001))
			if e != nil {
				return e
			}
			prompt = string(b)
		}
		if len([]rune(prompt)) > 32000 {
			return errors.New("指令超过 32000 字符")
		}
		j, e := c.Request("POST", "/api/jobs", M{"kind": "prompt", "agent": o.pos[0], "prompt": prompt, "timeout": timeout})
		if e != nil {
			return e
		}
		if o.get("wait", "") == "true" {
			wait := time.Duration(0)
			if timeout > 0 {
				wait = time.Duration(timeout+60) * time.Second
			}
			j, e = c.Wait(str(j["id"]), wait)
			if e != nil {
				return e
			}
		}
		if e = emit(j); e != nil {
			return e
		}
		if terminal[str(j["status"])] && j["status"] != "succeeded" {
			return errors.New("任务未成功完成")
		}
		return nil
	}
	if len(args) == 0 {
		return errors.New("缺少子命令")
	}
	action := args[0]
	args = args[1:]
	switch command {
	case "agents":
		if action == "list" {
			s, e := c.Request("GET", "/api/state", nil)
			if e != nil {
				return e
			}
			return emit(s["agents"])
		}
		if action == "create" {
			o, e := parse(args, "device kind name cwd", "")
			if e != nil {
				return e
			}
			b := M{}
			for _, k := range []string{"device", "kind", "name", "cwd"} {
				v, e := o.required(k)
				if e != nil {
					return e
				}
				b[k] = v
			}
			v, e := c.Request("POST", "/api/agents", b)
			if e != nil {
				return e
			}
			return emit(v)
		}
	case "jobs":
		if action == "list" {
			s, e := c.Request("GET", "/api/state", nil)
			if e != nil {
				return e
			}
			return emit(s["jobs"])
		}
		o, e := parse(args, "timeout", "")
		if e != nil {
			return e
		}
		if len(o.pos) != 1 {
			return errors.New("需要任务 ID")
		}
		id := o.pos[0]
		var v M
		switch action {
		case "show":
			v, e = c.Request("GET", "/api/jobs/"+id, nil)
		case "cancel":
			v, e = c.Request("POST", "/api/jobs/"+id+"/cancel", M{})
		case "wait":
			var n int
			n, e = o.integer("timeout", 330, 1, 86400)
			if e == nil {
				v, e = c.Wait(id, time.Duration(n)*time.Second)
			}
		default:
			return errors.New("未知 jobs 子命令")
		}
		if e != nil {
			return e
		}
		if e = emit(v); e != nil {
			return e
		}
		if action == "wait" && v["status"] != "succeeded" {
			return errors.New("任务未成功完成")
		}
		return nil
	case "files":
		if action == "list" {
			s, e := c.Request("GET", "/api/state", nil)
			if e != nil {
				return e
			}
			return emit(s["artifacts"])
		}
		o, e := parse(args, "device path output timeout", "")
		if e != nil {
			return e
		}
		output, e := o.required("output")
		if e != nil {
			return e
		}
		var id string
		if action == "download" {
			if len(o.pos) != 1 {
				return errors.New("需要文件 ID")
			}
			id = o.pos[0]
		} else if action == "pull" {
			if _, e = os.Lstat(expand(output)); e == nil {
				return errors.New("本地目标已存在: " + output)
			}
			device, e := o.required("device")
			if e != nil {
				return e
			}
			path, e := o.required("path")
			if e != nil {
				return e
			}
			n, e := o.integer("timeout", 90, 1, 86400)
			if e != nil {
				return e
			}
			j, e := c.Request("POST", "/api/jobs", M{"kind": "file_read", "device": device, "path": path})
			if e != nil {
				return e
			}
			j, e = c.Wait(str(j["id"]), time.Duration(n)*time.Second)
			if e != nil {
				return e
			}
			if j["status"] != "succeeded" {
				return fmt.Errorf("文件传输失败: %v", j["error"])
			}
			id = str(j["artifact_id"])
		} else {
			return errors.New("未知 files 子命令")
		}
		v, e := Download(c, id, output)
		if e != nil {
			return e
		}
		return emit(v)
	}
	return errors.New("未知命令: " + command + " " + action)
}
func installSkill(args []string) error {
	if len(args) == 0 || args[0] != "install" {
		return errors.New("用法: relayctl skill install [--target both|codex|claude]")
	}
	o, e := parse(args[1:], "target path", "force")
	if e != nil {
		return e
	}
	var bases []string
	if p := o.get("path", ""); p != "" {
		bases = []string{expand(p)}
	} else {
		target := o.get("target", "both")
		if target != "both" && target != "claude" && target != "codex" {
			return errors.New("target 必须为 both、codex 或 claude")
		}
		if target == "both" || target == "codex" {
			base := os.Getenv("CODEX_HOME")
			if base == "" {
				base = expand("~/.codex")
			}
			bases = append(bases, filepath.Join(expand(base), "skills"))
		}
		if target == "both" || target == "claude" {
			base := os.Getenv("CLAUDE_CONFIG_DIR")
			if base == "" {
				base = expand("~/.claude")
			}
			bases = append(bases, filepath.Join(expand(base), "skills"))
		}
	}
	installed := []string{}
	backups := []string{}
	for _, base := range bases {
		dest := filepath.Join(base, "agent-bridge/SKILL.md")
		if b, e := os.ReadFile(dest); e == nil && string(b) != string(assets.AgentBridge) {
			if o.get("force", "") != "true" {
				return errors.New("目标 skill 已存在且内容不同；使用 --force 备份后更新: " + dest)
			}
			backup, err := os.CreateTemp(filepath.Dir(dest), "SKILL.md.backup-*")
			if err != nil {
				return err
			}
			_, err = backup.Write(b)
			closed := backup.Close()
			if err != nil {
				return err
			}
			if closed != nil {
				return closed
			}
			backups = append(backups, backup.Name())
		} else if e != nil && !os.IsNotExist(e) {
			return e
		}
		if e = os.MkdirAll(filepath.Dir(dest), 0755); e != nil {
			return e
		}
		if e = os.WriteFile(dest, assets.AgentBridge, 0644); e != nil {
			return e
		}
		installed = append(installed, dest)
	}
	return emit(M{"installed": installed, "backups": backups})
}
