# SkillSpector adapter 部署说明

本文档描述本 fork 的容器镜像、`docker-compose.yml`、环境变量和上线测试步骤。

当前测试镜像：

```text
ghcr.io/xjoker/skillspector-adapter:20260709.1
```

## 构建镜像

本仓库根目录的 `VERSION` 是镜像 tag 的单一真相源。

```bash
cat VERSION
make docker-release-build
make docker-smoke
```

构建会同时打以下本地 tag：

```text
ghcr.io/xjoker/skillspector-adapter:20260709.1
ghcr.io/xjoker/skillspector-adapter:dev
ghcr.io/xjoker/skillspector-adapter:latest
skillspector
```

`make docker-release-build` 使用 `linux/amd64` 和 `--no-cache`，并把以下信息注入 `/health`：

- `release_version`: `VERSION` 文件内容
- `git_commit`: 当前 commit，dirty tree 会带 `-dirty`
- `schema_version`: `SCHEMA_VERSION` make 变量，默认 `none`

推送镜像前必须显式获得确认：

```bash
docker push ghcr.io/xjoker/skillspector-adapter:20260709.1
docker push ghcr.io/xjoker/skillspector-adapter:dev
docker push ghcr.io/xjoker/skillspector-adapter:latest
```

## Web/API compose

仓库根目录的 `docker-compose.yml` 按 Web/API 单端口模式配置，默认只绑定本机
`127.0.0.1:18477`，便于本地和 CI 直接启动：

```yaml
services:
  skillspector-adapter:
    image: ghcr.io/xjoker/skillspector-adapter:20260709.1
    container_name: skillspector-adapter
    restart: unless-stopped
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    env_file:
      - ${SKILLSPECTOR_ENV_FILE:-.env}
    command: ["web", "--port", "8477", "--max-upload-mb", "${SKILLSPECTOR_WEB_MAX_UPLOAD_MB:-50}"]
    ports:
      - "127.0.0.1:18477:8477"
```

启动：

```bash
cp .env.adapter.example .env
# edit .env and set SKILLSPECTOR_AUTH_TOKEN
docker compose up -d
```

按当前上线测试主机同时绑定 `10.0.4.43:18477` 时必须显式设置绑定 IP：

```bash
export SKILLSPECTOR_LAN_BIND_IP=10.0.4.43
docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d
```

也可以在部署机 `.env` 中显式配置：

```bash
SKILLSPECTOR_LAN_BIND_IP=10.0.4.43
```

本地只检查 compose 渲染时可以直接使用示例 env：

```bash
SKILLSPECTOR_ENV_FILE=.env.adapter.example docker compose --env-file .env.adapter.example config
SKILLSPECTOR_ENV_FILE=.env.adapter.example SKILLSPECTOR_LAN_BIND_IP=10.0.4.43 \
  docker compose --env-file .env.adapter.example -f docker-compose.yml -f docker-compose.lan.yml config
```

验证：

```bash
curl http://127.0.0.1:18477/health
curl -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  http://127.0.0.1:18477/api/history
```

未带鉴权访问 API 应返回 `401`：

```bash
curl -i http://127.0.0.1:18477/api/history
```

浏览器 UI 在 Bearer-only 配置下会显示访问令牌输入框，并把该 token 附加到同源
API 请求；如果配置 Basic auth，浏览器也可以使用标准 Basic 认证弹窗。

非 localhost 暴露时不要直接在不可信网络上使用明文 HTTP。推荐只让 compose
绑定 `127.0.0.1`，再由带 TLS 的反向代理对外提供 `https://...`。需要让 ticket
返回外部 URL 时，设置：

```bash
SKILLSPECTOR_PUBLIC_URL=https://skillspector.example.com
```

## HTTP API 扫描流程

创建上传 ticket：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"filename":"skill.zip","max_bytes":52428800}' \
  http://127.0.0.1:18477/api/tickets
```

上传文件：

```bash
curl -X PUT \
  -H "Authorization: Bearer <ticket-token>" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @skill.zip \
  http://127.0.0.1:18477/api/uploads/<upload_id>
```

扫描：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"use_llm":false}' \
  http://127.0.0.1:18477/api/scans/<upload_id>
```

