# HoH - Harness of Harness

独立的本地控制平面，用统一任务、事件和验证协议调度 Hermes 与 DeepSeek Harness（DSH）。

**License:** MIT — see [`LICENSE`](LICENSE).

> HoH is an independent control plane, not a Hermes or DSH core modification. Hermes and DSH are connected through thin adapters.

## 当前能力

- SQLite 任务和事件持久化
- Hermes / DSH 真实入口健康检查
- Hermes ACP 依赖检查
- 任务状态生命周期
- 任务依赖 DAG、循环检测和 ready 队列
- Git worktree 创建与删除工具
- 后台 Worker 生命周期（mock / Hermes CLI / DSH CLI）
- 工作区边界验证
- 独立验证命令
- CLI 创建、运行、查询和 dry-run
- 本地 HTTP 控制面
- 内置 Web Dashboard（启动后访问 `http://127.0.0.1:8765/`）
- SSE 实时事件流
- 失败任务重试和运行日志保存到 `HOH_HOME/runs/`

## 使用

在 Windows Git Bash 中使用 Hermes 自带解释器：

```bash
export PYTHONPATH=F:/Tools/HOH
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli health
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli create \
  --title "认证模块" \
  --prompt "分析当前项目的认证模块" \
  --workspace "F:/Output/project" \
  --harness auto
```

安全演练不会启动 Agent：

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli run \
  --title "dry run" --prompt "inspect only" \
  --workspace "F:/Tools/HOH" --harness hermes --dry-run
```

真正启动 Worker 前，先运行 `health`。涉及生产、推送、部署和删除操作仍应由后续策略层拦截并人工批准。

## 规划与物化 API (Planner)

通过目标描述自动进行依赖分析，并按能力路由到 Hermes 或 DSH：

```bash
# 1. 纯预览规划（不产生副作用，不写入数据库）
curl -X POST http://127.0.0.1:8765/v1/plans/preview \
  -H 'Content-Type: application/json' \
  -d '{"goal":"实现并验证导出功能","workspace":"F:/Output/project","harness":"auto"}'

# 2. 确认无误后物化为具体 DAG 任务
curl -X POST http://127.0.0.1:8765/v1/plans/materialize \
  -H 'Content-Type: application/json' \
  -d '{"goal":"实现并验证导出功能","workspace":"F:/Output/project","harness":"auto"}'
```

也可以直接通过 CLI 进行规划预览与物化：

```bash
# 预览
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli plan --goal "分析认证流程" --workspace "F:/Output/project"

# 物化
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli materialize --goal "分析认证流程" --workspace "F:/Output/project"
```

## DAG 与 Worker API

创建子任务时传入 `depends_on`：

```bash
curl -X POST http://127.0.0.1:8765/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"child","prompt":"implement","workspace":"F:/Output/project","depends_on":["task_parent"]}'
```

查看依赖已满足、可以启动的任务：

```bash
curl http://127.0.0.1:8765/v1/tasks/ready
```

启动一个任务。当前 `mock` 用于确定性测试，生产执行器可传 `hermes` 或 `dsh`：

```bash
curl -X POST http://127.0.0.1:8765/v1/tasks/task_id/start \
  -H 'Content-Type: application/json' \
  -d '{"harness":"mock"}'
```

Git 仓库任务的生命周期是：

```text
pending → running → verifying → waiting_approval → completed
```

验证通过后不会自动合并。审批接口：

```bash
curl -X POST http://127.0.0.1:8765/v1/tasks/task_id/approve \
  -H 'Content-Type: application/json' \
  -d '{}'
```

审批会真实执行 Git merge，返回 `merge_sha`，并清理对应 worktree。验证失败则进入 `failed`，不会合并。

实时订阅任务事件：

```bash
curl -N "http://127.0.0.1:8765/v1/tasks/task_id/stream?after=0"
```

事件使用 SSE 格式，客户端用返回的 `id` 作为下一次 `after` 游标，避免断线后重复处理。一次性读取已有事件可使用 `?once=1`。

失败任务可以重试：

```bash
curl -X POST http://127.0.0.1:8765/v1/tasks/task_id/retry \
  -H 'Content-Type: application/json' \
  -d '{"harness":"hermes"}'
```

不指定 `harness` 时，会复用上一次运行的 Harness。

## 当前边界

- Git 仓库任务启动时会自动创建隔离 worktree，并在验证通过后等待审批；非 Git 工作区仍使用原目录执行，暂不支持自动合并。
- Hermes CLI 已接入；Hermes ACP 仍需当前 Hermes 环境安装 `[acp]` 可选依赖。
- DSH 插件目前是 REST 薄客户端骨架，尚未安装到用户 profile，也未假设未经探测的 DSH Tool/UI 私有 API。
