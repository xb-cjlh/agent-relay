# Relay 安装与使用

服务端：https://relay.chaojibot.com 。所有安装脚本、二进制和 skill 均由此服务端提供；用户无需拉取项目源码。

## 1. 准备

支持 macOS（Apple Silicon / Intel）和 Linux（arm64 / amd64），需要 bash、curl 和 SHA-256 工具（系统通常自带）。不需要 Go、Java、Python 或 sudo。Windows 暂不支持。

要远程继续 Codex / Claude Code 对话，需要在执行任务的设备上安装、登录对应 CLI：

```bash
codex --version
claude --version
```

只安装自己使用的工具即可。登录步骤参见各自官方文档：

- Codex：https://developers.openai.com/codex/cli/
- Claude Code：https://code.claude.com/docs/en/setup

Relay 不代装模型 CLI，不复制登录凭据，安装验收不会调用模型。还需在 Relay 网页注册自己的邮箱账号，并在「账号与 SK」创建一个 SK，供 CLI 和 Agent 使用。SK 统一为读写权限，只访问所属账号的数据；可设置 1–365 天有效期，随时撤销，完整密钥只显示一次。设备接入后自动发现当前用户各目录的会话，允许远程续聊和读取当前系统用户可访问的文件，无需配置目录白名单。

## 注册登录

Relay 使用独立账号体系，与知屿等应用的账号无关。注册需邮箱验证码（10 分钟有效、最多尝试 5 次），密码至少 8 字符且不超过 72 个 UTF-8 字节。浏览器使用 HttpOnly Cookie 登录，无需在网页保存 SK。忘记密码可用邮箱验证码重置；重置后旧登录会话和 SK 失效，设备身份继续保留。

登录后在「账号与 SK」创建密钥，再用这个 SK 配置 CLI 并注册设备。每台设备首次接入就会自动发现当前系统用户已有的 Codex / Claude Code 会话。

## 2. 让 Agent 一句话安装

CLI 0.6.3 会过滤 Codex 自动注入的 AGENTS.md 指令和环境上下文；0.6.2 起也会过滤 Codex 的上下文压缩摘要和 Claude 的内部续聊摘要。旧设备更新后重启后台进程：

```bash
curl -fsSL https://relay.chaojibot.com/install.sh | bash -s -- --cli-only
"$HOME/.local/bin/relayctl" daemon restart
```

更新保留设备身份、会话和原有访问设置；重启前等待当前 Relay 任务执行完成。

在目标设备的 Codex 或 Claude Code 中发送：

> 请阅读 https://relay.chaojibot.com/install-agent.md，帮我安装并连接 Relay CLI 和 agent-bridge skill，设置后台自启并同步整机会话，令牌优先复用本机已有配置，没有则提示我在终端隐藏输入。

只想让本机 Agent 管理其他设备时，补充“只安装控制端，不注册本机后台进程”。安装说明公开可读，不包含令牌，登录后网页也可复制这句话。

## 3. 安装命令

仅安装 CLI 和两个工具的 skill：

```bash
curl -fsSL https://relay.chaojibot.com/install.sh | bash
```

完整接入并设置后台自启：

```bash
curl -fsSL https://relay.chaojibot.com/install.sh | bash -s -- --setup
```

上述安装默认让模型只读。如果需要网页中的 Claude 执行 Python、运行命令或修改文件，首次安装使用：

```bash
curl -fsSL https://relay.chaojibot.com/install.sh | bash -s -- --setup --allow-agent-writes
```

已有只读后台服务先执行 `relayctl daemon uninstall`，再用该命令安装；身份和会话保留。只更新 CLI 或 `daemon restart` 不会改变原来的读写设置。

接入后可以继续当前用户任意可用工作目录的会话，并读取该用户有权限访问的文件。首次运行会在有交互终端时隐藏读取令牌，已配置同一服务端则复用。

无人值守安装使用已有私有令牌文件，先把文件权限设为 `0600`：

```bash
curl -fsSL https://relay.chaojibot.com/install.sh | bash -s -- \
  --setup --token-file "$HOME/.secrets/relay-token"
```

