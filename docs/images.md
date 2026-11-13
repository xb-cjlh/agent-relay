# OSS 附件上传与图片预览

会话图片保存在私有阿里云 OSS。浏览器先压缩图片，通过后端签发的 OSS V4 PUT 地址直传，后端只接收摘要和大小等 JSON 元数据，通过 HEAD 核验后登记。页面预览和查看原图使用短期 GET 签名直连 OSS，图片内容不经过 Relay 服务端。OSS 长期凭据仅保存在服务端。

## 任意文件（CLI 0.6.9）

会话输入框的附件按钮支持任意文件，也支持粘贴和拖入文件。每条消息最多 8 个附件，其中原生图片最多 4 张；普通文件单个最大 16 MiB，附件合计最大 32 MiB。PDF、Office、压缩包、代码、文本和空文件保留原始字节；浏览器可解码的常见图片仍走下方的压缩及原生图片输入流程，无法解码的图片作为原文件发送。

普通文件使用 `POST /api/attachments/uploads` 和 `/api/attachments/uploads/:uploadId/complete`，同样签发 5 分钟 PUT、15 分钟内 HEAD 确认，校验大小、MD5 和 SHA-256 元数据。上传 MIME 固定为 `application/octet-stream`。发送时只传 `files: ["att_..."]`，支持与 `images` 混合发送。每个账号每日最多申请 200 个普通文件、512 MiB，同时待完成最多 20 个；单独计入文件配额。未发送文件草稿上传后保留 24 小时，之后在下一次上传时清理；已发送文件保留。

消息中的文件卡片显示名称、大小，点击下载原文件。`GET /api/attachments/:id/url` 返回短期 OSS GET 地址，稳定入口 `/api/attachments/:id` 鉴权后 302 跳转；签名强制 attachment 下载，不在网页执行 HTML、SVG 等文件。浏览器与目标设备均直接读取 OSS，文件字节不经过 Relay。用户只能访问自己的附件；`GET /api/device/attachments/:id/url` 仅向该条消息分配的设备签发下载地址，设备校验实际大小和 SHA-256 后落盘。

目标设备需要 CLI **0.6.9** 并重启后台进程。文件保存到 `~/.config/agent-manager/devices/<设备标识>/attachments/<附件 ID>/<文件名>`（`AM_CONFIG_DIR` 可改变配置目录），每个附件独立目录，目录 `0700`、文件 `0600`，原子写入；跨轮次与重启保留，目前不自动清理。名称中的路径和控制字符会被清理。Relay 将本地路径交给 Codex / Claude，由 agent 使用文件工具读取；文件格式的解析能力取决于对应 agent 和设备已安装的工具。Relay 不自动解压或执行附件。图片功能继续兼容旧版设备。

