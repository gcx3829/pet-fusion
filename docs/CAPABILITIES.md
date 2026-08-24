# Pet Fusion 能力矩阵

本文件记录仓库当前已经实现、已经验证和仍未实现的能力。它描述的是运行现状，
不是目标架构；目标和非协商约束仍以实施指南与 `AGENTS.md` 为准。

最后人工审计：2026-08-21。

| 能力 | 实现状态 | 验证状态 | 代码证据 |
| --- | --- | --- | --- |
| 内容寻址素材、不可变 source manifest、1～5 张宠物参考 | 已实现 | 离线测试通过 | `backend/app/services/asset_store.py`、`backend/app/api/projects.py` |
| Guidance Mask 画笔、上传与 Search 绑定 | 已实现 | 前后端测试通过 | `frontend/src/features/placement/GuidanceMaskEditor.tsx`、`backend/app/api/projects.py` |
| 白名单 EXIF/拍摄信息提取 | 已实现 | 单元测试及真实照片验证通过 | `backend/app/services/photography_metadata_service.py` |
| 多模态 Prompt Refiner 与结构化专业 Prompt | fake/live 均已实现 | 离线测试通过；兼容端点真实 smoke 通过 | `backend/app/graphs/multimodal_prompt_subgraph.py`、`backend/app/services/openai_prompt_refiner_client.py` |
| GPT Image 2 多参考图生成 | fake/live 均已实现 | 离线测试通过；兼容端点单候选真实 smoke 通过 | `backend/app/services/generator_service.py`、`backend/app/services/openai_image_client.py` |
| 独立 Critic、结构化问题与确定性 Ranker | fake/live Critic 已实现；live rubric 强制 0–100 量表并使用 source/raw 对照图检查构图和全局渲染漂移；跨字段矛盾会退出自动排名 | 离线测试通过；兼容端点真实 smoke 通过（新版 rubric 仍需重新 smoke） | `backend/app/graphs/critic_subgraph.py`、`backend/app/services/critic_service.py`、`backend/app/services/proxy_builder.py` |
| 自动多轮 source-only rebase、历史 Global Winner、interrupt/resume | 已实现 | 离线集成测试通过；尚未完成真实多轮基准 | `backend/app/graphs/search_graph.py`、`backend/app/services/search_runner.py` |
| Feedback Planner | 确定性本地实现 | 离线测试通过；没有 GPT-5.6 Luna transport | `backend/app/graphs/feedback_planner_subgraph.py`、`backend/app/services/planner_service.py` |
| 可选 Fusion Mask、羽化、原图像素保护 | 前后端已实现 | 像素与前端测试通过 | `backend/app/services/fusion_service.py`、`frontend/src/features/fusion/FusionEditor.tsx` |
| Local Fix 深度 0→2 | 后端图与 API 已实现 | fake provider 测试通过；没有真实 edit transport 和前端入口 | `backend/app/graphs/local_fix_graph.py`、`backend/app/api/local_fixes.py` |
| PNG/JPEG、原始分辨率、ICC/EXIF 导出 | 后端 API 已实现 | 离线测试通过；没有完整前端导出面板和真实摄影文件集基准 | `backend/app/services/export_service.py`、`backend/app/api/exports.py` |
| React 工作台、Raw 审片、Prompt 历史、Critic、时间线 | 已实现；审片区展示 8 个维度、结构化问题证据与人工决策上下文 | 组件测试、生产构建与应用浏览器验收通过 | `frontend/src/app/App.tsx`、`frontend/src/features/review/CriticInspector.tsx`、`frontend/src/features/search/PromptHistory.tsx` |

## 真实调用验证边界

2026-08-21 使用一张真实人物旅行照和一张真实宠物参考图，关闭
`FAKE_GENERATOR`、`FAKE_CRITIC`、`FAKE_PROMPT_REFINER`，完成了单候选、单轮 smoke：

- Prompt Refiner Responses Structured Outputs 成功；
- Image edits 成功并返回 PNG；
- Critic Responses Structured Outputs 成功，并正确指出宠物比例过大、位置侵入人物的阻断问题；
- provider-call 审计记录了三段调用，恢复时复用了已完成的 Prompt Refiner 和生成结果。

该验证使用本机 `.env` 配置的 OpenAI-compatible base URL，不是
`https://api.openai.com/v1`。因此它只证明当前兼容端点和 SDK 适配器在这组输入上可用，
不证明官方 OpenAI 直连、其他中转站、真实多轮搜索、服务端幂等 header 语义或主观成片质量。

## 文档同步规则

实现状态变化时，至少检查并按需更新：

1. 本能力矩阵；
2. `README.md` 与 `README.en.md` 的仓库状态、配置和限制；
3. `docs/QA_AND_LIVE_SMOKE.md` 的验证边界；
4. 若架构约束或目标发生变化，再更新实施指南、`CODEX_TASK.md` 或 `AGENTS.md`。