令牌不应放在 `--token` 参数或聊天消息里。也可通过受控进程环境提供 `AM_TOKEN`。脚本不会将令牌加到下载请求、安装 URL 或日志中。

常用选项：

| 选项 | 作用 |
| --- | --- |
| `--server URL` | 指定下载和连接的服务端；默认本站 |
| `--bin-dir PATH` | 默认 `~/.local/bin`；需绝对路径 |
| `--setup` | 配置认证并安装后台自启服务 |
| `--setup --no-daemon` | 只配控制端，不注册本机 |
| `--setup --allow-agent-writes` | 允许模型执行命令和修改文件 |
| `--name NAME` | 可选的设备名称，默认主机名 |
| `--token-file PATH` | 从私有文件读取令牌 |
| `--skill-target codex` | 只装 Codex skill；也可 `claude` 或默认 `both` |
| `--force-skill` | 先备份旧 skill，再更新为当前版本 |
| `--cli-only` | 仅下载 CLI，不装 skill、不连接 |

安装器识别系统与 CPU，从 `/downloads/current.json` 读取版本，下载该版本对应文件及 `SHA256SUMS`，校验成功后原子替换 CLI。校验失败不安装。它不修改 shell 配置；若 `relayctl` 不在 PATH，始终可用 `~/.local/bin/relayctl`。

将下面一行加到实际使用的 shell 配置（zsh 为 `~/.zshrc`，bash 通常为 `~/.bashrc` 或登录配置），重新打开终端即可：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 4. 分步安装、配置和连接

适合需要逐步检查，或已经手动下载二进制的情况：

```bash
# 只安装 CLI
curl -fsSL https://relay.chaojibot.com/install.sh | bash -s -- --cli-only
export PATH="$HOME/.local/bin:$PATH"

# 配置连接：隐藏输入，或在命令末尾增加 --token-file /private/file
relayctl configure --server https://relay.chaojibot.com

# 安装 skill，both 为默认值
relayctl skill install --target both

# 在前台运行，确认设备在线（按 Ctrl-C 停止）
relayctl daemon run

# 停止前台进程后，安装后台自启
relayctl daemon install
```

macOS 使用当前用户 LaunchAgent，Linux 使用 `systemd --user`。Linux 必须有可用的用户 systemd 会话；SSH/容器环境若不可用，可先用 `daemon run`。Linux 退出登录后继续运行需管理员按系统策略启用该用户 linger；安装器不会改系统设置。

Relay 0.3 起已取消 `--root` 目录限制。旧后台服务中残留的参数会由新版 CLI 兼容忽略；升级后无需重新注册设备。

设备按机器、系统用户及服务端识别，换目录或名称不会创建新设备。不要删除身份文件或给同一机器反复指定不同 `--state-dir`。现有前台进程必须先正常停止后再安装自启。

CLI 0.5 起支持网页发送图片。选择图片、粘贴截图或拖入输入框后可预览和移除，发送后图片保留在本条消息中，Codex / Claude Code 会收到原生图片输入。浏览器在上传前将长边缩至最多 2048 像素，目标每张不超过 1 MiB；不放大小图，较小的 PNG 保持无损，其他图片按需转换为 JPEG。无法读取的 HEIC 请先转为 JPEG。每条消息最多 4 张，服务端与设备端保留每张 5 MiB、合计 12 MiB 的校验上限。

