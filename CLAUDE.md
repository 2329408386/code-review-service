# XKL 代码检视服务 (code-review-service)

FastAPI 服务：接收 CodeHub MR webhook → 隔离 clone → 调用 Codex CLI 检视 diff → 将意见回写为 MR 评论并发表 MR 整体评价；自带 Web 控制台（`app/static/`）。

## 常用命令
- 启动（默认 8083 端口）：`./run.sh`（首次会自动创建 `.venv` 并安装依赖；`PORT=xxxx ./run.sh` 可改端口）
- 测试：`.venv/bin/python -m pytest -q`（当前 81 个用例全部通过，改动后必须保持全绿）
- 单个测试：`.venv/bin/python -m pytest -q tests/test_xxx.py -k name`
- 端到端冒烟（真实 Codex + 大模型网关）：`.venv/bin/python tests/e2e_smoke.py`（读取 `.env.example`，再用 `.env` 覆盖密钥）
- 健康检查：`curl -s localhost:8083/health`
- 本地提交自动检视：`scripts/install-git-hook.sh [仓库路径]` 安装 post-commit 钩子，每次提交调用 `POST /api/local-reviews`（仅本机或管理员），结果只在平台展示

## 配置
- `app/config.py` 的 `Settings` 只保留部署方必须提供的参数（网关、CodeHub 凭据、管理员等），从环境变量 / `.env` 读取。
- 超时、重试、并发、分片大小等调优参数是所在模块的大写常量（如 `review/service.py`、`llm/codex.py`、`codehub/publisher.py`），**不要再加回 Settings**；测试用 monkeypatch 修改。
- Codex 并发只由模型路由的“并发度”控制；未配置管理员时控制台只读。
- 模型故障切换（`llm/registry.py`）：Codex 进程级失败（`ModelUnavailable`）时当次调用自动换下一条路由；路由连续失败 `FAILURE_THRESHOLD` 次进入 `COOLDOWN_SECONDS` 冷却，期间所有任务绕开它，冷却后重新优先尝试，成功即切回。健康状态只在内存中。
- `OPENAI_MODEL` 只用于首次启动生成兜底路由；之后以控制台的模型路由为准。
- 大模型默认网关：`OPENAI_API_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`。模型路由（控制台可改）可为每条路由单独配置网关，存于 SQLite `runtime_settings`。
- 模板见 `.env.example`；`.env` 含密钥，已被 gitignore，**不要把密钥写进 `.env.example`**。
- 运行时数据：`data/`（`reviews.db` SQLite、`repos/`、`worktrees/`、`codex-logs/`），日志：`log/`。均已 gitignore。

## 代码结构（app/）
- `main.py` — `create_app()`：组装运行时、请求日志中间件、路由与静态资源；模块级 `app = create_app()`
- `config.py` / `logging_config.py` / `models.py`（Webhook 载荷、API 入参、领域记录）/ `storage.py`（SQLite `Database`）
- `runtime.py` — `ReviewRuntime`：组装组件，检视工作线程、延后任务调度线程、启动恢复
- `api/` — 路由：`auth`、`webhooks`、`local`（本地提交检视）、`reviews`（列表/详情/事件/SSE/导出/管理员操作）、`settings`（模型路由、检视范围）、`stats`；`deps.py` 提供 `Runtime`、`AdminOnly`
- `review/` — 检视领域：`service.py`（单次 MR 主流程）、`diff.py`（解析/过滤/分片）、`findings.py`（定位校验/去重）、`prompts.py`、`scope.py`（检视范围与跳过规则）、`git.py`（镜像/clone/diff）、`codegraph.py`（可选索引，失败只降级）
- 任务 `origin`：`codehub`（MR，结果回写 CodeHub）/ `local`（本地提交，意见与评价状态为 `local`，不回写）
- `llm/` — `registry.py`（模型路由与网关）、`codex.py`（Codex CLI 调用与重试）、`output.py`（输出 JSON 与事件流解析）、`chat.py`（OpenAI 兼容 /models、/chat/completions）
- `codehub/` — `client.py`（CodeHub OpenAPI）、`publisher.py`（单线程限流回写评论与评价）、`evaluation.py`（MR 评分与评价文案）、`replies.py`（回复分类与自动解决）

## 约定
- 测试用 `tests/conftest.py` 的 `settings`/`database` fixture（tmp_path 隔离，`_env_file=None`）；公共构造工具在 `tests/helpers.py`（含伪造的 codex CLI）。
- 前端测试（`tests/test_frontend.py`）校验 `app.js` 引用的元素 id 都存在于 `index.html`，并对关键接口做字符串断言，改前端时同步更新。
- 用户可见文案、日志说明与代码注释以中文为主。
