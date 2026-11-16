# Docker Compose 示例

本目录包含 uni-api 与控制台的参考 Compose 配置。它是起步模板，不会自动适配现有 Docker 网络或主机策略。

## 初始化

```sh
cp .env.example .env
# 编辑 .env，设置两个互不相同的随机值：
# AUDIT_ADMIN_TOKEN（网页登录）
# AUDIT_MANAGEMENT_KEY（uni-api 原生管理员凭据）
mkdir -p audit-data uni-api/config uni-api/data
sudo chown -R 1000:1000 audit-data uni-api
chmod 700 .env audit-data uni-api/config uni-api/data
docker compose up -d --build
docker compose ps
```

容器健康后，访问 `http://127.0.0.1:8787`。新安装可从控制台初始化上游和模型配置；写入的原生 admin Key 必须与 `.env` 的 `AUDIT_MANAGEMENT_KEY` 一致。

默认端口只发布到 loopback。若需更改绑定地址或端口，在 `.env` 设置 `AUDIT_BIND_ADDRESS` / `AUDIT_PORT_PUBLISHED`。请勿将无 TLS 的管理页面直接暴露到不可信网络；注意配置及请求正文可能包含敏感信息。

## 数据与升级

- `uni-api/config/`：原生配置，含上游凭据。
- `uni-api/data/`：uni-api 运行数据。
- `audit-data/`：审计数据库与管理版本数据。
- `.env`：管理凭据，不应提交或共享。

先停止并备份以上数据，再更新镜像/源码。升级控制台：`docker compose up -d --build audit-console`；更新 uni-api 镜像时先设置 `UNI_API_IMAGE`，确认兼容后再执行 `docker compose pull uni-api && docker compose up -d uni-api`。执行 `docker compose down` 不会删除这些 bind mount 数据。

## 现有 uni-api

如要将控制台连到已有实例，请先确认它的管理 API、网络和密钥兼容性。修改 Compose 中 `AUDIT_UPSTREAM`，将控制台加入已有网络，并只启动 `audit-console`（必要时用 `docker compose up -d --no-deps audit-console`）。不要同时启动此模板的 `uni-api` 服务。不要把管理凭据写入源码、命令历史或公开 issue。

## 安全说明

控制台审计请求和响应正文，可能保存 API Key、提示词及个人数据；管理版本快照可能包含原生配置凭据。限制主机和备份访问，设定合理保留期限，不要将数据库挂载目录放在公开同步位置。反向代理需正确配置 TLS 与访问控制。