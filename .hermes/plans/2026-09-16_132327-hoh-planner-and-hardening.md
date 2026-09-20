# HoH Planner and Harness Integration Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 将 HoH 从“可手工创建和执行任务”扩展为可解释的 Planner：把自然语言目标或结构化步骤转换为真实 DAG，按角色和能力路由到 Hermes/DSH，并在不削弱现有 worktree、验证、审批约束的前提下提供 CLI/HTTP 使用入口。

**Architecture:** Planner 保持为独立的纯 Python 模块，只产生 `Plan` 和 `PlannedStep`，不复制 Runner 调度逻辑，也不直接调用模型。`materialize()` 是唯一把计划转换成 `HohStore` 任务的边界；所有执行仍由现有 `TaskRunner`、worktree、验收和人工审批链路负责。Hermes 与 DSH 继续作为可替换适配器，Planner 的路由只依据显式能力表和实际健康状态，不把“规划成功”当成“执行成功”。

**Tech Stack:** Python 3.13；标准库 `dataclasses`、`sqlite3`、`unittest`、`http.server`；现有 Hermes CLI、DSH CLI、Git worktree；本地 HTTP 绑定 `127.0.0.1`。

---

## 当前上下文与已知问题

1. **已经完成并通过回归的基础能力**
   - `HohStore` 持久化任务、事件、依赖和扩展规格。
   - 任务依赖 DAG、循环检测和 ready 队列。
   - Git worktree 隔离、真实验收命令、验证证据、人工 approve、merge SHA。
   - SSE：`GET /v1/tasks/{id}/stream`，支持 `after` 游标和 `once=1`。
   - 失败重试：`POST /v1/tasks/{id}/retry`，每次产生新的 `run_id`。
   - Hermes/DSH CLI 健康检查和基础适配器。

2. **Planner 当前状态**
   - `C:/Users/ASUS/hoh/hoh/planner.py` 已有一个确定性、规则驱动的初版。
   - `C:/Users/ASUS/hoh/tests/test_planner.py` 已有测试草稿。
   - Planner 尚未接入 `hoh.cli` 或 `hoh.server`，因此用户不能从 HoH 入口创建计划。
   - Planner 目前只是一组规则，不能声称理解任意自然语言；模糊目标应保持只读或要求人工提供结构化步骤。

3. **当前未完成或存在的问题**
   - Planner 测试目前不全绿：
     - 测试在异常断言路径未关闭 `HohStore`，Windows 临时目录清理会出现 `WinError 32`。
     - `plan_from_steps()` 对未知依赖的校验发生在计划构造阶段，而测试预期在 `materialize()` 阶段；需要统一契约。
   - 需要确认并补充完整读模型：任务详情必须稳定返回 `acceptance`、`constraints`、`depends_on`，不能只返回主表字段。
   - Planner 路由当前把 Hermes/DSH 能力写死为同等能力；需要与 `configured_harnesses()` 的健康状态结合，并对不可用 Harness 给出明确错误或降级策略。
   - 没有 Planner HTTP/CLI 入口，也没有“计划预览后再物化”的人工确认边界。
   - 没有计划级状态：无法区分“计划已生成”“计划已物化”“计划中的某个子任务执行失败”。
   - 没有计划版本、输入摘要或规则版本记录，后续无法解释为什么生成了某个 DAG。
   - Planner 生成的步骤暂时没有可靠的验收标准推导；不能自动生成未经确认的危险命令。
   - `TaskRunner` 的真实 Hermes/DSH 执行仍是 CLI 黑盒调用；Hermes ACP 依赖尚未安装，DSH 插件也尚未安装到 profile，不能把原生流式 Tool/UI 接入列为当前完成项。
   - 当前项目目录不是 Git 仓库，不能用项目自身 commit 作为交付证据；应依靠真实测试、编译、Daemon HTTP E2E 和文件内容验证。

---

## 实现原则

