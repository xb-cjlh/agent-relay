package relay

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"
)

func projectDir() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Dir(filepath.Dir(filepath.Dir(file)))
}
func java17() (string, error) {
	candidates := []string{}
	if h := os.Getenv("JAVA_HOME"); h != "" {
		candidates = append(candidates, filepath.Join(h, "bin/java"))
	}
	if p, e := exec.LookPath("java"); e == nil {
		candidates = append(candidates, p)
	}
	candidates = append(candidates, "/opt/homebrew/opt/openjdk/bin/java", "/opt/homebrew/opt/openjdk@21/bin/java", "/opt/homebrew/opt/openjdk@17/bin/java", "/usr/local/opt/openjdk/bin/java")
	matches, _ := filepath.Glob("/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java")
	candidates = append(candidates, matches...)
	re := regexp.MustCompile(`version "([0-9]+)`)
	for _, p := range candidates {
		b, e := exec.Command(p, "-version").CombinedOutput()
		if e != nil {
			continue
		}
		m := re.FindStringSubmatch(string(b))
		if len(m) > 1 {
			n, _ := strconv.Atoi(m[1])
			if n >= 17 {
				return p, nil
			}
		}
	}
	return "", errors.New("启动 Java 服务端需要 JDK 17 或更高版本；设备守护进程不需要 Java")
}
func Serve(host string, port int, data string) error {
	java, err := java17()
	if err != nil {
		return err
	}
	root := projectDir()
	jar := os.Getenv("RELAY_SERVER_JAR")
	if jar == "" {
		executable, _ := os.Executable()
		candidates := []string{filepath.Join(filepath.Dir(executable), "relay-server.jar"), "server/target/relay-server.jar", filepath.Join(root, "server/target/relay-server.jar")}
		for _, p := range candidates {
			if _, e := os.Stat(p); e == nil {
				jar = p
				break
			}
		}
	}
	if jar == "" {
		pom := filepath.Join(root, "server/pom.xml")
		if _, err = os.Stat(pom); err != nil {
			return errors.New("找不到 relay-server.jar；请设置 RELAY_SERVER_JAR 或将 jar 放在 relayctl 旁")
		}
		resolved, e := filepath.EvalSymlinks(java)
		if e != nil {
			return e
		}
		cmd := exec.Command("mvn", "-q", "-f", pom, "-DskipTests", "package")
		cmd.Env = replaceEnv(os.Environ(), "JAVA_HOME", filepath.Dir(filepath.Dir(resolved)))
		cmd.Stdout = os.Stderr
		cmd.Stderr = os.Stderr
		fmt.Fprintln(os.Stderr, "正在构建 Java 服务端…")
		if err = cmd.Run(); err != nil {
			return err
		}
		jar = filepath.Join(root, "server/target/relay-server.jar")
	}
	jar, err = filepath.Abs(expand(jar))
	if err != nil {
		return err
	}
	data, err = filepath.Abs(expand(data))
	if err != nil {
		return err
	}
	env := os.Environ()
	args := []string{java, "-jar", jar, "--server.address=" + host, "--server.port=" + strconv.Itoa(port), "--relay.data=" + data}
	return syscall.Exec(java, args, env)
}
func replaceEnv(env []string, key, value string) []string {
	result := []string{}
	for _, v := range env {
		if !strings.HasPrefix(v, key+"=") {
			result = append(result, v)
		}
	}
	return append(result, key+"="+value)
}
