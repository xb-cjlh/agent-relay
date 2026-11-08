# 参与 Relay

欢迎提交问题和 Pull Request。涉及远程执行、账号隔离或文件访问的改动，请说明受影响的权限及失败时的行为；漏洞请按 [安全报告方式](SECURITY.md) 私下报告。

## 开发环境

- Go 1.18+；JDK 17+ 和 Maven 3.6.3+。
- MySQL 8.0+ 和 `mysql` 客户端，使用专门的本地测试实例。
- Python 3.9–3.11：测试邮件接收器使用 `smtpd`，该标准库模块已在 Python 3.12 移除。
- Node.js 用于前端语法检查；页面不需要 npm 构建。

```bash
make build
go test -race ./...
go vet ./...
node --check relayctl/static/app.js
node --check relayctl/static/account.js
node --check relayctl/static/image-upload.js
```

HTTP 集成测试会创建及删除随机 `relay_test_*` 数据库。先配置只连接 `127.0.0.1` 的 `.local/mysql-test.json`，格式见 [README](README.md#验证)，不要使用生产数据库凭据。

```bash
python3 -m unittest discover -s tests -v
```

浏览器测试需要 Playwright：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r tests/requirements.txt
.venv/bin/python -m playwright install chromium webkit
.venv/bin/python tests/auth_loading_smoke.py
.venv/bin/python tests/account_images_smoke.py
```

测试默认使用模拟 Agent 和本机 HTTP/SMTP/OSS 协议服务，不需要云密钥或付费模型。`RELAY_CODEX_TEST=1`、`RELAY_CLAUDE_TEST=1` 才启用可选真实 CLI 协议测试，仍指向本机模拟模型；不要把用户会话作为测试输入。

## 提交变更

从 `main` 创建分支，说明具体问题、最终行为和验证结果。新增配置或 CLI 参数时同步修改文档。请保持 Go / Java 服务职责边界和页面的简洁交互，保留已有单元测试与集成测试中的账号隔离检查。

仓库不包含本地配置、密钥、用户会话、数据库、截图、下载包和生产部署记录；这些文件由 `.gitignore` 排除。发布设备 CLI 时运行 `make downloads VERSION=<新版本>`，每个版本目录不可覆盖。贡献代码按本项目 [MIT 许可证](LICENSE) 发布，第三方代码保留原许可证。
