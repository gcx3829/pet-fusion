# QA 与手工 Live Smoke

本文档记录当前 greenfield MVP 的离线验收边界，以及**只有显式授权后**才执行的手工 live smoke。本文档本身不会发起任何 OpenAI 或中转站请求。

## 离线验收

从仓库根目录运行：

```bash
UV_CACHE_DIR=/tmp/pet-fusion-uv-cache ./scripts/test.sh
```

`scripts/test.sh` 会强制设置：

```dotenv
FAKE_GENERATOR=1
FAKE_CRITIC=1
RUN_OPENAI_LIVE_TESTS=0
```

脚本还会屏蔽 `OPENAI_API_KEY` / `PET_FUSION_OPENAI_API_KEY` 与两套 base URL 别名。因此测试不依赖、也无法使用 `.env` 或当前 shell 中可能存在的真实凭据；直接在 `backend/` 运行 pytest 时，默认 harness 会施加相同隔离。完整套件覆盖 ruff、mypy、pytest、TypeScript typecheck、Vitest 和 Vite production build。

其中 [`backend/tests/unit/test_architecture_contract.py`](../backend/tests/unit/test_architecture_contract.py) 固定以下离线契约：

- SearchGraph 的 CriticSubgraph 与 FeedbackPlannerSubgraph 均以显式嵌套图存在；
- Local Fix 只能经过一次性的 `resolve → apply → finalize` 路径，不能回到自动 Search；
- checkpoint guard 接受资产引用、拒绝图像 bytes 和 `data:image` 数据；
- fake/live generator 与 Critic 开关在构造期不请求网络，且公共 health/OpenAPI 不泄露 API key；
- Export 的创建和读取路由保持显式、版本化的 API 合约。

已有更细的集成测试继续覆盖 immutable-source rebase、provider idempotency、Critic reducer、Local Fix 深度上限、Composite Floor 像素精确性、JPEG/PNG 导出和 metadata 回贴。

## 手工 live smoke（不在默认测试中运行）

先从模板创建只存在于本机的配置：

```bash
cp .env.example .env
```

在 `.env` 中只在准备实际付费验证时修改以下值：

```dotenv
FAKE_GENERATOR=0
FAKE_CRITIC=0
RUN_INLINE=0
OPENAI_API_KEY=<仅保存在后端本机环境的密钥>

# 官方端点：留空
OPENAI_BASE_URL=

# 若使用你管理的 OpenAI-compatible 中转站，改为其 SDK base URL，例如：
# OPENAI_BASE_URL=https://<relay-host>/v1
```

`RUN_OPENAI_LIVE_TESTS` 目前不参与应用 provider 选择，保持 `0` 即可；它只为未来独立的 opt-in 自动 live test 预留。手工 smoke 是否调用 provider 完全由上面的 `FAKE_GENERATOR` / `FAKE_CRITIC` 决定。

配置检查不会调用 provider，也不会打印密钥：

```bash
cd backend
uv run --locked --env-file ../.env python -c 'from app.config import Settings; s = Settings(); print({"fake_generator": s.fake_generator, "fake_critic": s.fake_critic, "has_api_key": bool(s.openai_api_key and s.openai_api_key.get_secret_value()), "base_url_configured": bool(s.openai_base_url)})'
```

启动 API、独立 worker 和前端：

```bash
cd ..
./scripts/dev.sh
```

只有在工作台提交搜索后才会调用 provider。首次实际 smoke 建议只提交一个小尺寸背景、1 张参考图、`candidate_count=1`、`max_rounds=1`，并在结果出现后停止；不要把默认测试脚本改成 live 调用。

`OPENAI_BASE_URL` 同时传给 Image edits 和 Critic Responses 客户端。因此中转站至少要兼容：

- `images.edit` 的多 PNG 输入与 PNG 输出；
- Responses API 的 Pydantic Structured Outputs；
- 返回可审计 request ID/usage 时的稳定响应形状。

若只想先隔离验证一个 provider，可保留另一个 fake 开关为 `1`。Planner 目前没有 live transport，仍使用确定性本地规划，不会因为设置 `OPENAI_PLANNER_MODEL` 而产生请求。

完成验证后，将 `FAKE_GENERATOR` 和 `FAKE_CRITIC` 恢复为 `1`，再运行完整离线套件。

## 当前实现边界与后续基准

| 领域 | 当前状态 | 仍需人工/后续实现 |
| --- | --- | --- |
| 自动 Search | 显式 LangGraph、Critic/Planner 子图、Ranker、Global Winner、rebase、checkpoint | 真实凭据联调、生产队列与跨主机协调 |
| Critic / Planner | fake Critic 与可选 live Critic transport；确定性 Planner policy | GPT-5.6 Luna Planner transport、Sol escalation |
| 背景保护 / 导出 | Composite Floor、原始分辨率回贴、PNG/JPEG、ICC/EXIF 尽力保留、Export API | 前端导出体验与真实摄影文件集验证 |
| Local Fix | 独立后端图、tight mask、0→2 深度保护、SQLite 幂等回退与 FastAPI route | 真实 edit transport、前端入口 |
| 基准 | 离线架构和回归测试 | 按实施指南准备 5 只宠物 / 15 张旅行照的盲评集，比较单轮、rebase search 与连续 I2I，并记录成本、轮数和摄影师偏好 |

这些限制不会改变 immutable source、PNG lineage、历史 Global Winner、Composite Floor 和 Local Fix 深度上限等架构约束。
