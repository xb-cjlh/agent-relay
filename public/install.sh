#!/usr/bin/env bash
set -eo pipefail

# No sudo, no package manager, no shell profile edits.
relay_server='https://relay.chaojibot.com'
relay_bin_dir="${RELAY_INSTALL_DIR:-$HOME/.local/bin}"
relay_setup=false
relay_daemon=true
relay_allow_writes=false
relay_skill=true
relay_skill_target=both
relay_force_skill=false
relay_token_file=''
relay_name=''
fail() { printf 'Relay: %s\n' "$*" >&2; exit 1; }
need_value() { [[ $# -ge 2 && -n "$2" ]] || fail "$1 需要参数"; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --server) need_value "$@"; relay_server="${2%/}"; shift 2 ;;
        --bin-dir) need_value "$@"; relay_bin_dir="$2"; shift 2 ;;
        --setup) relay_setup=true; shift ;;
        --no-daemon) relay_daemon=false; shift ;;
        --allow-agent-writes) relay_allow_writes=true; shift ;;
        --cli-only) relay_skill=false; shift ;;
        --skill-target) need_value "$@"; relay_skill_target="$2"; shift 2 ;;
        --force-skill) relay_force_skill=true; shift ;;
        --token-file) need_value "$@"; relay_token_file="$2"; shift 2 ;;
        --name) need_value "$@"; relay_name="$2"; shift 2 ;;
        --help|-h)
            cat <<'HELP'
Relay installer (macOS / Linux, amd64 / arm64)
  --server URL           下载与连接的服务端，默认 https://relay.chaojibot.com
  --bin-dir PATH         CLI 目录，默认 ~/.local/bin（不修改 shell 配置）
  --setup                配置连接并安装用户后台服务
  --no-daemon            配合 --setup，只配置 CLI / skill，不注册本机
  --allow-agent-writes   配合 --setup，允许模型执行代码和修改文件
  --name NAME            设备名称，默认主机名
  --token-file PATH      从私有文件读取令牌；也可通过 AM_TOKEN 或隐藏输入
  --skill-target TARGET  both / codex / claude，默认 both
  --force-skill          备份并更新已有的不同版本 skill
  --cli-only             仅安装 CLI（不能与 --setup 一起使用）
不带 --setup 时，只安装 CLI 和 skill，不连接服务端或启动进程。
HELP
            exit 0 ;;
        *) fail "未知参数: $1" ;;
    esac
done
case "$relay_skill_target" in both|codex|claude) ;; *) fail 'skill-target 必须为 both / codex / claude' ;; esac
if $relay_setup && ! $relay_skill; then fail '--cli-only 不能与 --setup 一起使用'; fi
if ! $relay_setup && { [[ -n "$relay_token_file" || -n "$relay_name" ]] || ! $relay_daemon; }; then
    fail '--name / --token-file / --no-daemon 需要 --setup'
fi
if $relay_allow_writes && { ! $relay_setup || ! $relay_daemon; }; then
    fail '--allow-agent-writes 需要 --setup，且不能与 --no-daemon 一起使用'
fi
case "$relay_server" in
    https://*) ;;
    http://127.0.0.1:*|http://localhost:*) ;;
    *) fail '服务端必须使用 HTTPS；本机回环测试可使用 HTTP' ;;