- **核心编排逻辑只有一份。** Hermes 和 DSH 内只保留薄客户端，不复制 Planner、依赖调度、验证或审批逻辑。
- **先预览，后物化。** Planner 生成结果必须可以 JSON 展示并人工检查；HTTP/CLI 显式调用 materialize 后才创建真实任务。
- **Agent 声明不决定完成。** 完成状态仍由 HoH Runner 的退出码、验收命令、Git diff、人工 approve 决定。
- **默认最小权限。** 无法确定目标需要写入时，生成只读计划；任何写操作都必须由明确的写入目标或结构化步骤触发。
- **独立审查。** implementer 与 reviewer 默认使用不同 Harness；如果只有一个健康 Harness，计划必须标记“无法执行异构审查”，不能伪造独立性。
- **不自动提交、推送、部署或删除生产数据。** Planner 只能提出任务，后续策略层和人工审批负责高风险动作。
- **确定性优先。** 规则 Planner 先稳定落地；未来如接入 LLM，只允许 LLM 输出经过严格 schema、依赖、路径、命令和能力校验的 `Plan`。

---

## 逐步实施计划

### Task 1: 固定 Planner 数据契约并修复当前测试

**Objective:** 让 `Plan`、`PlannedStep`、依赖校验和资源清理的行为明确且测试全绿。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/planner.py`
- Modify: `C:/Users/ASUS/hoh/tests/test_planner.py`
- Verify: `C:/Users/ASUS/hoh/hoh/core.py`

**Step 1: Write failing tests**

补充并明确以下契约：

- `plan_from_steps()` 负责校验重复 key 和未知依赖，失败就在该函数抛出 `ValueError`。
- `materialize()` 负责拓扑排序和循环检测。
- 所有创建 `HohStore` 的测试都在 `try/finally` 或 `addCleanup()` 中关闭 Store。
- `Plan` 的每个 step 必须拥有有效 role、非空 key、合法 harness（`auto` 或健康能力表中的名称）。
- 结构化步骤顺序可以不是拓扑顺序，但 materialize 后必须按拓扑顺序创建。

Run:

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_planner -v
```

Expected initial state: the current planner tests fail because of the cleanup path and the mismatch between where unknown dependencies are validated.

**Step 2: Implement minimal fixes**

- 统一未知依赖校验位置，不让 `plan_from_steps()` 和 `_topological()` 重复产生不同语义。
- 在测试中确保每个 Store 都关闭，避免 Windows SQLite 文件锁影响后续断言。
- 保留 `RLock`，因为任务读模型会在 `_task_from_row()` 内读取依赖和扩展规格。
- 不在此任务加入 API、模型调用或自动执行。