图片通过短期预签名 PUT 从浏览器直传私有 OSS，上传签名绑定大小、类型、MD5 和图片摘要，禁止覆盖；服务端核验完成后才可发送。页面预览和大图经 Relay 校验账号归属、取得 5 分钟有效的 OSS GET 地址后直接加载，图片内容直达浏览器；点击图片查看大图，签名过期时自动重新获取。设备取图按目标设备隔离。存储配置与 CORS 维护见 [OSS 图片上传与预览](https://relay.chaojibot.com/docs/images.md)。本次服务端更新不需要升级已支持图片的设备 CLI。

需要在后续轮次继续查看或裁剪原图时，请升级目标设备到 CLI 0.5.3 或更新版本并重启后台进程。原图保存在设备配置目录的 `devices/<设备标识>/images/` 下，不再随单轮回复结束删除，也不会因后台进程重启而消失。此前已被删除的临时原图需要重新发送。

旧设备需升级 CLI 并重启后台进程，网页才会启用图片按钮。已安装旧 skill 时更新：

```bash
curl -fsSL https://relay.chaojibot.com/install.sh | bash -s -- --force-skill
relayctl daemon restart
```

若使用 `daemon run`，先正常停止旧 Relay，再用原身份目录启动。无需退出 Codex 或重新注册设备。

## 5. 安装 skill

CLI 内嵌完整 skill，不依赖网络安装第二份内容：

```bash
relayctl skill install --target codex
relayctl skill install --target claude
# 两者一起安装
relayctl skill install --target both
# 更新已有的不同版本，原文件先备份
relayctl skill install --target both --force
```

默认位置：

- Codex：`~/.codex/skills/agent-bridge/SKILL.md`，支持 `CODEX_HOME`。
- Claude Code：`~/.claude/skills/agent-bridge/SKILL.md`，支持 `CLAUDE_CONFIG_DIR`。
- 自定义 skill 父目录：`relayctl skill install --path /absolute/skills`。

安装后重新打开相应 Agent 会话。可以直接说“使用 agent-bridge，列出设备和可用会话”，或者“把家里电脑的 XX 文件取回当前目录”。原始 skill 可从 `/skills/agent-bridge/SKILL.md` 下载，不包含任何用户令牌。

## 6. 检查安装与使用

```bash
relayctl version
relayctl devices
relayctl agents list
relayctl daemon status
```

等待数秒，网页中应出现一个在线设备，并陆续看到当前用户各项目的已有会话。全新设备没有历史会话属于正常情况。`daemon status` 的 `running` 表示本地进程存在，云端是否在线以 `devices` 为准；只安装控制端无需本机后台进程。

多台设备接入后，用侧栏顶部「切换设备」查看指定设备的会话，或选「全部设备」；选项显示会话数与在线状态。全部设备的会话按最新活跃时间混排，按今天、昨天、近 7 天、近 30 天和更早分段，每条显示简短的设备与 Agent 标记，长名称省略，悬停可看完整信息。设备弹窗也提供「查看会话」。切换会恢复该设备上次查看的会话，刷新后保留设备选择，新建会话和取文件默认使用当前设备。搜索仅在当前选择的设备范围内生效。

常用操作（先通过列表取得真实 ID）：

```bash
relayctl send AGENT_ID '阅读 README，说明如何启动项目' --wait
relayctl jobs show JOB_ID
relayctl jobs wait JOB_ID --timeout 600
relayctl jobs cancel JOB_ID
relayctl files pull --device DEVICE_ID --path 'XX.pdf' --output './XX.pdf'
relayctl agents create --device DEVICE_ID --kind codex --name project-codex --cwd /absolute/project
```

任务会调用设备上已登录的模型工具；同一会话按顺序执行。原生会话通过原 session ID 续接。离线时任务排队。文件经服务端中转，最大 16 MiB，校验 SHA-256，不覆盖已有本地文件。相对文件路径基于远端系统用户主目录，也支持 `~/` 和绝对路径。

会话回复中的 Markdown 文件链接可直接点击取回，在右侧预览并「下载原文件」，手机上显示全屏预览。支持绝对路径、`file:///`、`~/` 和相对路径，例如 `[说明](docs/README.md)`、`[代码](src/main.go#L20)`；这里的相对路径基于该会话的工作目录，预览文档内的相对链接则基于文档所在目录，始终从原设备取回。Markdown、UTF-8 / UTF-16 文本和代码、常见图片可预览；其他格式可下载查看。文本最多预览前 256 KiB / 5000 行，下载保留完整原文件。设备离线时等待上线，失败时可重试；关闭未完成的预览会取消取回。普通网页链接仍在新标签页打开。此功能无需更新设备端 CLI。

成功取回的原文件保存在当前浏览器的 IndexedDB 中，最后成功校验后保留 30 天，刷新页面和重新打开浏览器仍可复用；按账号、设备和路径隔离，退出登录会删除该账号的缓存。缓存最多 100 个文件、合计 128 MiB，优先保留最近使用的文件。打开预览时先显示缓存，再自动检测源文件变化，保持预览打开时每 10 秒检查一次，回到页面后也会补查。内容变化后自动更新预览与下载，无需手动刷新；离线或检查失败时继续显示缓存并自动重试。CLI 0.6.4 起，设备按 SHA-256 检查完整内容，未变化时只返回校验结果、不上传原文件；旧设备兼容自动检查，每 60 秒进行一次，更新 CLI 并重启后台进程后可省去未变化文件的上传。浏览器限制本地存储时会退回内存缓存。

Relay 0.4 起，仍在本机 Codex 共享服务中打开的会话会通过原服务接续，保留原会话 ID；等待当前回复结束后再执行，不再启动第二个会话写入进程。本轮使用 Relay 的模型写权限设置，提交后立即恢复本机会话后续轮次的原权限。共享服务需支持独立更新后续轮次设置，已验证 Codex 0.166 / 0.167。若旧版 Codex 或独立进程未提供共享服务，需先在本机退出这个会话，再从 Relay 继续；无需关闭其他会话。升级 CLI 后重启 Relay 后台进程才会生效。

不同目录的会话可以直接续聊。工作目录已删除、无法访问或缺少对应 CLI 时仍显示历史和新消息，页面提示当前不可执行。默认模型只读；远程访问可用目录不改变模型写权限。需要执行代码或写文件时，在设备端使用 `relayctl daemon install --allow-agent-writes`（前台为 `relayctl daemon run --allow-agent-writes`）。已有只读自启服务先 `relayctl daemon uninstall` 再安装，配置、设备身份和会话保留；仅重启仍沿用原设置。

CLI 0.6.0 起，Codex 与 Claude Code 会话的输入框旁可选择「设备默认」「按需审批」「完全访问」。后两项需升级目标设备 CLI、执行 `relayctl daemon restart`，并在设备端启用 `--allow-agent-writes`；旧设备或只读设备上不可选择。权限从下一条发送的消息生效，已提交消息的权限不会变化。

支持权限选择且已启用可写时，网页默认选择「完全访问」；手动选择会在当前浏览器标签页中按会话记住。旧设备或只读设备仍使用「设备默认」，CLI 的默认权限不变。

按需审批时，Codex 使用项目读写沙箱，在申请额外权限时由网页确认；Claude 遵循本机权限规则，需要确认的工具调用显示为审批卡片。查看命令、路径或工具参数后选择允许此次操作或拒绝。刷新页面可继续处理待审批，允许不创建永久规则；取消、超时、失联或请求撤回后不可再批准。CLI 0.6.1 起默认无固定时限，直到完成或手动停止。显式通过 `relayctl send --timeout 600` 设置时限时，等待审批也计入该时限。只处理 Relay 发起的轮次，原生客户端的审批仍留在原客户端；Claude `AskUserQuestion` 等填写答案的交互暂不支持。

完全访问对应 Codex `danger-full-access` / Claude `bypassPermissions`，允许以设备用户权限执行命令、访问文件和网络；本机或组织强制规则仍可能限制操作。请仅对信任的任务使用。需要操作系统隔离时使用独立用户或容器。

CLI 0.6.1 起，网页和 `relayctl send` 默认不设执行时限，Codex 和 Claude Code 均执行到完成或手动停止；可使用 `--timeout 600` 设置 1–1800 秒上限，`--timeout 0` 表示不限。`send --wait` 默认一直等待；`jobs wait --timeout` 仅影响本次等待，不取消任务。内部日志放在网页可展开的「执行日志」，与助手回复分开；设备失联和手动取消保护保留。旧设备需升级并执行 `relayctl daemon restart`，否则网页会提示升级，防止继续沿用旧的 5 分钟限制。

CLI 0.5.2 起，开启写入后 Claude 使用完整内置工具并加载本机 settings、skills 和 MCP 配置。常用读写和 Bash 工具自动允许，本机禁止规则仍然生效；其他需要确认的工具按本机已有允许规则执行。默认只读模式仍仅开放文件读取工具。此选项不构成完整的操作系统沙箱。

## 7. 配置、更新和卸载

配置与身份保存在 `~/.config/agent-manager/`，可用 `AM_CONFIG_DIR` 指定其他目录。配置和身份文件为 `0600`，保存令牌，勿公开分享。守护进程保留安装时的 PATH、`CODEX_HOME` 和 `CLAUDE_CONFIG_DIR`。

- 重启已有服务：`relayctl daemon restart`，保留安装参数和身份。
- 查看本地状态：`relayctl daemon status`。
- 更新 CLI / skill：重新运行安装器，已有 skill 内容不同则加 `--force-skill`；已安装后台服务的用户随后执行 `relayctl daemon restart` 使新二进制生效。首次安装和更新均不删除身份。
- 修改安装参数：`relayctl daemon uninstall`，再使用完整的新参数运行 `daemon install`。卸载仅停止 Relay 自己的服务，保留云端历史和本地身份。
- macOS 日志：`~/.config/agent-manager/logs/`（或自定义配置目录）。
- Linux 日志：`journalctl --user -u 'dev.relay.agent-*'`。

卸载后台服务和命令：

```bash
relayctl daemon uninstall
rm "$HOME/.local/bin/relayctl"
```

如需删除 skill，先检查 `agent-bridge/SKILL.md` 的实际路径和备份，再手动删除对应 `agent-bridge` 目录。安装器不自动删除配置、身份、历史或其他 skill。

## 8. 排查

- `command not found`：使用 `~/.local/bin/relayctl`，按前文配置 PATH。
- `401 / 访问令牌无效`：核对服务端，重新 `configure`，请使用在网页创建的 SK，不要把设备自己的令牌用于管理 CLI。
- 未显示当前设备：确认不是只装了 CLI，执行 `daemon status`，检查服务日志及 HTTPS 连接。
- 已连接但没有会话：先在对应 CLI 正常创建会话，核对其 `CODEX_HOME` / `CLAUDE_CONFIG_DIR` 与守护进程是否一致；记录约 5 秒扫描一次并分批同步。
- 提示本机已有进程：用 `daemon status` 确认身份，正常停止旧前台 Relay 进程，或重启已有服务，不再次注册。
- skill 内容冲突：使用 `--force`（CLI）或 `--force-skill`（安装器）备份后更新。
- 服务安装设置不同：使用 `daemon restart` 沿用原设置，或卸载再以新参数安装。
- Linux 用户服务不可用：在真实登录用户会话下安装；受限容器可先前台运行。
- 校验失败 / 下载中断：重新下载，安装器保留原可执行文件；不要跳过校验。
- `already has an active writer`：原会话由本机 Codex 持有。先更新设备端 Relay 到 0.4 或更新版本，再执行 `relayctl daemon restart`；若后台以 `daemon run` 运行，正常停止后重新启动。Codex 未提供本机共享服务时，先在本机退出该会话后再发送。历史失败指令不会自动重发。
- 模型未登录：在目标设备按工具官方流程登录，Relay 不代替模型认证。

公共下载版本清单：`/downloads/current.json`；每个版本在 `/downloads/VERSION/` 提供四个平台的 `relayctl` 与 `SHA256SUMS`。网页 `/docs/` 展示直接下载链接。

## 服务端邮件设置

注册和密码重置依赖 SMTP。部署者在 Relay 独立配置中设置 `relay.mail.from`、`spring.mail.host`、`spring.mail.port`、`spring.mail.username`、`spring.mail.password`，以及 `spring.mail.properties.mail.smtp.auth/starttls.enable/starttls.required/ssl.enable`。HTTPS 部署还需设置 `relay.auth.public-origin=https://relay.chaojibot.com` 和 `relay.auth.secure-cookie=true`。邮件主题为 Relay，不读取或复用其他应用的用户表、验证码、Cookie 或 SK。
