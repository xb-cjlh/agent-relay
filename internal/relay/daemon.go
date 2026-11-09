package relay

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func discover() []M {
	found := []M{}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "ps", "-u", strconv.Itoa(os.Getuid()), "-o", "pid=,comm=").Output()
	if err != nil {
		return found
	}
	for _, line := range strings.Split(string(out), "\n") {
		parts := strings.Fields(line)
		if len(parts) < 2 {
			continue
		}
		pid, e := strconv.Atoi(parts[0])
		if e != nil {
			continue
		}
		executable := filepath.Base(strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), parts[0])))
		name := strings.ToLower(executable)
		kind := ""
		if name == "codex" || name == "codex-cli" {
			kind = "codex"
		}
		if name == "claude" || name == "claude-code" {
			kind = "claude"
		}
		if kind != "" {
			found = append(found, M{"pid": pid, "kind": kind, "executable": executable, "mode": "observed"})
		}
		if len(found) >= 500 {
			break
		}
	}
	return found
}
func daemonCLI(c *Client, args []string) error {
	if len(args) == 0 {
		return errors.New("需要 daemon run、install、status、restart 或 uninstall")
	}
	action := args[0]
	o, err := parse(args[1:], "name root state-dir interval", "allow-agent-writes virtual-device")
	if err != nil {
		return err
	}
	name := o.get("name", "")
	virtual := o.get("virtual-device", "") == "true"
	if virtual && name == "" {
		return errors.New("模拟设备需要 --name")
	}
	virtualName := ""
	if virtual {
		virtualName = name
	}
	machine, err := machineID(virtualName)
	if err != nil {
		return err
	}
	if action == "uninstall" {
		return uninstallService(c.Server, machine, name)
	}
	if action == "status" {
		return daemonStatus(c.Server, machine)
	}
	if action == "restart" {
		return restartService(c.Server, machine)
	}
	roots := o.vals["root"]
	// Accept old service arguments for upgrade compatibility. They never limit access.
	if !virtual {
		roots = nil
	}
	for i, p := range roots {
		p, err = filepath.EvalSymlinks(expand(p))
		if err != nil {
			return err
		}
		p, err = filepath.Abs(p)
		if err != nil {
			return err
		}
		info, e := os.Stat(p)
		if e != nil {
			return e
		}
		if !info.IsDir() {
			return errors.New("共享路径必须为目录")
		}
		roots[i] = p
	}
	writable := o.get("allow-agent-writes", "") == "true"
	if action == "install" {
		if _, err = c.Request("GET", "/api/state", nil); err != nil {
			return err
		}
		if err = writePrivate(filepath.Join(configDir(), "config.json"), M{"server": c.Server, "token": c.Token}); err != nil {
			return err
		}
		return installService(c.Server, name, roots, writable, o.get("state-dir", ""), machine, virtual)
	}
	if action != "run" {
		return errors.New("未知 daemon 子命令")
	}
	interval := 2 * time.Second
	if s := o.get("interval", ""); s != "" {
		n, e := strconv.ParseFloat(s, 64)
		if e != nil || n < 0.05 || n > 60 {
			return errors.New("interval 必须介于 0.05 和 60 秒")
		}
		interval = time.Duration(n * float64(time.Second))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return runDaemon(ctx, c, name, roots, o.get("state-dir", ""), writable, interval, machine, virtual)
}
func runDaemon(ctx context.Context, admin *Client, name string, roots []string, location string, writable bool, interval time.Duration, machine string, virtual bool) error {
	canonical := canonicalDeviceDir(admin.Server, machine)
	lockPath, err := machineLockPath(admin.Server, machine)
	if err != nil {
		return err
	}
	machineLock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer machineLock.Close()
	if err = syscall.Flock(int(machineLock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("本机已连接此服务端；修改目录后重启已有进程即可，无需重复注册")
	}
	defer syscall.Flock(int(machineLock.Fd()), syscall.LOCK_UN)
	if location == "" {
		location = canonical
	}
	location = expand(location)
	if err = os.MkdirAll(location, 0700); err != nil {
		return err
	}
	// Keep the legacy per-directory lock while adopting an existing identity.
	lock, err := os.OpenFile(filepath.Join(location, "daemon.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("此设备后台进程已在运行")
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	statePath := filepath.Join(location, "identity.json")
	state, err := readJSON(statePath)
	if err != nil {
		return err
	}
	canonicalPath := filepath.Join(canonical, "identity.json")
	saved, err := readJSON(canonicalPath)
	if err != nil {
		return err
	}
	if len(saved) > 0 {
		if len(state) > 0 && state["id"] != saved["id"] {
			return errors.New("本机已有整机身份；旧目录身份需先合并，请使用默认状态目录启动")
		}
		state = saved
	}
	if virtual && roots == nil {
		for _, value := range arr(state["roots"]) {
			roots = append(roots, str(value))
		}
	}
	if roots == nil {
		roots = []string{}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	base := home
	if virtual && len(roots) > 0 {
		base = roots[0]
	}
	_, codexErr := exec.LookPath("codex")
	_, claudeErr := exec.LookPath("claude")
	scope := "user"
	if virtual {
		scope = "roots"
	}
	caps := M{"codex": codexErr == nil, "claude": claudeErr == nil, "writable": writable, "discovery_scope": scope, "access_scope": "user", "home": home, "relay_version": Version, "image_input": true, "codex_permissions": true, "claude_permissions": true}
	discoveryRoots := roots
	// Legacy roots is a compatibility field for old servers, not a user setting.
	if !virtual {
		roots = []string{"/"}
	}
	if len(state) > 0 {
		if state["server"] != admin.Server {
			return errors.New("设备状态目录属于另一设备或服务端")
		}
		if prior := str(state["machine_id"]); prior != "" && prior != machine {
			return errors.New("设备状态目录属于另一机器或模拟设备，请使用默认状态目录")
		}
		if str(state["id"]) == "" || str(state["token"]) == "" {
			return errors.New("设备身份文件无效")
		}
	} else {
		if name == "" {
			name = defaultDeviceName()
		}
		state, err = admin.Request("POST", "/api/register", M{"name": name, "platform": runtime.GOOS + " / " + runtime.GOARCH, "roots": roots, "capabilities": caps, "machine_id": machine})
		if err != nil {
			return err
		}
		state["server"] = admin.Server
		state["name"] = name
	}
	state["machine_id"] = machine
	state["roots"] = roots
	if err = writePrivate(canonicalPath, state); err != nil {
		return err
	}
	if statePath != canonicalPath {
		if err = writePrivate(statePath, state); err != nil {
			return err
		}
	}
	name = str(state["name"])
	client, _ := NewClient(admin.Server, str(state["token"]))
	fmt.Printf("设备 %s 已连接，ID: %s，会话发现: %s，访问范围: 当前系统用户可访问的所有目录\n", name, str(state["id"]), scope)
	tasks := map[string]*Task{}
	mirror := newMirror(discoveryRoots)
	mirror.scoped = virtual
	lastContact := time.Now()
	defer func() {
		for _, t := range tasks {
			t.cancel()
		}
		for id, t := range tasks {
			select {
			case <-t.done:
				client.Request("POST", "/api/device/jobs/"+id+"/complete", t.result)
			case <-time.After(6 * time.Second):
			}
		}
	}()
	for ctx.Err() == nil {
		active := []M{}
		for _, t := range tasks {
			active = append(active, t.progress())
		}
		capacity := 4 - len(tasks)
		if capacity < 0 {
			capacity = 0
		}
		reply, e := client.Request("POST", "/api/device/poll", M{"processes": discover(), "active": active, "capacity": capacity, "roots": roots, "capabilities": caps, "sessions": mirror.scan(), "machine_id": machine})
		if e == nil {
			mirror.acknowledge()
			lastContact = time.Now()
			for _, id := range arr(reply["cancel"]) {
				if t := tasks[str(id)]; t != nil {
					t.cancel()
				}
			}
			for _, v := range arr(reply["jobs"]) {
				job := obj(v)
				id := str(job["id"])
				if tasks[id] != nil {
					continue
				}
				t := newTask(job)
				t.client = client
				t.imageDir = filepath.Join(canonical, "images")
				tasks[id] = t
				go t.run([]string{base}, writable)
			}
			for id, t := range tasks {
				select {
				case <-t.done:
					if _, e = client.Request("POST", "/api/device/jobs/"+id+"/complete", t.result); e == nil {
						if t.job["kind"] == "prompt" {
							sid := str(t.result["session_id"])
							if sid == "" {
								sid = str(obj(t.job["agent"])["session_id"])
							}
							mirror.release(str(obj(t.job["agent"])["kind"]), sid)
						}
						delete(tasks, id)
					}
				default:
				}
				if e != nil {
					break
				}
			}
		}
		if e != nil {
			fmt.Fprintln(os.Stderr, "连接暂不可用: "+e.Error())
			if time.Since(lastContact) > 30*time.Second {
				for _, t := range tasks {
					t.cancel()
				}
			}
			var api *APIError
			if errors.As(e, &api) && (api.Status == 401 || api.Status == 403) {
				return e
			}
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
	return nil
}
