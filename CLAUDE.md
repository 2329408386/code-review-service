# XKL 代码检视服务 (code-review-service)

FastAPI 服务：接收 CodeHub MR webhook → 隔离 clone → 调用 Codex CLI 检视 diff → 将意见回写为 MR 评论并发表 MR 整体评价；自带 Web 控制台（`app/static/`）。

## 常用命令
- 启动（默认 8083 端口）：`./run.sh`（首次会自动创建 `.venv` 并安装依赖；`PORT=xxxx ./run.sh` 可改端口）
- 测试：`.venv/bin/python -m pytest -q`（当前 70 个用例全部通过，改动后必须保持全绿）
- 单个测试：`.venv/bin/python -m pytest -q tests/test_xxx.py -k name`
- 端到端冒烟（真实 Codex + 大模型网关）：`.venv/bin/python tests/e2e_smoke.py`（读取 `.env.example`，再用已 gitignore 的 `.env.local` 覆盖密钥）
- 健康检查：`curl -s localhost:8083/health`

## 配置
- 全部配置在 `app/config.py` 的 `Settings`（pydantic-settings），从环境变量 / `.env` 读取，大小写不敏感。
- 大模型默认网关：`OPENAI_API_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`。模型路由（控制台可改）可为每条路由单独配置网关，存于 SQLite `runtime_settings`。
- 模板见 `.env.example`；`.env`、`.env.local` 含密钥，已被 gitignore，**不要把密钥写进 `.env.example`**。
- 运行时数据：`data/`（`reviews.db` SQLite、`repos/`、`worktrees/`、`codex-logs/`），日志：`log/`。均已 gitignore。

## 代码结构（app/）
- `main.py` — `create_app()`：组装运行时、请求日志中间件、路由与静态资源；模块级 `app = create_app()`
- `config.py` / `logging_config.py` / `models.py`（Webhook 载荷、API 入参、领域记录）/ `storage.py`（SQLite `Database`）
- `runtime.py` — `ReviewRuntime`：组装组件，检视工作线程、延后任务调度线程、启动恢复
- `api/` — 路由：`auth`、`webhooks`、`reviews`（列表/详情/事件/SSE/导出/管理员操作）、`settings`（模型路由、检视范围）、`stats`；`deps.py` 提供 `Runtime`、`AdminOnly`
- `review/` — 检视领域：`service.py`（单次 MR 主流程）、`diff.py`（解析/过滤/分片）、`findings.py`（定位校验/去重/复核合并）、`prompts.py`、`scope.py`（检视范围与跳过规则）、`git.py`（镜像/clone/diff）、`codegraph.py`（可选索引，失败只降级）
- `llm/` — `registry.py`（模型路由与网关）、`codex.py`（Codex CLI 调用与重试）、`output.py`（输出 JSON 与事件流解析）、`chat.py`（OpenAI 兼容 /models、/chat/completions）
- `codehub/` — `client.py`（CodeHub OpenAPI）、`publisher.py`（单线程限流回写评论与评价）、`evaluation.py`（MR 评分与评价文案）、`replies.py`（回复分类与自动解决）

## 约定
- 测试用 `tests/conftest.py` 的 `settings`/`database` fixture（tmp_path 隔离，`_env_file=None`）；公共构造工具在 `tests/helpers.py`（含伪造的 codex CLI）。
- 前端测试（`tests/test_frontend.py`）校验 `app.js` 引用的元素 id 都存在于 `index.html`，并对关键接口做字符串断言，改前端时同步更新。
- 用户可见文案、日志说明与代码注释以中文为主。
