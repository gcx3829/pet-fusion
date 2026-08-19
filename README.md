# Pet Fusion

**Pet Fusion** 是一个面向摄影师和宠物主人的 AI 摄影合成工作台：把一只具体、可辨认的宠物自然地放入真实旅行照片，同时尽量保留原始照片的构图、人物、环境、色彩空间和高分辨率细节。

它不是一个通用文生图工具，也不是一个 ComfyUI 节点集合。项目关注的是一个更窄但更难的任务：

> 给定真实旅行照和同一只宠物的若干参考照片，生成一张看起来像在同一时刻、由同一台相机拍摄的真实照片。

## 为什么做这个项目

最初设想是一条摄影工程管线：宠物抠图、姿态控制、EXIF 分析、透视匹配、颜色匹配、景深匹配、阴影和毛发融合。原型验证后得到一个重要结论：这些模块可以提高可控性，却无法弥补基础图像模型在三维重建、身份保持、光线理解和真实感上的能力差距。

因此当前版本采用新的职责划分：

- **GPT Image 2** 负责真正的视觉重建与合成；
- **LangGraph** 负责 Critic、反馈规划、候选排名、Global Winner、停止策略和崩溃恢复；
- **本地图像代码**负责裁切、Guidance/Fusion Mask、可选像素融合、全分辨率回贴、ICC 和 EXIF 交付；
- **用户界面**负责本地 Guidance Mask 画笔、摄影师文字意图和候选审片；旧 placement 字段仅作为 API/历史数据兼容。

## 核心工作流

```text
原始旅游照 + 1～5 张宠物参考图 + Guidance Mask 画笔 + 摄影师文字意图
                    ↓
       多模态 Prompt Refiner（Round 0）
       自然语言 → professional prompt
                    ↓
          GPT Image 2 生成候选组
                    ↓
       LangGraph 多模态 Critic 独立盲评
                    ↓
        确定性排名 + 历史 Global Winner
                    ↓
   达标则接受；自动无选中轮只应用 bounded directives
                    ↘
      用户选 raw candidate + 自然语言反馈
                    ↓
       Prompt Refiner（revision）生成 professional prompt
                    ↓
       immutable source + selected raw visual reference rebase
                    ↓
        用户可选 Fusion Mask + 羽化回贴
                    ↓
      回贴到原始分辨率并恢复 ICC / EXIF
```

自动 Critic/Planner 搜索永远从 immutable source 重新生成，绝不自动把上一轮候选作为图像输入；无人工选中时只在本地应用最多 3 条 bounded directives。人工明确选择当前轮 raw candidate 并继续时，下一轮仍以 immutable original 作为 image[0] 和 Guidance Mask base，把 selected raw 仅作为 image[1] visual reference，称为 `candidate_anchored_rebase`，不是 candidate edit。Fusion 和 Local Fix 都不进入 Search。

Round 0 与人工 revision 都经过多模态 Prompt Refiner：它读取底图、宠物参考、Guidance Mask、用户自然语言（revision 还读取 selected raw、对应 Critic 结果和反馈），输出经过本地校验的专业 Prompt。Critic、Ranker、人工审片仍只看 raw candidate。

## MVP 范围

第一版只解决以下任务：

1. 建立一个项目并上传一张旅行照；
2. 为同一只宠物上传 1～5 张参考图；
3. 在 Guidance Mask 画布中用 PS 风格画笔刷出模型可编辑区域；姿态、朝向和接触关系直接写入 prompt；
4. 一次生成多个候选；
5. 自动从身份、透视、光线、光学一致性、物理融合和背景保护等维度审片；
6. 进行最多若干轮、可恢复、预算受控的 Rebase 搜索；
7. 始终保留历史最佳结果；
8. 允许用户接受、继续搜索、取消或执行最多两次局部修复；
9. 输出原始分辨率 JPEG/PNG，并尽量保留 ICC Profile 和 EXIF。

