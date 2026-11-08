package relay

import (
	"bytes"
	"encoding/xml"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

func xmlText(value string) string {
	var b bytes.Buffer
	xml.EscapeText(&b, []byte(value))
	return b.String()
}
func serviceID(server, name string) string {
	return "dev.relay.agent-" + hash(server + "/" + name)[:12]
}
func servicePath(id string) (string, error) {
	switch runtime.GOOS {
	case "darwin":
		return expand("~/Library/LaunchAgents/" + id + ".plist"), nil
	case "linux":
		return expand("~/.config/systemd/user/" + id + ".service"), nil
	}
	return "", errors.New("后台服务安装支持 macOS / Linux")
}
func runServiceCommand(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
func installService(server, name string, roots []string, writable bool, stateDir, machine string, virtual bool) error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	executable, err = filepath.EvalSymlinks(executable)
	if err != nil {
		return err
	}
	args := []string{executable, "--server", server, "daemon", "run"}
	if name != "" {
		args = append(args, "--name", name)
	}
	if stateDir != "" {
		args = append(args, "--state-dir", stateDir)
	}
	if virtual {
		args = append(args, "--virtual-device")
	}
	for _, root := range roots {
		args = append(args, "--root", root)
	}
	if writable {
		args = append(args, "--allow-agent-writes")
	}
	id := serviceID(server, machine)
	path, err := servicePath(id)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	logDir := filepath.Join(configDir(), "logs")
	if err = os.MkdirAll(logDir, 0700); err != nil {
		return err
	}
	conf, err := filepath.Abs(configDir())
	if err != nil {
		return err
	}
	env := map[string]string{"PATH": os.Getenv("PATH"), "AM_CONFIG_DIR": conf}
	for _, key := range []string{"CODEX_HOME", "CLAUDE_CONFIG_DIR"} {
		if v := os.Getenv(key); v != "" {
			env[key] = v
		}
	}
	keys := make([]string, 0, len(env))
	for key := range env {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var content string
	if runtime.GOOS == "darwin" {
		var b strings.Builder
		b.WriteString(`<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>Label</key><string>` + xmlText(id) + `</string><key>ProgramArguments</key><array>`)
		for _, a := range args {
			b.WriteString("<string>" + xmlText(a) + "</string>")
		}
		b.WriteString("</array><key>EnvironmentVariables</key><dict>")
		for _, k := range keys {
			b.WriteString("<key>" + xmlText(k) + "</key><string>" + xmlText(env[k]) + "</string>")
		}
		b.WriteString("</dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>5</integer>")
		for _, k := range []string{"StandardOutPath", "StandardErrorPath"} {
			b.WriteString("<key>" + k + "</key><string>" + xmlText(filepath.Join(logDir, id+".log")) + "</string>")
		}
		b.WriteString("</dict></plist>\n")
		content = b.String()
	} else {
		quote := func(s string) string {
			return `"` + strings.NewReplacer(`\`, `\\`, `"`, `\"`, "%", "%%", "$", "$$", "\n", `\n`, "\r", `\r`).Replace(s) + `"`
		}
		quoted := []string{}
		for _, a := range args {
			quoted = append(quoted, quote(a))
		}
		content = "[Unit]\nDescription=Relay device daemon\nAfter=network-online.target\n\n[Service]\nExecStart=" + strings.Join(quoted, " ") + "\n"
		for _, k := range keys {
			content += "Environment=" + quote(k+"="+env[k]) + "\n"
		}
		content += "Restart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=20\n\n[Install]\nWantedBy=default.target\n"
	}
	if previous, e := os.ReadFile(path); e == nil {
		if string(previous) == content {
			return restartService(server, machine)
		}
		return errors.New("后台服务已安装且设置不同；使用 daemon restart 保留原设置，或先 daemon uninstall 再按新参数安装。设备身份与历史会保留")
	} else if !os.IsNotExist(e) {
		return e
	}
	if running, e := daemonRunning(server, machine); e != nil {
		return e
	} else if running {
		return errors.New("本机已有前台后台进程；请先停止该进程，再安装自启服务，设备身份与历史会保留")
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, err = f.WriteString(content)
	closeErr := f.Close()
	if err != nil {
		os.Remove(path)
		return err
	}
	if closeErr != nil {
		os.Remove(path)
		return closeErr
	}
	if runtime.GOOS == "darwin" {
		err = runServiceCommand("launchctl", "bootstrap", "gui/"+strconv.Itoa(os.Getuid()), path)
		if err == nil {
			err = runServiceCommand("launchctl", "kickstart", "gui/"+strconv.Itoa(os.Getuid())+"/"+id)
		}
	} else {
		err = runServiceCommand("systemctl", "--user", "daemon-reload")
		if err == nil {
			err = runServiceCommand("systemctl", "--user", "enable", "--now", filepath.Base(path))
		}
	}
	if err != nil {
		return err
	}
	if err = waitDaemonRunning(server, machine); err != nil {
		return err
	}
	fmt.Println("后台服务已安装: " + path)
	return nil
}
func waitDaemonRunning(server, machine string) error {
	deadline := time.Now().Add(10 * time.Second)
	for {
		running, e := daemonRunning(server, machine)
		if e != nil {
			return e
		}
		if running {
			break
		}
		if time.Now().After(deadline) {
			return errors.New("用户服务未启动，请通过 daemon status 和服务日志检查；服务配置已保留，可用 daemon restart 重试")
		}
		time.Sleep(200 * time.Millisecond)
	}
	return nil
}
func restartService(server, machine string) error {
	id := serviceID(server, machine)
	path, err := servicePath(id)
	if err != nil {
		return err
	}
	if _, err = os.Stat(path); err != nil {
		return errors.New("后台服务未安装，请先 daemon install")
	}
	if runtime.GOOS == "darwin" {
		err = runServiceCommand("launchctl", "kickstart", "-k", "gui/"+strconv.Itoa(os.Getuid())+"/"+id)
	} else {
		err = runServiceCommand("systemctl", "--user", "restart", filepath.Base(path))
	}
	if err != nil {
		return err
	}
	return waitDaemonRunning(server, machine)
}
func uninstallService(server, machine, name string) error {
	id := serviceID(server, machine)
	path, err := servicePath(id)
	if err != nil {
		return err
	}
	if _, err = os.Stat(path); err != nil && name != "" {
		path, err = servicePath(serviceID(server, name))
		if err != nil {
			return err
		}
	}
	if _, err = os.Stat(path); err != nil {
		return errors.New("后台服务未安装")
	}
	if runtime.GOOS == "darwin" {
		err = runServiceCommand("launchctl", "bootout", "gui/"+strconv.Itoa(os.Getuid()), path)
	} else {
		err = runServiceCommand("systemctl", "--user", "disable", "--now", filepath.Base(path))
	}
	if err != nil {
		return err
	}
	if err = os.Remove(path); err != nil {
		return err
	}
	if runtime.GOOS == "linux" {
		if err = runServiceCommand("systemctl", "--user", "daemon-reload"); err != nil {
			return err
		}
	}
	fmt.Println("后台服务已卸载，设备身份和历史记录已保留")
	return nil
}
