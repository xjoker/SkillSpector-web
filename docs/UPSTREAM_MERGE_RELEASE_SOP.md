# 上游合并与发版 SOP

本文档描述本 fork 在 NVIDIA 上游 `upstream/main` 更新后，如何合并、验证、构建镜像并发布。

适用镜像：

```text
ghcr.io/xjoker/skillspector-adapter
```

## 硬性规则

- 发版版本号使用 `YYYYMMDD.N`，修改 `VERSION` 前必须先取本机日期。
- `VERSION` 是镜像版本 tag 的单一真相源。
- `docker push` 成功不代表远端容器已更新，必须以远端 `/health` 为准。
- 发布前必须跑代码审查和安全审查；Critical/High 必须修复后再发布。
- 绑定非 localhost 时必须配置鉴权，并优先通过 TLS 反向代理暴露。
- 不用 `git reset --hard`、`git checkout --` 等破坏性命令清理用户改动。
- 不要把真实 token、密码、生产 `.env` 写入 commit、日志、PR 或 Yuki。

## 角色分工

| 阶段 | 负责人 | 产物 |
| --- | --- | --- |
| 上游合并 | 开发者/AI | 合并 commit 或 PR |
| 回归验证 | 开发者/AI | 测试、lint、mypy、Docker smoke 结果 |
| 安全审查 | security-reviewer | SAST finding 列表或通过结论 |
| 发版 | 发布负责人 | commit、镜像 tag、推送记录 |
| 上线复测 | 发布负责人 | 远端 `/health` 和原问题复测证据 |

## 1. 合并前检查

先确认本机时间、当前分支、工作区和远端配置：

```bash
date "+%Y-%m-%d %H:%M:%S %Z"
git status --short --branch
git remote -v
```

要求：

- `upstream` 指向 `https://github.com/NVIDIA/skillspector.git`。
- `origin` 指向 fork 仓库。
- 工作区已有改动必须先确认来源；不要覆盖用户改动。
- 若已有未完成实现，先提交到当前工作分支，或创建临时安全分支。

拉取上游信息：

```bash
git fetch upstream
git fetch origin
git log --oneline --decorate --left-right --graph HEAD...upstream/main
git diff --stat HEAD..upstream/main
```

如果上游变更触及这些区域，合并后必须重点复测：

| 区域 | 复测重点 |
| --- | --- |
| `src/skillspector/input_handler.py` | URL/zip/路径输入、安全边界、SSRF 用例 |
| `src/skillspector/mcp_server.py` | 上游 MCP 行为；本 fork 原则上不改此文件 |
| `src/skillspector/web.py` | Web/API 鉴权、ticket、扫描、报告接口 |
| `src/skillspector/upload_mcp_server.py` | 远程 MCP upload ticket 流程 |
| `pyproject.toml` / `uv.lock` | 依赖、extras、生产镜像依赖体积 |
| `Dockerfile` / `docker/entrypoint.sh` | CLI、Web、upload-mcp 入口 |

## 2. 合并上游

建议在独立分支上合并：

```bash
git switch main
git pull --ff-only origin main
git switch -c merge/upstream-$(date +%Y%m%d)
git merge --no-ff upstream/main
```

冲突处理原则：

- 优先保留 NVIDIA 上游对原有扫描器、规则、CLI、上游 MCP 的更新。
- 本 fork 的远程入口保持附加层：`web.py`、`upload_mcp_server.py`、Docker/compose/docs。
- 除非上游文件签名或公共接口改变，不要主动改 `src/skillspector/mcp_server.py`。
- 如果上游修改了输入处理或扫描报告结构，先修适配层测试，再改实现。

冲突解决后检查：

```bash
git status --short
git diff --stat upstream/main...HEAD
git diff -- src/skillspector/mcp_server.py
```

若 `src/skillspector/mcp_server.py` 出现 fork 专属改动，必须说明原因；否则应只保留上游版本。

