# SkillSpector fork 使用说明

本 fork 保留 NVIDIA 上游扫描器能力，并额外提供适合远程部署的上传式 Web/API 与 MCP 接入层。

Compose、镜像 tag 和完整环境变量说明见 [docs/ADAPTER_DEPLOYMENT.md](docs/ADAPTER_DEPLOYMENT.md)。
上游合并后的验证、构建和发版流程见 [docs/UPSTREAM_MERGE_RELEASE_SOP.md](docs/UPSTREAM_MERGE_RELEASE_SOP.md)。

关键约束：

- 不要把上游 `skillspector mcp` 作为公网入口；它是路径扫描模型，适合本机 stdio/loopback。
- 远程 MCP 使用本 fork 的 `skillspector-upload-mcp`，只暴露 upload-ticket 工具，不暴露上游 `scan_skill(target)`。
- 绑定到 `0.0.0.0` 前必须配置鉴权；未配置时服务会拒绝启动。
- 上传文件先进入临时目录，扫描完成后删除原始上传文件，报告保留在进程内历史中。

## 快速开始

构建镜像：

```bash
make docker-build
```

运行 Web/API：

```bash
docker run --rm \
  -p 8765:8765 \
  -e SKILLSPECTOR_AUTH_TOKEN='replace-with-a-long-random-token' \
  skillspector web --port 8765
```

验证：

```bash
curl http://127.0.0.1:8765/health
```

返回中应包含：

```json
{
  "ok": true,
  "service": "skillspector",
  "version": "2.3.11",
  "git_commit": "47afd41",
  "schema_version": "none"
}
```

## 远程 MCP 部署

远程 MCP 分两条 HTTP 边界：

- MCP 控制面：AI 客户端连接，默认端口 `8001`。
- 上传数据面：AI 客户端拿到 ticket 后 `PUT` 文件字节，示例端口 `8765`。

```bash
docker run --rm \
  -p 8001:8001 \
  -p 8765:8765 \
  -e SKILLSPECTOR_AUTH_TOKEN='replace-with-a-long-random-token' \
  -e SKILLSPECTOR_MCP_UPLOAD_HOST=0.0.0.0 \
  -e SKILLSPECTOR_MCP_UPLOAD_PORT=8765 \
  -e SKILLSPECTOR_MCP_UPLOAD_PUBLIC_URL='http://127.0.0.1:8765' \
  skillspector upload-mcp --port 8001
```

MCP 控制面鉴权使用：

```http
Authorization: Bearer $SKILLSPECTOR_MCP_AUTH_TOKEN
```

如果 `SKILLSPECTOR_MCP_AUTH_TOKEN` 为空，会回退到 `SKILLSPECTOR_AUTH_TOKEN`。

上传数据面使用 Web/API 鉴权。只配置 `SKILLSPECTOR_MCP_AUTH_TOKEN` 不足以暴露 `SKILLSPECTOR_MCP_UPLOAD_HOST=0.0.0.0`；必须同时配置 `SKILLSPECTOR_AUTH_TOKEN` 或 Basic auth。

### AI 客户端接入 MCP

AI 客户端连接的是 MCP 控制面，不是上传数据面。Streamable HTTP MCP URL 为：

```text
http://<host>:8001/mcp
```

请求头：

```http
Authorization: Bearer <SKILLSPECTOR_MCP_AUTH_TOKEN 或 SKILLSPECTOR_AUTH_TOKEN>
```

通用 MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "skillspector-upload": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": {
        "Authorization": "Bearer ${SKILLSPECTOR_MCP_AUTH_TOKEN}"
      }
    }
  }
}
```

如果客户端字段名不同，保持这三项不变即可：transport 使用 Streamable HTTP，URL 指向 `/mcp`，并带上 Bearer 鉴权头。AI 侧可用工具为 `skills_smoke`、`skills_create_upload_ticket`、`skills_scan_upload`、`skills_get_report`；文件字节通过 ticket 返回的上传 URL 发送，不放进 MCP 参数。

## HTTP API 流程

创建上传 ticket：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"filename":"skill.zip","max_bytes":52428800}' \
  http://127.0.0.1:8765/api/tickets
```

上传文件：

```bash
curl -X PUT \
  -H "Authorization: Bearer <ticket-token>" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @skill.zip \
  http://127.0.0.1:8765/api/uploads/<upload_id>
```

扫描：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"use_llm":false}' \
  http://127.0.0.1:8765/api/scans/<upload_id>
```

获取报告：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  http://127.0.0.1:8765/api/reports/<report_id>
```

