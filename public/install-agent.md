# Relay：给 Agent 的安装说明

用于用户要求在当前设备安装 Relay CLI、agent-bridge skill 并接入设备管理。下载源和服务端是当前说明 URL 的 origin；本站为 `https://relay.chaojibot.com`。完整文档在 `/docs/`，纯文本版在 `/docs/install.md`。

## 安装范围

- 支持 macOS / Linux 的 arm64 / amd64，设备无需源码、Go 或 Java。以设备当前用户安装，不使用 sudo。
- 安装 `~/.local/bin/relayctl` 和用户要求的 Codex / Claude Code skill；默认两者都安装。
- 用户要求本机接入或后台运行时，安装用户级 LaunchAgent / systemd 服务。只需要跨设备控制入口时使用 `--no-daemon`，不注册当前机器。
- Relay 0.3 起不使用 `--root`：整机注册后可以续聊任意可用工作目录的会话、读取当前系统用户能访问的文件。相对文件路径基于该系统用户主目录；文件仍受操作系统权限和 16 MiB 大小限制。
- 不安装、登录或调用模型；先检查已安装的 `codex --version` / `claude --version`。如果两者都缺失，Relay 可观察历史，但远程续聊需要用户先安装并登录相应工具。

## 执行

1. 检查当前平台、`command -v relayctl`、`~/.local/bin/relayctl` 和本机已有 Relay 配置。不要打印令牌文件。保留已有 `AM_CONFIG_DIR`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`，不要换身份或删除状态目录。
2. 将本站 `/install.sh` 下载到临时文件，检查其作用后执行。脚本下载本站版本清单和对应二进制，校验 SHA-256 后安装。注册整机的完整安装示例：

   ```bash
   relay_install_script="$(mktemp)"
   curl -fsS 'https://relay.chaojibot.com/install.sh' -o "$relay_install_script"
   bash "$relay_install_script" --server 'https://relay.chaojibot.com' --setup
   rm -f "$relay_install_script"
   ```

   将 URL 替换为用户指定服务端。不传 `--root` 或其他目录白名单参数，接入后按当前系统用户权限直接续聊和取文件。脚本参数不要自行填入令牌。已有不同版本 skill 时，先说明旧文件将备份，再加 `--force-skill` 更新；安装任务允许更新 Relay 自己的 skill，不要改其他 skill。
3. 用户先在 Relay 网页注册/登录并在「账号与 SK」创建密钥；SK 统一为读写权限，仅访问该账号的设备。认证优先复用同一服务端已有配置，或使用已提供的私有令牌文件 `--token-file /absolute/private/file`、进程环境 `AM_TOKEN`。没有凭据时请用户在本机终端运行 configure 隐藏输入；无人值守环境无法得到令牌时，完成 CLI / skill 安装并明确剩余认证步骤。不要将 SK 写入命令参数、shell 历史、公开文件或聊天回复。
4. 前台旧进程占用整机锁时，先确认该 Relay 进程身份及是否有进行中的任务。已有用户级服务可用 `relayctl daemon restart`；前台进程需正常停止后安装服务，不启动第二个身份。已安装服务设置不同会明确报错；保留原设置，或者按本次用户指定设置卸载再安装，历史及身份保留。只影响 Relay 自己的进程。
   默认模型只读，Claude 只能使用 `Read,Glob,Grep`。用户要求在网页中执行代码、运行命令或修改文件时，安装器使用 `--setup --allow-agent-writes`，或配置后运行 `relayctl daemon install --allow-agent-writes`。已有只读服务先确认没有执行中的 Relay 任务，再卸载并按该参数安装；不要只重启。CLI 0.5.2 起，该模式保留 Claude 完整内置工具及本机 settings、skills、MCP 配置，本机禁止规则仍生效。
5. 确认 `~/.local/bin` 在后续终端 PATH 中。必要时在当前用户实际使用的 shell 配置中幂等添加这一目录，不改其他环境变量；若不改 shell，使用 CLI 绝对路径。重新打开 Codex / Claude Code 会话，让 skill 被加载。

## 验收

```bash
~/.local/bin/relayctl version
~/.local/bin/relayctl devices
~/.local/bin/relayctl agents list
~/.local/bin/relayctl daemon status
```

服务启动后等待数秒，再确认目标设备在线、当前用户的会话被发现；空白设备可能没有历史会话，这是正常情况。`daemon status` 的 `running` 表示守护进程持有本机锁，云端在线状态以 `devices` 为准。`--no-daemon` 模式下不要求本机出现或 `running=true`。

检查 skill 文件：`${CODEX_HOME:-$HOME/.codex}/skills/agent-bridge/SKILL.md` 和 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/agent-bridge/SKILL.md`，按实际目标检查。不要为验收自动调用付费模型或发送远程任务。

报告实际 CLI 路径、版本、服务端、设备 ID、后台运行状态、访问范围（当前系统用户权限）、skill 安装位置以及需要用户完成的登录步骤。安装失败时报告真实错误和已完成步骤，不把“只下载完成”描述为“设备已连接”。