esac
[[ "$relay_server" != *'?'* && "$relay_server" != *'#'* && "$relay_server" != *'@'* && "$relay_server" != *$'\n'* ]] || fail '服务端 URL 无效'
[[ "$relay_bin_dir" == /* && "$relay_bin_dir" != *$'\n'* ]] || fail 'bin-dir 必须为绝对路径'
command -v curl >/dev/null || fail '需要 curl'
case "$(uname -s)" in Darwin) relay_os=darwin ;; Linux) relay_os=linux ;; *) fail '当前仅支持 macOS / Linux' ;; esac
case "$(uname -m)" in arm64|aarch64) relay_arch=arm64 ;; x86_64|amd64) relay_arch=amd64 ;; *) fail '当前仅支持 amd64 / arm64' ;; esac
relay_temp="$(mktemp -d "${TMPDIR:-/tmp}/relay-install.XXXXXXXX")"
trap 'rm -rf -- "$relay_temp"' EXIT
fetch() { curl --fail --silent --show-error --connect-timeout 15 --max-time 180 "$1" --output "$2"; }
fetch "$relay_server/downloads/current.json" "$relay_temp/current.json"
relay_version="$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([a-zA-Z0-9._-]*\)".*/\1/p' "$relay_temp/current.json")"
[[ "$relay_version" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || fail '下载版本清单无效'
relay_asset="relayctl-$relay_os-$relay_arch"
fetch "$relay_server/downloads/$relay_version/SHA256SUMS" "$relay_temp/SHA256SUMS"
fetch "$relay_server/downloads/$relay_version/$relay_asset" "$relay_temp/relayctl"
relay_expected="$(awk -v asset="$relay_asset" '$2 == asset {print $1}' "$relay_temp/SHA256SUMS")"
[[ "$relay_expected" =~ ^[0-9a-f]{64}$ ]] || fail '下载校验清单无效'
if command -v shasum >/dev/null; then
    relay_actual="$(shasum -a 256 "$relay_temp/relayctl" | awk '{print $1}')"
elif command -v sha256sum >/dev/null; then
    relay_actual="$(sha256sum "$relay_temp/relayctl" | awk '{print $1}')"
else fail '需要 shasum 或 sha256sum'; fi
[[ "$relay_expected" == "$relay_actual" ]] || fail 'CLI 的 SHA-256 校验失败，未安装'
mkdir -p -- "$relay_bin_dir"
[[ ! -d "$relay_bin_dir/relayctl" ]] || fail '目标 relayctl 是目录'
# Rename the verified file inside the same filesystem; running processes keep their inode.
relay_staged="$(mktemp "$relay_bin_dir/.relayctl.XXXXXXXX")"
trap 'rm -rf -- "$relay_temp"; if [[ -n "${relay_staged:-}" ]]; then rm -f -- "$relay_staged"; fi' EXIT
cp -- "$relay_temp/relayctl" "$relay_staged"
chmod 0755 "$relay_staged"
mv -f -- "$relay_staged" "$relay_bin_dir/relayctl"
relay_staged=''
export PATH="$relay_bin_dir:$PATH"
relay_cli="$relay_bin_dir/relayctl"
"$relay_cli" version
if $relay_skill; then
    relay_skill_args=(skill install --target "$relay_skill_target")
    if $relay_force_skill; then relay_skill_args+=(--force); fi
    "$relay_cli" "${relay_skill_args[@]}"
fi
if $relay_setup; then
    relay_config_args=(configure --server "$relay_server")
    if [[ -n "$relay_token_file" ]]; then relay_config_args+=(--token-file "$relay_token_file"); fi
    # The installer may be piped to bash, so hidden interactive input uses the terminal.
    if [[ -z "$relay_token_file" && -z "${AM_TOKEN:-}" && -r /dev/tty ]] && ( : </dev/tty ) 2>/dev/null; then
        "$relay_cli" "${relay_config_args[@]}" </dev/tty
    else
        "$relay_cli" "${relay_config_args[@]}" </dev/null
    fi
    if $relay_daemon; then
        relay_daemon_args=(--server "$relay_server" daemon install)
        if $relay_allow_writes; then relay_daemon_args+=(--allow-agent-writes); fi
        if [[ -n "$relay_name" ]]; then relay_daemon_args+=(--name "$relay_name"); fi
        "$relay_cli" "${relay_daemon_args[@]}"
        "$relay_cli" --server "$relay_server" daemon status
    fi
fi
printf '\n安装完成：%s\n' "$relay_cli"
printf 'CLI 未在 PATH 时使用该绝对路径，或把 %s 加入 PATH。\n' "$relay_bin_dir"
printf '重新打开 Codex / Claude Code 会话以加载 agent-bridge skill。\n使用文档：%s/docs/\n' "$relay_server"