默认返回 compact report。需要原始报告时追加：

```bash
?include_raw=true
```

## 配置

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SKILLSPECTOR_AUTH_TOKEN` | 远程 Web/API 必需 | 空 | Web/API Bearer token；也可作为 MCP token 回退值。 |
| `SKILLSPECTOR_API_USERNAME` / `SKILLSPECTOR_API_PASSWORD` | 可选 | 空 | Web/API Basic auth。 |
| `SKILLSPECTOR_MCP_AUTH_TOKEN` | 远程 MCP 建议 | 回退到 `SKILLSPECTOR_AUTH_TOKEN` | MCP 控制面 Bearer token。 |
| `SKILLSPECTOR_MCP_UPLOAD_HOST` | 远程 MCP 数据面必配 | `127.0.0.1` | 上传数据面监听地址。 |
| `SKILLSPECTOR_MCP_UPLOAD_PORT` | 远程 MCP 数据面必配 | `0` | 上传数据面监听端口；`0` 表示随机端口。 |
| `SKILLSPECTOR_MCP_UPLOAD_PUBLIC_URL` | 反代/容器映射时必配 | 自动推导 | 返回给客户端的上传数据面公网 URL。 |
| `SKILLSPECTOR_MCP_PUBLIC_URL` | 反代 MCP 时可选 | 自动推导 | MCP auth resource URL。 |
| `SKILLSPECTOR_WEB_MAX_UPLOAD_MB` | 可选 | `50` | Web/API 上传大小限制。 |
| `SKILLSPECTOR_GIT_COMMIT` | 构建注入 | `unknown` | `/health` 发布验证字段。 |
| `SKILLSPECTOR_SCHEMA_VERSION` | 构建注入 | `none` | `/health` 部署/schema 标记。 |

LLM provider 变量沿用上游：

- `SKILLSPECTOR_PROVIDER`
- `NVIDIA_INFERENCE_KEY`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_PROXY_ENDPOINT_URL`
- `ANTHROPIC_PROXY_API_KEY`

## 容器验证

本地完整冒烟：

```bash
make docker-smoke
```

默认 smoke 不访问 GitHub，避免 CI 或内网环境 DNS/出网失败误报。需要显式测试远程 URL 时：

```bash
SKILLSPECTOR_DOCKER_REMOTE_SMOKE=1 make docker-smoke
```

发布后验证顺序：

1. 拉取或启动新镜像。
2. 请求 `/health`，确认 `git_commit` 是预期 commit。
3. 用无鉴权请求 `/api/history`，应返回 `401`。
4. 用 Bearer 或 Basic 请求 `/api/history`，应返回 `200`。
5. 创建 ticket、上传样本、触发扫描、读取报告。

## 与上游的差异

| 能力 | 上游 | 本 fork |
| --- | --- | --- |
| 本机 CLI 扫描 | 支持 | 保持支持 |
| 上游 path-based MCP | 支持 | 保留，仅建议本机使用 |
| 远程 upload-ticket MCP | 不提供 | 新增 `skillspector-upload-mcp` |
| Web/API Bearer/Basic 鉴权 | 不提供 | 新增 |
| 上传 ticket 单次使用 | 不提供 | 新增 |
| 容器 Web/MCP 入口 | CLI 为主 | 新增 `web` / `upload-mcp` 分派 |
| `/health` 发布元数据 | `ok` | 新增 version、git_commit、schema_version |

## 常见问题

### 远程绑定启动失败

如果看到要求设置 token 的错误，说明服务正在绑定非 localhost，但缺少对应鉴权。

Web/API：

```bash
export SKILLSPECTOR_AUTH_TOKEN='replace-with-a-long-random-token'
```

MCP 控制面：

```bash
export SKILLSPECTOR_MCP_AUTH_TOKEN='replace-with-a-long-random-token'
```

MCP 上传数据面如果绑定 `0.0.0.0`，仍然需要 Web/API auth。

### MCP 客户端拿到的 upload_url 不可访问

设置外部可访问 URL：

```bash
export SKILLSPECTOR_MCP_UPLOAD_PUBLIC_URL='https://skillspector-upload.example.com'
```

如果 MCP 控制面也在反向代理后面：

```bash
export SKILLSPECTOR_MCP_PUBLIC_URL='https://skillspector-mcp.example.com'
```

### Docker smoke 里的远程 GitHub 扫描被跳过

这是默认行为。默认 smoke 只验证离线可复现路径。需要外网 URL 验证时设置：

```bash
SKILLSPECTOR_DOCKER_REMOTE_SMOKE=1 make docker-smoke
```