获取报告：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  http://127.0.0.1:18477/api/reports/<report_id>
```

## 环境变量

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SKILLSPECTOR_AUTH_TOKEN` | 远程 Web/API 必需 | 空 | Web/API Bearer token。绑定到非 localhost 时必须配置。 |
| `SKILLSPECTOR_API_USERNAME` | 可选 | 空 | Basic auth 用户名。需要与 `SKILLSPECTOR_API_PASSWORD` 同时设置。 |
| `SKILLSPECTOR_API_PASSWORD` | 可选 | 空 | Basic auth 密码。 |
| `SKILLSPECTOR_WEB_MAX_UPLOAD_MB` | 可选 | `50` | Web/API 上传大小限制，单位 MiB。 |
| `SKILLSPECTOR_PUBLIC_URL` | 反代/公网部署推荐 | 空 | 创建 upload ticket 时使用的公开 base URL。非 localhost 建议使用 `https://...`。 |
| `SKILLSPECTOR_LAN_BIND_IP` | LAN override 必需 | 空 | `docker-compose.lan.yml` 使用的宿主机绑定 IP，必须显式设置。 |
| `SKILLSPECTOR_PROVIDER` | 可选 | `nv_build` | LLM provider。静态扫描不需要 provider key。 |
| `NVIDIA_INFERENCE_KEY` | LLM 时按 provider 必需 | 空 | `SKILLSPECTOR_PROVIDER=nv_build` 时使用。 |
| `OPENAI_API_KEY` | LLM 时按 provider 必需 | 空 | `SKILLSPECTOR_PROVIDER=openai` 时使用。 |
| `OPENAI_BASE_URL` | 可选 | 空 | OpenAI-compatible endpoint。 |
| `ANTHROPIC_API_KEY` | LLM 时按 provider 必需 | 空 | `SKILLSPECTOR_PROVIDER=anthropic` 时使用。 |
| `ANTHROPIC_PROXY_ENDPOINT_URL` | 代理 provider 必需 | 空 | Anthropic proxy endpoint。 |
| `ANTHROPIC_PROXY_API_KEY` | 代理 provider 必需 | 空 | Anthropic proxy Bearer token。 |
| `SKILLSPECTOR_RELEASE_VERSION` | 构建注入 | `dev` | `/health.release_version`。不要在部署 `.env` 里覆盖，除非明确调试。 |
| `SKILLSPECTOR_GIT_COMMIT` | 构建注入 | `unknown` | `/health.git_commit`。 |
| `SKILLSPECTOR_SCHEMA_VERSION` | 构建注入 | `none` | `/health.schema_version`。 |

## 远程 MCP compose 变体

远程 MCP 不是单端口服务。它至少需要：

- MCP 控制面端口，例如 `8001`
- 上传数据面端口，例如 `8477`

示例：

```yaml
services:
  skillspector-adapter:
    image: ghcr.io/xjoker/skillspector-adapter:20260709.1
    container_name: skillspector-adapter
    restart: unless-stopped
    env_file: [.env]
    command: ["upload-mcp", "--port", "8001"]
    environment:
      SKILLSPECTOR_MCP_UPLOAD_HOST: "0.0.0.0"
      SKILLSPECTOR_MCP_UPLOAD_PORT: "8477"
      SKILLSPECTOR_MCP_UPLOAD_PUBLIC_URL: "https://skillspector.example.com"
    ports:
      - "10.0.4.43:18001:8001"
      - "10.0.4.43:18477:8477"
```

MCP 控制面鉴权：

```http
Authorization: Bearer $SKILLSPECTOR_MCP_AUTH_TOKEN
```

如果 `SKILLSPECTOR_MCP_AUTH_TOKEN` 为空，会回退到 `SKILLSPECTOR_AUTH_TOKEN`。

上传数据面绑定非 localhost 时仍必须配置 Web/API auth，并应放在 TLS 反向代理后面。
只配置 `SKILLSPECTOR_MCP_AUTH_TOKEN` 不允许暴露上传数据端口。

AI 客户端接入 MCP 控制面时使用 Streamable HTTP：

```text
http://10.0.4.43:18001/mcp
Authorization: Bearer <SKILLSPECTOR_MCP_AUTH_TOKEN 或 SKILLSPECTOR_AUTH_TOKEN>
```

通用 MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "skillspector-upload": {
      "type": "http",
      "url": "http://10.0.4.43:18001/mcp",
      "headers": {
        "Authorization": "Bearer ${SKILLSPECTOR_MCP_AUTH_TOKEN}"
      }
    }
  }
}
```

不同 AI 客户端的配置字段名可能不同，但必须保持：Streamable HTTP transport、URL 指向 `/mcp`、请求带 Bearer token。上传数据面端口 `18477` 只用于 ticket 上传文件字节，不是 MCP 控制面 URL。

## 上线测试检查

1. `docker compose pull` 或部署新镜像。
2. `docker compose up -d --force-recreate`。
3. 检查健康：

```bash
curl http://127.0.0.1:18477/health
```

确认：

- `release_version` 等于 `20260709.1`
- `git_commit` 等于本次构建 commit；未提交构建会带 `-dirty`
- `schema_version` 等于构建时传入的 `SCHEMA_VERSION`

4. 未鉴权 API 请求返回 `401`。
5. 带 Bearer 的 `/api/history` 返回 `200`。
6. 跑一次 ticket 上传和静态扫描。

注意：`docker push` 成功不代表远端容器已经更新。必须以远端 `/health` 为准。
