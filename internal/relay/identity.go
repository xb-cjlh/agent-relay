package relay

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Names and directory permissions are configuration, never device identity.
// Hash the OS identifier with the account ID; never transmit the raw hardware ID.
func machineID(virtualName string) (string, error) {
	var raw string
	if runtime.GOOS == "linux" {
		for _, path := range []string{"/etc/machine-id", "/var/lib/dbus/machine-id"} {
			if data, e := os.ReadFile(path); e == nil && strings.TrimSpace(string(data)) != "" {
				raw = strings.TrimSpace(string(data))
				break
			}
		}
	} else if runtime.GOOS == "darwin" {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		output, e := exec.CommandContext(ctx, "ioreg", "-rd1", "-c", "IOPlatformExpertDevice").Output()
		if e == nil {
			match := regexp.MustCompile(`"IOPlatformUUID"\s*=\s*"([^"]+)"`).FindSubmatch(output)
			if len(match) == 2 {
				raw = string(match[1])
			}
		}
	}
	if raw == "" {
		return "", errors.New("无法读取本机标识；整机注册支持 macOS / Linux")
	}
	scope := "physical"
	if virtualName != "" {
		scope = "virtual:" + virtualName
	}
	return hash("relay-machine-v1:" + raw + ":" + strconv.Itoa(os.Getuid()) + ":" + scope), nil
}
func canonicalDeviceDir(server, machine string) string {
	return filepath.Join(configDir(), "devices", hash(server + "/" + machine)[:16])
}
func machineLockPath(server, machine string) (string, error) {
	base, e := os.UserCacheDir()
	if e != nil {
		return "", e
	}
	directory := filepath.Join(base, "relay", "daemon-locks")
	if e = os.MkdirAll(directory, 0700); e != nil {
		return "", e
	}
	return filepath.Join(directory, hash(server+"/"+machine)+".lock"), nil
}
func defaultDeviceName() string {
	name, e := os.Hostname()
	if e != nil || name == "" {
		return "my-computer"
	}
	return head(strings.TrimSuffix(name, ".local"), 80)
}