升级命令见 [安装文档](https://relay.chaojibot.com/docs/install.md)。

## 压缩

- 原始文件最大 30 MiB；长边超过 2048 像素时等比缩小，小图不放大。
- 压缩结果每张不超过 1 MiB。较小的 PNG 保持原样，缩放后体积较小的 PNG 保持无损；其他情况用 JPEG 约 84% 质量，按需降低质量或尺寸。透明图片转为 JPEG 时使用白色背景。
- SHA-256 与 Content-MD5 都针对压缩后的实际上传内容计算。每条最多 4 张，服务端和设备端继续校验每张 5 MiB、合计 12 MiB 的上限。

## 上传及鉴权

1. `POST /api/images/uploads` 提交名称、MIME、大小、SHA-256、Base64 MD5。归属用户和对象键由服务端确定，上传配额包含待完成任务。
2. 返回 5 分钟有效的固定对象 PUT 签名，绑定 Content-Length、Content-Type、Content-MD5、摘要及任务元数据，并禁止覆盖。浏览器自动生成 Content-Length，不手动设置。
3. 浏览器 PUT 压缩后的 Blob 到 OSS，使用 `credentials: omit`，不携带 Relay Cookie、SK 或 `X-Relay-Request`。
4. `POST /api/images/uploads/:uploadId/complete` 在 15 分钟内确认。服务端只 HEAD 检查大小、类型和任务摘要，确认可重复调用；丢失 PUT 响应时仍尝试确认。
5. 发送消息只传已确认的图片 ID。浏览器只能读取所属账号图片，设备只能读取分配给自己的消息图片。

## 预览及原图

页面通过 `GET /api/images/:id/url` 获取有效期 5 分钟的 OSS V4 GET 地址。Relay 只验证登录状态及图片归属并返回不缓存的签名 JSON，不读取图片内容；浏览器直接从 OSS 加载缩略图和原图。点击图片在页面内查看大图。签名仅缓存在当前页面内存中，临近到期时重新申请；延迟加载遇到过期或失败的 GET 会自动换签重试一次。刷新页面后重新签发，已显示的图片不会因签名过期而消失。

跨域预览使用 `crossorigin="anonymous"`，不向 OSS 发送 Relay Cookie、SK、请求标记或来源页面地址。签名绑定对象；图片沿用上传时签名约束并核验的 PNG/JPEG 类型，不覆盖 OSS 的响应类型限制。稳定地址 `/api/images/:id` 仍提供鉴权后 302 跳转的下载入口，正文为空；OSS 默认域名可能强制下载，因此页面内查看大图直接使用签名图片。签名是短期访问凭据，不应持久化或分享；桶保持私有。设备端仍通过 `/api/device/images/:id` 获取附件，Relay 继续验证 SHA-256、真实图片格式及分辨率，已安装的 CLI 无需更新。

对象路径为 `<relay.oss.prefix>/users/<稳定用户 ID>/images/<图片 ID>`，文件名不参与对象键。生产使用 `relay-prod/`，与知识库前缀独立。每个账号每日最多申请 200 张、200 MiB，同时待完成最多 20 张。上传时清理过期的未完成对象和超过 24 小时的未发送草稿；已发送图片保留。没有新的上传时不会触发清理。

## 设备端原图保留

CLI 0.5.3 起，设备收到的原图保存在 `~/.config/agent-manager/devices/<设备标识>/images/<图片 ID>.png|jpeg`；设置 `AM_CONFIG_DIR` 时使用对应配置目录。目录权限为 `0700`，文件为 `0600`，校验完成后原子写入。会话历史引用稳定路径，回复结束或重启后台进程不会删除原图，目前不自动过期；需要保留会话继续看图时请保留该目录。

0.5.2 及以前在单轮回复结束后删除临时原图，下一轮的裁剪或查看工具可能提示文件不存在。升级并重启目标设备的后台进程才能修正后续图片；升级不会自动恢复旧临时路径。旧会话可重新发送图片继续讨论，OSS 中已发送的图片和网页历史仍保留。Agent 自行生成的裁剪图、依赖安装和脚本错误由对应工具处理。

## 服务端配置

```properties
relay.oss.endpoint=https://oss-cn-hangzhou-internal.aliyuncs.com
relay.oss.upload-endpoint=https://oss-cn-hangzhou.aliyuncs.com
relay.oss.region=cn-hangzhou
relay.oss.bucket=YOUR_PRIVATE_BUCKET
relay.oss.access-key-id=SERVER_ONLY
relay.oss.access-key-secret=SERVER_ONLY
relay.oss.security-token=
relay.oss.prefix=relay-prod/
```

后端读取可以使用内网 endpoint，浏览器上传和预览地址必须是公共 HTTPS 地址，均使用 `relay.oss.upload-endpoint`。未配置 OSS 时图片功能返回不可用提示。Bucket 保持私有；应用对象凭据需要该前缀的读写和清理权限。

CORS 维护账号另需 `oss:GetBucketCors` 和 `oss:PutBucketCors`。显式运行维护工具，为 `relay.auth.public-origin` 指定的精确 HTTPS 来源配置 PUT、GET、HEAD 规则，先备份并保留已有规则；应用启动不会更改共享桶配置：

```bash
mvn -q -f server/pom.xml compile dependency:build-classpath -Dmdep.outputFile=target/runtime-classpath.txt
relay_oss_cp="server/target/classes:$(cat server/target/runtime-classpath.txt)"
mkdir -p server/target/maintenance
javac --release 17 -cp "$relay_oss_cp" -d server/target/maintenance scripts/ConfigureImageCors.java
java -cp "server/target/maintenance:$relay_oss_cp" dev.relay.ConfigureImageCors /private/relay.properties --check
java -cp "server/target/maintenance:$relay_oss_cp" dev.relay.ConfigureImageCors /private/relay.properties --apply /private/relay-cors-before.json
```

上传和预览签名均不应写入日志或浏览器持久化存储。应用 CSP 的 `connect-src` 和 `img-src` 仅额外允许配置的精确 OSS origin。备份需要同时保留 MySQL 和对应的 OSS 对象。
