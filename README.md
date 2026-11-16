# uni-api

自托管的 uni-api 网页管理与请求审计控制台。为 uni-api 提供一个中文 Web 界面，用于查看请求/响应记录、管理上游渠道、模型授权池和客户端 API Key。项目包含 Docker Compose 示例，适合个人或小团队在自己的服务器上运行。

> 本项目是 uni-api 的独立管理/审计 sidecar，不是 uni-api 官方项目，也不取代其原生路由、负载均衡或重试逻辑。请求仍由 uni-api 原生服务处理。

## 功能

- 中文响应式网页：仪表盘、API 密钥、使用记录、上游接入。
- 本地 SQLite 请求审计：可查询请求/响应上下文、状态、模型及用量；支持保留期管理。
- 上游 Provider 与模型管理，按需发现模型。
- 模型授权池与客户端 Key 管理；池变更同步到绑定 Key。
- 管理变更回滚；本地健康检查。
- 无第三方前端字体或运行时 UI 依赖。

## 快速开始（Docker Compose）

要求：Docker Engine、Docker Compose v2、可访问 uni-api 容器镜像的网络，以及兼容的 uni-api 版本。第一次启动会构建控制台镜像，并启动 uni-api 与控制台。

```sh
git clone https://github.com/key-zxy/uni-api-console.git
cd uni-api-console/deploy
cp .env.example .env
```

编辑 `deploy/.env`，设置两个**不同的强随机秘密**：

- `AUDIT_ADMIN_TOKEN`：登录网页用。
- `AUDIT_MANAGEMENT_KEY`：uni-api 原生管理密钥；初始化和管理配置时使用。不要与网页密码复用。

可用下列命令生成随机值，并分别填入 `.env`：

```sh
openssl rand -hex 32
```

初始化目录并启动：

```sh
mkdir -p audit-data uni-api/config uni-api/data
sudo chown -R 1000:1000 audit-data uni-api
chmod 700 .env audit-data uni-api/config uni-api/data
docker compose up -d --build
docker compose ps
```

等待容器健康后，在运行 Docker 的主机打开 `http://127.0.0.1:8787`，用 `AUDIT_ADMIN_TOKEN` 登录。首次启动时按网页提示配置至少一个上游 Provider 和模型。启动配置中的原生 admin Key 必须与 `AUDIT_MANAGEMENT_KEY` 一致。配置文件会写入 `deploy/uni-api/config/api.yaml`。

### 已有 uni-api 实例

若你已有 uni-api，推荐只运行控制台服务，并将它接入现有 Docker 网络：在 Compose 文件中将 `AUDIT_UPSTREAM` 改为该实例在网络内可访问的 URL（例如 `http://uni-api:8000`），确保控制台容器加入该网络，并提供可访问原生管理接口的 `AUDIT_MANAGEMENT_KEY`。不要同时启动 Compose 中的 `uni-api` 服务，避免意外创建第二个实例。已有实例的网络/权限配置因环境而异，请先备份并核对。

## 配置

配置文件 `deploy/.env` 不应提交到 Git。模板在 `deploy/.env.example`。

| 变量 | 默认值 / 说明 |
| --- | --- |
| `AUDIT_ADMIN_TOKEN` | 必填。控制台登录密码/令牌。 |
| `AUDIT_MANAGEMENT_KEY` | 原生 uni-api 管理 Key；需与原生配置中的 admin Key 匹配。 |
| `AUDIT_RETENTION_DAYS` | 审计保留天数，默认 `7`。 |
| `AUDIT_BIND_ADDRESS` | 主机发布地址，默认 `127.0.0.1`，仅本机可访问。 |
| `AUDIT_PORT_PUBLISHED` | 主机端口，默认 `8787`。 |
| `UNI_API_IMAGE` | 可选。覆盖 Compose 示例中的 uni-api 镜像与版本。升级前阅读对应版本变更说明。 |