## 3. 合并后本地验证

安装或刷新依赖：

```bash
uv sync --all-extras
```

基础验证：

```bash
uv run --python 3.12 --extra dev ruff check src tests
uv run --python 3.12 --extra dev mypy src/skillspector/web.py src/skillspector/upload_mcp_server.py
uv run --python 3.12 --extra dev pytest -q
```

Adapter 重点验证：

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/test_web.py \
  tests/unit/test_upload_mcp_server.py \
  tests/unit/test_mcp_server.py \
  tests/unit/test_input_handler.py \
  tests/unit/test_input_handler_ssrf.py \
  -q
```

Compose 渲染验证：

```bash
SKILLSPECTOR_ENV_FILE=.env.adapter.example \
  docker compose --env-file .env.adapter.example config

SKILLSPECTOR_ENV_FILE=.env.adapter.example SKILLSPECTOR_LAN_BIND_IP=10.0.4.43 \
  docker compose --env-file .env.adapter.example \
  -f docker-compose.yml -f docker-compose.lan.yml config
```

检查点：

- 默认 compose 只绑定 `127.0.0.1:18477:8477`。
- LAN compose 只有显式设置 `SKILLSPECTOR_LAN_BIND_IP` 后才出现 `10.0.4.43`。
- `.env.adapter.example` 不包含真实密钥。

## 4. 发布前审查

发布前必须完成两类审查：

```text
code-reviewer: 正确性、边界条件、测试覆盖、容器入口、compose 可用性
security-reviewer: 鉴权、授权、输入处理、SSRF、路径遍历、zip 解压、密钥泄露、容器暴露面
```

处理规则：

- `[CRITICAL]` / `[HIGH]`：必须修复并复测。
- `[MEDIUM]`：默认本轮修复；若延后，必须记录原因和后续任务。
- `[LOW]`：可后续 hardening，但不能掩盖已知风险。

安全审查至少要覆盖：

- `/api/tickets` 不信任客户端 `X-Forwarded-*`。
- `SKILLSPECTOR_PUBLIC_URL` 只在显式配置时用于 ticket URL。
- 未鉴权 API 请求返回 `401`。
- 上传数据面绑定非 localhost 时必须配置 Web/API auth。
- 非 localhost 暴露文档要求 TLS 反向代理。

## 5. Bump 版本

先取本机日期：

```bash
date "+%Y-%m-%d %H:%M:%S %Z"
date "+%Y%m%d"
```

版本规则：

- 当天首次发版：`YYYYMMDD.1`
- 当天后续发版：`YYYYMMDD.N+1`
- 跨天后重新从 `.1` 开始
- 不允许沿用上一版日期机械递增

写入 `VERSION`：

```bash
printf '%s\n' 20260709.1 > VERSION
```

同步引用该版本的文件：

- `docker-compose.yml`
- `docs/ADAPTER_DEPLOYMENT.md`
- 需要展示当前镜像 tag 的 README 或 release notes

版本改完后再跑一次：

```bash
git diff -- VERSION docker-compose.yml docs/ADAPTER_DEPLOYMENT.md
```

## 6. 本地镜像冒烟

先确认工作区状态。如果还未 commit，构建出的 `/health.git_commit` 会带 `-dirty`，只能用于本地测试：

```bash
git status --short --branch
```

本地构建只用于调试和 smoke，不作为正式发布推送入口：

```bash
make docker-release-build
make docker-smoke
```

该命令会构建 `linux/amd64`，并同时打 tag：

```text
ghcr.io/xjoker/skillspector-adapter:<VERSION>
ghcr.io/xjoker/skillspector-adapter:dev
ghcr.io/xjoker/skillspector-adapter:latest
skillspector
```

确认 tag 指向同一个 image ID：

```bash
docker image ls ghcr.io/xjoker/skillspector-adapter --format '{{.Repository}}:{{.Tag}}\t{{.ID}}' \
  | sort