**Step 3: Verify**

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_planner -v
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m py_compile hoh/*.py tests/*.py
```

Expected: Planner tests pass; Python compilation passes.

---

### Task 2: 完善任务读模型和计划证据

**Objective:** 让任何客户端读取任务时都能看到依赖、验收、约束和计划来源，而不是只看到主表字段。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/core.py`
- Modify: `C:/Users/ASUS/ASUS/hoh/tests/test_core.py` (实际路径应为 `C:/Users/ASUS/hoh/tests/test_core.py`)
- Modify: `C:/Users/ASUS/hoh/tests/test_server.py`

**Step 1: Write failing tests**

覆盖：

- `get_task()` 返回 `depends_on`、`acceptance`、`constraints`。
- `list_tasks()` 返回同样的字段。
- 不存在规格记录的旧任务仍能读取，使用空列表兼容旧数据库。
- HTTP `GET /v1/tasks/{id}` 和 `GET /v1/tasks` 与 Store 读模型一致。

**Step 2: Implement**

- 保持 `tasks` 主表兼容，不把扩展字段强行迁移进主表。
- 由单一 `_task_from_row()` 组装完整 `Task`。
- 读取扩展表和依赖表时使用现有 `RLock`。
- 如加入 `plan_id`、`plan_version`、`planner_kind`，应新建 `task_plan_meta` 扩展表或在现有规格表追加兼容列，并提供初始化迁移；不要破坏已有 `state.db`。

**Step 3: Verify**

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_core tests.test_server -v
```

Expected: 所有原有任务生命周期、DAG、HTTP 读模型测试通过。

---

### Task 3: 添加计划预览 JSON 序列化

**Objective:** 在物化前能把完整计划返回给 CLI、HTTP 和未来 UI，便于人工确认。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/planner.py`
- Create or modify: `C:/Users/ASUS/hoh/hoh/serialization.py`（仅在现有模块没有合适位置时创建）
- Test: `C:/Users/ASUS/hoh/tests/test_planner.py`

**Step 1: Write failing tests**

测试 `plan_to_dict(plan)`：

- 返回 `goal`、绝对或规范化 `workspace`、按顺序排列的 steps。
- 每个 step 返回 `key`、`title`、`role`、`prompt`、`depends_on_keys`、`acceptance`、`harness`。
- JSON 可被 `json.dumps(..., ensure_ascii=False)` 序列化。
- 不包含线程句柄、Path 原对象或不可序列化状态。

**Step 2: Implement**

- 使用 `dataclasses.asdict` 或显式 serializer，统一 Path 转字符串。
- 不返回模型内部对象引用。
- 记录 `planner_kind="deterministic-v1"` 和规则版本常量，便于后续审计。

**Step 3: Verify**

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_planner -v
```

Expected: 计划序列化测试通过，输出稳定。

---

### Task 4: 加入 CLI `plan` 和 `materialize` 命令

**Objective:** 让操作者可以先预览计划，再明确物化为真实 HoH 任务。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/cli.py`
- Modify: `C:/Users/ASUS/ASUS/hoh/README.md` (实际路径应为 `C:/Users/ASUS/hoh/README.md`)
- Test: Create or modify `C:/Users/ASUS/hoh/tests/test_cli.py`

**Step 1: Write failing tests**

新增命令契约：

```bash
hoh plan --goal "实现离线任务编辑并补测试" --workspace C:/repo --harness dsh
hoh materialize --goal "实现离线任务编辑并补测试" --workspace C:/repo --harness dsh
```

- `plan` 只打印 JSON，不写任务数据库，不启动 Harness。
- `materialize` 创建真实任务并打印各 step 的 key、task_id、依赖和 harness。
- `--harness` 只接受 `auto|hermes|dsh`。
- 不可用 Harness 时返回非零，并解释是健康检查失败还是路由无候选。

**Step 2: Implement**

- `command_plan()` 调用 `plan_from_goal()` 和 serializer。
- `command_materialize()` 打开 Store，调用 `materialize()`，在 finally 中关闭 Store。
- 不在 CLI 中复制依赖、路由或执行逻辑。
- `materialize` 不自动 `start`，避免预览/物化命令产生未审批的执行副作用。

**Step 3: Verify**

```bash
set PYTHONPATH=C:/Users/ASUS/hoh
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_cli -v
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli --home C:/Users/ASUS/hoh/.plan-test plan --goal "分析认证流程" --workspace C:/Users/ASUS/hoh
```

Expected: `plan` 返回合法 JSON，数据库没有新增任务。

---

### Task 5: 加入 HTTP 计划预览和物化接口

**Objective:** 让 Hermes/DSH 薄客户端和未来 UI 使用同一套 Planner，而不是各自实现拆解逻辑。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/server.py`
- Modify: `C:/Users/ASUS/hoh/hoh/planner.py`
- Modify: `C:/Users/ASUS/ASUS/hoh/tests/test_server.py` (实际路径应为 `C:/Users/ASUS/hoh/tests/test_server.py`)
- Modify: `C:/Users/ASUS/hoh/README.md`

**Step 1: Write failing tests**

建议 API：

```text
POST /v1/plans/preview
POST /v1/plans/materialize
```

请求：

```json
{
  "goal": "实现离线任务编辑并补测试",
  "workspace": "C:/repo",
  "harness": "dsh"
}
```

测试要求：

- preview 返回计划 JSON，不创建 tasks。
- materialize 返回 `plan_id` 或确定性计划摘要以及 task 列表。
- materialize 前再次验证依赖、workspace 和 Harness 路由。
- malformed JSON、缺少 goal/workspace、不可用 Harness 返回 400/409，不返回 500。

**Step 2: Implement**

- 在 `HohApi` 增加薄方法，实际逻辑放 Planner。
- 若引入 `plan_id`，在 SQLite 中持久化 goal、workspace、planner kind/version 和 step-to-task 映射。
- 计划物化后发送 `plan.created`、`plan.materialized` 事件，不能把子任务直接标记 completed。
- 保持服务只绑定 `127.0.0.1`，不增加远程开放端口。

**Step 3: Verify**

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_server -v
```

然后用独立临时 home 启动 Daemon，通过 `curl` 验证 preview 不写 DB、materialize 写入任务并可从 `/v1/tasks` 读回。

---

### Task 6: 将真实 Harness 健康状态纳入路由

**Objective:** 防止 Planner 把任务路由给当前不可用的 Hermes 或 DSH，并诚实呈现“无法异构审查”的情况。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/planner.py`
- Modify: `C:/Users/ASUS/hoh/hoh/adapters.py`
- Test: `C:/Users/ASUS/hoh/tests/test_planner.py`
- Test: `C:/Users/ASUS/hoh/tests/test_adapters.py`

**Step 1: Write failing tests**

- 路由输入接受健康 Harness 集合。
- preferred Harness 不可用时返回明确错误，或只在显式允许 fallback 时降级。
- implementer/reviewer 需要异构 Harness，但健康集合只有一个时返回 `independent_review_available=false` 和原因。
- `auto` 路由只选择 `available=true` 的适配器。

**Step 2: Implement**

- 将 `configured_harnesses()` 转成 Planner 的能力输入，不在 Planner 内重复执行版本命令。
- 为路由结果增加可解释字段：`harness`、`reason`、`capabilities`、`available`。
- 保留 deterministic fallback；不要因为健康检查失败而自动调用未经确认的其他外部程序。

**Step 3: Verify**

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_planner tests.test_adapters -v
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m hoh.cli --home C:/Users/ASUS/hoh/.plan-health health
```

Expected: 输出真实 Hermes/DSH 健康状态；不可用时 exit code 非零。

---

### Task 7: 接入 DSH/Hermes 薄客户端的计划能力

**Objective:** 让 DSH 插件和 Hermes 入口只调用 HoH REST，不复制 Planner 和 DAG 逻辑。

**Files:**
- Modify: `C:/Users/ASUS/hoh/integrations/dsh-plugin-hoh/lib/index.js`
- Modify: `C:/Users/ASUS/hoh/integrations/dsh-plugin-hoh/package.json`
- Add tests where supported: `C:/Users/ASUS/hoh/integrations/dsh-plugin-hoh/tests/`
- Modify: `C:/Users/ASUS/hoh/README.md`

**Step 1: Write failing tests**

- client 暴露 `previewPlan()`、`materializePlan()`、`getTaskStreamUrl()`、`retryTask()`。
- 请求失败时保留 HTTP status 和 HoH error body。
- 不把 HoH_URL、workspace 或 token 写死在插件源码。
- DSH 插件只注册薄 REST handler；不直接操作 SQLite、Git 或执行 Worker。

**Step 2: Implement**

- 复用当前 `request()`，增加小型 client 方法。
- 对 SSE 只返回 URL/订阅信息；若 DSH webServer 支持代理，再做经过确认的代理，否则不猜私有 API。
- Hermes 侧继续以 CLI adapter 为主，ACP 依赖未安装前不宣称 ACP 原生接入。

**Step 3: Verify**

```bash
node --check integrations/dsh-plugin-hoh/lib/index.js
```

用本地 HoH Daemon 真实响应验证 preview/materialize 请求；只有确认 DSH plugin API 后才安装到 profile。

---

### Task 8: 计划级失败恢复、取消和审计

**Objective:** 让计划作为一个整体可观察、可暂停、可恢复，同时保留子任务级事实证据。

**Files:**
- Modify: `C:/Users/ASUS/hoh/hoh/core.py`
- Modify: `C:/Users/ASUS/hoh/hoh/runner.py`
- Modify: `C:/Users/ASUS/hoh/hoh/server.py`
- Test: `C:/Users/ASUS/hoh/tests/test_plan_lifecycle.py`

**Step 1: Write failing tests**

覆盖：

- 单个子任务失败时计划进入 `blocked` 或 `failed`，后续依赖任务不启动。
- retry 子任务成功后计划可重新进入 ready/running。
- 计划取消不会杀死不属于该计划的 run。
- 计划事件中保存真实子任务 ID、run ID、Harness、exit code、验证结果和审批状态。
- 重复 materialize 不会无意创建第二组任务；需要显式幂等 key 或返回冲突。

**Step 2: Implement**

- 添加计划到任务的持久化映射和状态聚合函数。
- Runner 仍是唯一执行者；计划协调器只选择 ready 子任务和汇总状态。
- 任何自动 retry 都要有有限次数、事件记录和人工可见原因；生产动作不自动 retry。

**Step 3: Verify**

```bash
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest tests.test_plan_lifecycle -v
```

使用两个 mock 子任务和一个真实临时 Git 仓库验证：失败、retry、审批、merge 和计划状态聚合。

---

## 最终验收清单

实现完上述任务后，必须执行并保留真实输出：

```bash
cd C:/Users/ASUS/hoh
set PYTHONPATH=C:/Users/ASUS/hoh
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m unittest discover -s tests -v
F:/hermes/hermes-agent/venv-win/Scripts/python.exe -m py_compile hoh/*.py tests/*.py
node --check integrations/dsh-plugin-hoh/lib/index.js
```

预期：

- 全部单元和 HTTP 测试通过。
- Planner preview 不产生执行副作用。
- materialize 创建真实 DAG，任务详情能读回依赖、验收和约束。
- ready 队列只释放依赖满足的任务。
- Runner 使用真实 worktree，不修改主工作区。
- 验收失败不会进入 approve/merge。
- approve 后返回真实 `merge_sha`，并清理 worktree。
- SSE 可以从事件 ID 续传，不重复处理已确认事件。
- retry 生成新的 `run_id`，且失败原因和重试关系可审计。
- Hermes/DSH 不包含第二套编排逻辑。

额外的真实 Daemon E2E：

1. 在临时 Git 仓库启动 HoH Daemon。
2. `POST /v1/plans/preview`，确认只返回计划，不新增 tasks。
3. `POST /v1/plans/materialize`，确认返回多个真实 task ID 和依赖。
4. `GET /v1/tasks/ready`，确认只有根任务 ready。
5. 启动 mock 或真实 Harness，读取 SSE。
6. 检查验证证据、等待审批、approve、真实 merge SHA 和主仓库产物。
7. 关闭 Daemon，确认没有后台进程、临时 worktree 或 SQLite 文件锁残留。

---

## 风险、取舍和开放问题

- **自然语言拆解准确性：** 规则 Planner 只能处理有限模式；不能把关键词命中包装成通用智能规划。LLM Planner 应作为后续 provider，输出必须 schema 校验、拒绝危险命令并等待人工预览。
- **异构审查可用性：** Hermes ACP 未安装、DSH 插件未安装时，不能宣称两个 Harness 都可用。只有一个健康后端时，应明确降低审查强度，而不是把同一后端调用两次称为独立审查。
- **Git 并发：** 多个计划同时在同一仓库创建分支、merge，可能遇到冲突。第一版应串行化同一根仓库的 approve，冲突进入人工处理状态，不自动强推或重写历史。
- **验收命令安全：** 当前验证使用受控 workspace 和 shell 命令；后续应增加命令 allowlist/超时/资源限制，尤其是来自 LLM 的计划。
- **持久化迁移：** 现有数据库没有计划表。必须采用向后兼容的 `CREATE TABLE IF NOT EXISTS` 或明确迁移版本，不删除旧列和旧数据。
- **SSE 生命周期：** 当前流有 30 秒轮询窗口，生产级客户端需要断线重连、心跳和明确的 stream close 语义；不要无限创建线程或连接。
- **DSH 私有 API：** 当前只确认 `webServer` 注入和薄插件骨架，未确认完整 Tool/UI 注册契约。在获得真实 API 文档或最小可运行插件验证前，不安装、不猜测、不把静态语法通过当作插件可用。
- **项目版本控制：** `C:/Users/ASUS/hoh` 当前不是 Git 仓库，无法通过 commit SHA 证明改动。交付证据必须依赖测试输出、编译结果、HTTP E2E 输出和实际文件路径。