其他运行时变量（例如 `AUDIT_REQUEST_CAP`、`AUDIT_RESPONSE_CAP`、`AUDIT_DB`）见 `audit_console.py` 的 `Settings.from_env`。修改绑定地址可能将服务暴露给局域网或公网；默认使用 loopback。不要直接公开 HTTP 端口。跨设备访问请使用带 TLS 的受信任反向代理/安全隧道及防火墙规则，并理解请求体审计会保存敏感内容。

## 使用说明

1. **登录**：访问控制台地址，输入 `AUDIT_ADMIN_TOKEN`。登录状态保存在当前浏览器；不要在共用设备上保持登录。
2. **接入上游**：进入“上游接入”，新增 Provider，填写 API 地址、上游 Key 和模型；可以按需请求模型目录。提交上游地址会使服务端连接该地址，请只配置可信端点。
3. **授权模型**：进入“API 密钥”，创建模型池并选择该池允许使用的 Provider/模型，再为客户端签发独立 API Key。客户端下载兼容 API 地址与 Key。
4. **查看记录**：进入“使用记录”筛选请求，选择记录可查看捕获的上下文。审计记录含请求与响应正文，只有可信管理员应访问。
5. **运维**：仪表盘查看状态、用量、保留期；上游管理页可刷新配置或回滚最近一次受支持的管理变更。

API 客户端使用控制台主机地址作为 OpenAI 兼容 Base URL（例如 `http://127.0.0.1:8787/v1`），并通过 `Authorization: Bearer YOUR_CLIENT_KEY` 认证。跨设备客户端需使用其可达的服务器主机名/IP 与安全传输方案，而不是 `127.0.0.1`。

## 更新、备份与卸载

```sh
cd deploy
docker compose pull uni-api
docker compose up -d --build
```

升级前备份 `deploy/uni-api/config/`、`deploy/uni-api/data/`、`deploy/audit-data/` 和私有 `.env`。配置、管理版本快照及审计请求正文可能包含凭据、提示词和个人数据；妥善限制备份访问，不要上传公开云盘或代码仓库。

停止服务：

```sh
cd deploy
docker compose down
```

这不会删除持久化数据。确认不再需要后，再由操作者自行删除数据目录。

## 安全与限制

- 默认只绑定主机 loopback。不要无 TLS 地将 Web 管理端口暴露到互联网。
- 审计记录有意捕获请求/响应正文，**不等同于脱敏**，可能包含 API Key、个人数据或机密提示词。使用前评估数据保护义务并设置合适的保留期。
- 管理凭据会被转发到 uni-api 原生管理接口。管理数据库/版本快照需限制文件权限；配置管理适合单一写入者，不要让多个控制台或手工进程并发改写原生配置。
- 网页登录是共享的管理员令牌，不是多用户身份系统，没有细粒度角色或审计员权限隔离。
- 控制台不实现模型路由、聚合、负载均衡或自动重试；这些由 uni-api 负责。
- 仅 HTTP/HTTPS API 代理；WebSocket/升级协议不在支持范围内。模型目录展示不代表上游账户有权调用或模型免费。
- 本项目未声称适用于所有 uni-api 版本。部署前请在非生产环境验证当前版本的原生管理 API 与数据兼容性。

## 开发与测试

Python 3.11+、Node.js 18+：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
npm ci
node --check app.js
npm run test:ui
python -m unittest discover -s tests -p 'test_*.py' -v
```

Windows PowerShell 可使用 `.venv\\Scripts\\Activate.ps1`。部分集成测试需要本机允许 loopback socket；被运行环境限制时应明确识别为环境阻塞，不要将其报成通过。

## 项目布局

- `audit_console.py`：aiohttp 代理、审计 API 与 Web 服务。
- `management.py`、`model_discovery.py`、`quotas.py`：原生管理适配、模型发现与配额视图。
- `app.js`、`console.css`、`index.html`：本地 Web UI 与品牌资源。
- `deploy/`：Compose 示例、环境模板和启动辅助脚本。
- `tests/`：后端与前端契约测试。

## 许可

本项目以 MIT License 发布。再分发软件或其较大部分时，请保留 `LICENSE` 中的版权与许可声明。