```

`make docker-smoke` 必须覆盖：

- CLI `--version`
- `git --version`
- `skillspector-web --help`
- `skillspector-upload-mcp --help`
- 本地 fixture 静态扫描
- Web `/health`
- Bearer API 鉴权
- ticket 创建、PUT 上传、扫描、报告读取

## 7. Commit 与线上构建

发布镜像前必须先 commit 并推送，让 GitHub Actions 构建干净 commit：

```bash
git status --short --branch
git add <changed-files>
git commit -m "release: upstream merge and adapter image YYYYMMDD.N"
git push origin main
```

`CI` workflow 成功后会自动触发 `Container` workflow。`Container` workflow 会：

- 使用 `GITHUB_TOKEN` 推送 GHCR 镜像。
- 推送 `ghcr.io/xjoker/skillspector-adapter:<VERSION>`、`:dev`、`:latest`。
- 使用 `ghcr.io/xjoker/skillspector-adapter:buildcache` 作为 buildx registry cache。
- 拉取刚推送的镜像并检查 `/health`。

要求：

- `release_version` 等于 `cat VERSION`
- `git_commit` 等于当前 commit 短 hash，且不带 `-dirty`
- `schema_version` 等于本次构建传入值，默认 `none`

## 8. GitHub Actions 注意事项

- 不要假设 `GITHUB_TOKEN` 创建的 tag 会触发另一个 `on: push: tags` workflow。
- 需要链式触发时，用 `gh workflow run <workflow>` 显式触发。
- 本 fork 的正式镜像发布入口是 `Container` workflow，不是本机 `docker push`。

## 9. 远端上线

在部署机拉取并重启：

```bash
docker compose pull
docker compose up -d --force-recreate
```

如果需要 LAN 绑定：

```bash
export SKILLSPECTOR_LAN_BIND_IP=10.0.4.43
docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d --force-recreate
```

数据安全检查：

- 确认 `.env` 在部署机本地，不进入镜像和 git。
- 确认没有把持久化数据放在会被容器替换删除的位置。
- 本服务当前报告历史为进程内状态，重启会丢失历史；若未来接入持久化存储，必须先确认外部 volume/数据库备份。

## 10. 上线后复测

先确认远端容器确实更新：

```bash
curl -sS http://127.0.0.1:18477/health
```

必须核对：

- `release_version` 等于新版本。
- `git_commit` 等于发布 commit。
- `schema_version` 等于本次构建值。

鉴权复测：

```bash
curl -i http://127.0.0.1:18477/api/history
curl -sS -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  http://127.0.0.1:18477/api/history
```

要求：

- 未鉴权返回 `401`。
- 带 Bearer 返回 `200`。

跑一次最小 ticket 扫描链路：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"filename":"skill.zip","max_bytes":52428800}' \
  http://127.0.0.1:18477/api/tickets
```

然后按返回的 `upload_url` 和 `headers.Authorization` 执行 `PUT`，再调用：

```bash
curl -sS \
  -H "Authorization: Bearer $SKILLSPECTOR_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"use_llm":false}' \
  http://127.0.0.1:18477/api/scans/<upload_id>
```

最后复测本次上游合并或 fork 修复针对的原问题。只确认镜像推送成功不算完成。

## 11. 收尾记录

发布完成后记录：

| 字段 | 示例 |
| --- | --- |
| 版本 | `20260709.1` |
| commit | `47afd41` |
| image | `ghcr.io/xjoker/skillspector-adapter:20260709.1` |
| image id/digest | `sha256:...` |
| 测试 | `pytest -q`、`ruff`、`mypy`、`make docker-smoke` |
| 审查 | code-reviewer / security-reviewer 结论 |
| 远端 `/health` | `release_version` / `git_commit` / `schema_version` |
| 原问题复测 | 具体命令和结果 |

如果有延后的 Medium/Low hardening，创建后续任务，不要只写在聊天里。
