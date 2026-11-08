package relay

import (
	"os"
	"path/filepath"
	"syscall"
)

func daemonRunning(server, machine string) (bool, error) {
	path, err := machineLockPath(server, machine)
	if err != nil {
		return false, err
	}
	f, err := os.OpenFile(path, os.O_RDWR, 0600)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	defer f.Close()
	err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err == syscall.EWOULDBLOCK || err == syscall.EAGAIN {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	return false, nil
}
func daemonStatus(server, machine string) error {
	running, err := daemonRunning(server, machine)
	if err != nil {
		return err
	}
	path, err := servicePath(serviceID(server, machine))
	if err != nil {
		return err
	}
	_, statErr := os.Stat(path)
	if statErr != nil && !os.IsNotExist(statErr) {
		return statErr
	}
	state, err := readJSON(filepath.Join(canonicalDeviceDir(server, machine), "identity.json"))
	if err != nil {
		return err
	}
	home, e := os.UserHomeDir()
	if e != nil {
		return e
	}
	return emit(M{"server": server, "running": running, "installed": statErr == nil, "service_path": path, "device_id": state["id"], "name": state["name"], "access_scope": "user", "home": home})
}