## 当前非目标

- 通用节点式 AI 工作流编辑器；
- 本地扩散模型训练或推理平台；
- 自动恢复真实相机内参和绝对场景尺度；
- 完整 Photoshop 替代品；
- 视频合成；
- 多宠物、多人物复杂遮挡的第一版全自动解决方案；
- 通过无限 Critic 循环追求抽象分数最大化。

## 技术方向

```text
frontend/   React + TypeScript + Vite，负责素材、Guidance 画布、Raw 审片、时间线和 Fusion
backend/    FastAPI + Python，负责 API、资产、OpenAI 调用、图像处理和导出
LangGraph   显式 StateGraph，负责搜索状态、Critic、Planner、排名和恢复
SQLite      MVP 的业务数据与 LangGraph checkpoint
OpenAI      GPT Image 2 + GPT-5.6 系列多模态模型
```

## 文档阅读顺序

1. [`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md)：项目背景、原始构思、经验结论和产品边界；
2. [`CODEX_TASK.md`](CODEX_TASK.md)：交给 Codex 的直接任务；
3. [`docs/CODEX_GPT_IMAGE_2_LANGGRAPH_GREENFIELD_IMPLEMENTATION_GUIDE.md`](docs/CODEX_GPT_IMAGE_2_LANGGRAPH_GREENFIELD_IMPLEMENTATION_GUIDE.md)：完整工程实施规范；
4. [`AGENTS.md`](AGENTS.md)：仓库级实施约束。

## 仓库状态

这是一次 **greenfield rewrite**，没有导入旧 MVP 的 ComfyUI、Depth、抠图或规则合成代码。当前可离线验证的 MVP 包括：

```text
创建项目
→ 上传一张背景图和 1～5 张同一宠物的参考图
→ 内容寻址存储并固化 source manifest
→ worker 执行显式 SearchGraph
   ├─ CriticSubgraph：候选独立盲评
   └─ FeedbackPlannerSubgraph：只把 selected blocking issue 转成有界 directive
   └─ MultimodalPromptSubgraph：初始/人工 revision 专业 Prompt
→ 确定性 Ranker、历史 Global Winner、停止策略与 immutable-source rebase
→ SQLite checkpoint 与业务事件持久化
→ REST / SSE 返回候选
→ React 工作台展示 Guidance Mask、候选和时间线
→ raw candidate 作为 Search/Critic/人工审片权威图
→ Search 前可用本地 PS 风格 Guidance 画笔编辑软引导区域；仅自定义时上传一次 alpha PNG
→ 用户可选 Fusion Mask + 羽化回贴、全分辨率导出 JPEG/PNG、ICC/EXIF 尽力保留
```

Local Fix 已实现为独立、一次一调用的 LangGraph 后端图、服务和 HTTP API：它只允许 `generation_depth` 从 0 到 2，保留回退候选，并且不会回流到自动 Search。API 可引用已存 PNG mask，或只提交 full-resolution 的结构化矩形以由后端创建受校验 mask；相同语义请求会复用 SQLite provider-call audit。默认配置不会调用 OpenAI，也不需要 API key；mock 候选用于验证资产、状态、恢复边界和前后端数据流，不代表最终生成质量。

## 当前实现结构

```text
backend/   FastAPI、Search/Critic/Planner/Local Fix 显式 LangGraph、SQLite、内容寻址资产、可选 Fusion 与导出
frontend/  React、TypeScript、Vite、TanStack Query、PS 风格 Guidance/Fusion 画布、Raw 审片和照片时间线
data/      本地业务数据库、checkpoint 和项目资产；除 .gitkeep 外不进入 Git
scripts/   联合开发启动与确定性完整测试脚本
```

后端请求只在数据库中保存结构化状态和资产引用；图片字节保存在资产目录，不写入 LangGraph checkpoint。自动搜索生成入口接收固化后的 source manifest；人工 revision 才会显式携带已校验的 selected raw visual anchor，且 image[0] 仍是不可变原片。

## 本地启动

环境要求：

- [`uv`](https://docs.astral.sh/uv/)；项目要求 Python 3.12 或更高版本，`uv` 负责项目虚拟环境；
- Node.js 20.19+ 或 22.12+ 与 pnpm；
- macOS 或常见 Linux shell 环境。

首次安装依赖：

```bash
cp .env.example .env

cd backend
uv sync --locked --dev

cd ../frontend
pnpm install --frozen-lockfile

cd ..
```

随后从仓库根目录启动三个开发进程：

```bash
./scripts/dev.sh
```

默认地址：

- Web 工作台：`http://127.0.0.1:5173`；
- FastAPI：`http://127.0.0.1:8000`；
- OpenAPI 文档：`http://127.0.0.1:8000/docs`；
- 健康检查：`http://127.0.0.1:8000/api/v1/health`。

`scripts/dev.sh` 默认运行独立 worker，和 `.env.example` 中的 `RUN_INLINE=0` 对应。如果明确使用内联执行，可在启动脚本前设置 `PET_FUSION_SKIP_WORKER=1`，并同时把后端 `RUN_INLINE` 配成 `1`。

也可以在三个终端中分别运行：

```bash
cd backend
uv run --locked --env-file ../.env uvicorn app.main:app --reload
```

```bash
cd backend
uv run --locked --env-file ../.env python -m app.worker
```

```bash
cd frontend
pnpm dev
```

## 测试

从仓库根目录运行：

```bash
./scripts/test.sh
```

该脚本强制使用 fake generator、fake Critic、fake Prompt Refiner，并关闭 live smoke 开关；不会执行付费 API 调用或读取真实凭据。它依次运行：

```text
ruff → mypy → pytest → TypeScript typecheck → Vitest → Vite production build
```

需要单独调试时，可分别在 `backend/` 运行 `uv run --locked pytest`，在 `frontend/` 运行 `pnpm test`。pytest harness 也会强制 fake provider 并屏蔽两套 OpenAI credential/base URL 环境别名，避免已配置 live shell 时单测意外付费。额外的离线架构契约覆盖 Search 的 Critic/Planner 子图、Local Fix 的隔离路径、checkpoint 禁止图像数据，以及 fake/live provider 与 Export 路由边界；见 [`backend/tests/unit/test_architecture_contract.py`](backend/tests/unit/test_architecture_contract.py)。

需要检查覆盖率时运行：

```bash
./scripts/coverage.sh
```

该命令同样强制使用 fake provider，并生成后端与前端 HTML/JSON 报告。当前最低门槛为后端总覆盖率 85%，前端语句/行覆盖率 70%、分支/函数覆盖率 60%；报告分别写入 `backend/coverage/` 与 `frontend/coverage/`，不会进入 Git。

## 环境变量与密钥

`.env.example` 的安全默认值是：

```dotenv
FAKE_GENERATOR=1
FAKE_CRITIC=1
FAKE_PROMPT_REFINER=1
RUN_OPENAI_LIVE_TESTS=0
RUN_INLINE=0
OPENAI_API_KEY=
OPENAI_BASE_URL=
```

默认 `FAKE_GENERATOR=1`、`FAKE_CRITIC=1` 且 `FAKE_PROMPT_REFINER=1`，测试和日常开发不会读取或调用真实 OpenAI 凭据。把 `FAKE_GENERATOR` 设为 `0` 后，后端会通过官方 Image API 的 `images.edit` 使用不可变背景图、Guidance Mask 和 1～5 张参考图；把 `FAKE_CRITIC` 设为 `0` 后，会通过官方 Responses API 的 Pydantic Structured Outputs 独立评价每张 raw candidate；把 `FAKE_PROMPT_REFINER` 设为 `0` 后，会通过同一 Responses Structured Outputs 边界，将多模态输入转为结构化 professional prompt plan。三个开关彼此独立。

`RUN_OPENAI_LIVE_TESTS` 不参与应用 provider 选择，只为未来独立的 opt-in 自动 live test 预留；手工工作台验证仍由三个 `FAKE_*` 开关明确控制。默认测试脚本和 pytest harness 会把它保持为 `0`。

`OPENAI_BASE_URL` 是可选的、仅后端使用的 SDK base URL：留空即走官方 OpenAI 端点；中转站兼容性必须分别验证 Image edits、Critic Responses Structured Outputs 和 Prompt Refiner Responses Structured Outputs，任何一项成功都不能推断其他能力已验证。本轮只记录代码路径与离线契约，不声称已完成真实 provider/live 验证。真实模式使用后端锁定依赖中的官方 `openai` Python SDK，并且只在后端进程中读取 `OPENAI_API_KEY`。候选输入和输出保持 PNG；provider request ID 与数值 usage 写入调用审计，但不会记录 API key、图片 Base64、endpoint 原文或完整用户 prompt。当前 Planner 仍是确定性离线实现，`OPENAI_PLANNER_MODEL` 和 `OPENAI_CRITIC_ESCALATION_MODEL` 仅保留为实施指导中的预留配置，尚不会触发调用。`.env`、`.env.local` 和所有 `.env.*` 本地变体都会被 Git 忽略，只有 `.env.example` 允许提交。

不执行真实调用的配置检查、以及明天可手工执行的一次低成本 live smoke 步骤见 [`docs/QA_AND_LIVE_SMOKE.md`](docs/QA_AND_LIVE_SMOKE.md)。

## 已实现的 API 流程

所有业务接口使用 `/api/v1` 前缀：

1. `POST /projects`：multipart 上传背景图和 `cat_references`；
2. `POST /projects/{project_id}/searches`：提交搜索选项；旧 `placement` 字段仍为 API 兼容字段，提供 Guidance Mask 时位置由画笔决定、姿态与朝向由 photographer prompt 决定；必须携带稳定的 `Idempotency-Key` 请求头，同一项目、同一 key、同一请求会返回原搜索；
3. `GET /searches/{search_id}`：读取搜索状态和候选；
4. `GET /searches/{search_id}/events`：通过 SSE 接收持久化时间线；Prompt Inspector 相关事件为 `prompt.refiner.started`、`prompt.refiner.ready` 和 `prompt.refiner.failed`，事件只含轮次、模式、版本/哈希等安全摘要，不携带完整 prompt、用户反馈或图片字节；
5. `GET /assets/{asset_id}`：读取后端校验过的图片资产。
6. `POST /searches/{search_id}/resume`：对待人工确认的搜索执行接受历史 Global Winner、接受指定候选（`action=accept_candidate` + `selected_candidate_id`）、继续一轮或取消；继续时提交 `reviewed_round_index`，并可附带 `selected_candidate_id` 与 `human_feedback`，相同轮次和相同内容的网络重试会幂等返回当前状态。带 `selected_candidate_id` 的 `continue_one_round` 才会启动 candidate-anchored rebase；没有选中候选时只应用本地 bounded directives。`GET /searches/{search_id}` 返回每张候选的 Critic 维度、问题和 ranker 分数，以及 Prompt Inspector 使用的 `prompt_history`：`prompt_version_id/hash`、round/refinement/generation mode、parent version、prompt/generation model、canonical/generation prompt 与 hash、professional prompt plan、active directives 与 hash、`visual_anchor`、用户反馈/选中候选和 `tuned`。公开 projection 不含服务器路径；
7. `POST /searches/{search_id}/export`：只导出已接受搜索的历史 Global Winner，可选 PNG/JPEG、JPEG 质量与 ICC/EXIF 复制策略；
8. `GET /searches/{search_id}/exports/{export_key}`：读取幂等、内容寻址的导出记录。
9. `POST /searches/{search_id}/local-fixes`：对已接受搜索的历史候选（省略 `candidate_id` 时为 Global Winner）执行一次 isolated Local Fix；mask 可为内部 PNG asset ID 或结构化 full-resolution box，结果包含可重放 `request_key`；
10. `GET /searches/{search_id}/local-fixes/{fix_id}`：读取 SQLite provider-call audit 中的 Local Fix 结果。
11. `POST /projects/{project_id}/guidance-masks`：注册同尺寸 RGBA PNG Guidance Mask；Search 启动请求可携带 `guidance_mask_asset_id`。前端画笔编辑、预览和撤销/重做均为本地操作，同一 project + document hash 的重试只上传一次；当前工作台要求至少绘制一笔后才能启动 Search。后端仍能读取旧 placement-only 请求作为 API/历史数据兼容，但新工作台不再提供位置框、姿态或朝向控件。Guidance Mask 是软引导而非像素锁，Search 启动后锁定。
12. `POST /searches/{search_id}/fusion-masks`：将同尺寸 RGBA PNG alpha mask 上传并绑定到已接受搜索；`POST /searches/{search_id}/fusions`：以矩形或已绑定 alpha mask + 羽化生成独立 Fusion 预览；`GET /searches/{search_id}/fusions/{fusion_key}`：幂等读取 Fusion 结果。Fusion 不改写 raw，也不回流 Critic/Search。

浏览器在开发模式下通过 Vite 的 `/api` 代理访问后端，因此不会接触服务端密钥。
`scripts/dev.sh` 会根据 `PET_FUSION_API_HOST` 和 `PET_FUSION_API_PORT` 自动配置 Vite 代理；也可用 `VITE_DEV_API_TARGET=http://host:port ./scripts/dev.sh` 显式覆盖。需要让浏览器跨域直连时，则在 `frontend/.env.local` 中设置 `VITE_API_BASE_URL`。

## 当前限制

- 默认仍使用确定性的 mock generator、Critic 和 Prompt Refiner；`FAKE_GENERATOR=0`、`FAKE_CRITIC=0`、`FAKE_PROMPT_REFINER=0` 的官方 SDK 代码路径已接通，但本轮不声称已完成真实 provider/live 验证。中转站必须分别验证 Image edits、Critic Responses Structured Outputs 与 Prompt Refiner Responses Structured Outputs；Image edits 成功不能推断 Responses 能力。Feedback Planner 仍是离线确定性 provider，尚无 GPT-5.6 Luna transport；
- raw-first Search/Critic/人工接受、Guidance Mask、全分辨率回贴、PNG/JPEG 生产导出 API，以及 ICC/EXIF 尽力保留均已实现。Fusion Mask 是用户显式触发的独立融合层，后端提供 search-scoped alpha mask 上传与矩形/PNG alpha + 羽化 API，前端提供 Fusion 预览编辑器；旧 Composite Floor 资产仅为兼容 Local Fix、导出和历史 SQLite 数据保留，不作为 Search/Critic 默认图像；
- checkpoint、搜索 lease 和 provider-call lease 已覆盖本机进程崩溃恢复边界，但生产队列、跨主机协调、鉴权和对象存储尚未实现；
- 前端已覆盖素材分配、Guidance 画笔、Search、Critic 分数/问题、Timeline 唯一选片控制、人工接受、Fusion 画笔与融合结果的全尺寸 PNG 直接下载。Local Fix 尚无前端入口；生产 Export API 的 JPEG/PNG、质量、ICC/EXIF 策略也尚未做成完整导出面板。

这些限制是实施指导中分阶段交付的结果，不是对非协商架构约束的替代；后续真实 provider、自动多轮和局部修复仍必须遵守 immutable-source rebase、幂等调用、PNG lineage、历史最佳和最大修复深度 2 等规则。
