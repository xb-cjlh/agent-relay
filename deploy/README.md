# 部署自己的 Relay 服务端

Relay 服务端包含 Java API、Web 页面、安装脚本和 skill。设备通过 HTTPS 主动轮询服务端，无需开放设备入站端口。服务端使用独立的 MySQL 数据库；邮箱验证码注册需要 SMTP；图片需要私有阿里云 OSS。以下配置均为示例，请替换为自己的域名和凭据。

## Docker Compose

准备 Docker Compose、MySQL 8.0+ 数据库和支持 STARTTLS 的 SMTP 账号。先为 Relay 单独创建数据库和用户，授予该库的建表、迁移和读写权限，不要使用其他应用的库。Compose 不启动或管理你的数据库。

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env，填写数据库、SMTP 和 https://relay.example.com 等实际配置

# 构建设备安装包，需要 Go 和 Python 3.9+
make downloads

# 仅将应用绑定到宿主机回环地址，外部访问交给 HTTPS 反向代理
docker compose up -d --build
```

下载包挂载自 `dist/downloads/`，先构建后启动；没有下载包时 API 和网页仍可运行，但设备安装入口不可用。构建同一 CLI 版本不会覆盖既有目录，后续发布使用新的 `VERSION`。Java 构建在 Docker 内完成。

`.env` 的 `RELAY_AUTH_PUBLIC_ORIGIN` 必须与浏览器访问来源一致，生产保留 `RELAY_AUTH_SECURE_COOKIE=true`。`compose.yaml` 的邮件设置适用于 SMTP 587 / STARTTLS；若使用其他邮件协议，请调整相应 Spring Mail 参数。OSS 可以先留空，此时图片功能不可用。详细图片配置见 [图片上传与预览](../docs/images.md)。

将 `deploy/relay.nginx.conf` 中的 `relay.example.com` 替换为自己的域名，放入已有 Nginx 配置。证书配置使用 Let's Encrypt 文件路径；首次签发前可先配置 HTTP 站点和 `/.well-known/acme-challenge/`。确认 DNS、80/443 端口和证书后启用 HTTPS。安装设备时使用自己的源地址：

```bash
curl -fsSL https://relay.example.com/install.sh | bash -s -- \
  --server https://relay.example.com --setup
```

浏览器打开该域名，验证邮箱注册后创建 SK，用于设备注册。账号、Cookie 和 SK 均由这一 Relay 实例独立管理。

## Java / systemd

不使用 Docker 时，需要 JRE 17+；构建另需 Maven 和 Go：

```bash
make build
make downloads
```

服务器创建专用系统用户 `relay`，准备 `/var/lib/relay`、`/etc/relay`、`/opt/relay/releases/<发布号>` 和 `/opt/relay/downloads`。将 jar 放入发布目录，设置 `/opt/relay/current` 符号链接；下载目录内容复制到 `/opt/relay/downloads`。将 `application-production.properties.example` 复制为 `/etc/relay/application.properties`，填写自己的配置，权限设为 `0640 root:relay`。

`relay.service` 使用 `relay` 用户运行，监听 `127.0.0.1:8765`。配置中的 `relay.downloads` 指向独立下载目录，更新 jar 不会替换已发布的设备安装包。安装 systemd 单元后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now relay.service
curl --fail http://127.0.0.1:8765/healthz
```

`GET /healthz` 检查 MySQL 连接，不要求登录。数据目录保存中转文件；图片使用配置的 OSS 前缀。数据库启动时自动执行 Flyway 迁移，应用禁止执行 Flyway clean。

## 证书和更新

`relay-cert-renew.service` / `.timer` 是独立的 Certbot 续期示例，使用前替换域名及 Certbot 路径。timer 每天检查两次，成功续期后测试并平滑 reload Nginx；不会由应用启动自动安装。Certbot 应具有证书和 Nginx 配置的必要权限，保留 ACME HTTP 路由。

每次发布先构建和测试，上传到新的发布目录，校验 SHA-256 后切换 `current` 并仅重启 Relay。检查健康状态失败时切回上一版本。数据库迁移与存储协议必须单独评估回滚兼容性；不能将 OSS 图片版本直接回退到早期磁盘图片版本。

```bash
systemctl status relay --no-pager
journalctl -u relay -n 100 --no-pager
```

备份需要包含 MySQL、中转文件目录和 OSS 对应前缀。服务端没有对普通中转文件设置自动保留期；已发送的 OSS 图片保留，未发送草稿按图片文档清理。设备 0.5.3 起将收到的原图保存在设备配置目录，单轮回复结束和后台重启均保留，目前不自动清理。

只在需要的设备上启用 `--allow-agent-writes`。文件读取遵循该设备系统用户权限；写入模式下 Claude 的 Bash 也拥有该用户权限。需要强隔离时使用独立系统用户或容器。
