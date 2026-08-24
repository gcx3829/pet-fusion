# Pet Fusion

[简体中文](README.md) | [English](README.en.md)

**Pet Fusion** is a photography-first AI compositing workbench. It places a specific, recognizable pet into a real photograph while preserving the source composition, people, environment, color characteristics, and high-resolution detail.

The project is deliberately narrower than a general image generator:

> Given a real travel photograph and one or more pet references, create an image that looks as if it was captured at the same moment by the same camera.

## How it works

- **GPT Image 2** performs visual reconstruction and generation.
- **LangGraph** runs explicit Prompt Refiner, Critic, feedback-planning, ranking, stopping, and recovery flows.
- **Local image processing** handles Guidance/Fusion masks, optional pixel blending, full-resolution export, ICC profiles, and EXIF metadata.
- **The React workbench** provides asset management, a PS-style mask brush, prompt inspection, raw-candidate review, a generation timeline, and optional Fusion.

```text
source photo + pet references + Guidance Mask + natural-language intent
                              ↓
                multimodal Prompt Refiner
                              ↓
                   GPT Image 2 candidates
                              ↓
                  independent raw Critic
                              ↓
          deterministic ranking + historical winner
                              ↓
     accept, revise from a selected raw reference, or continue
                              ↓
              optional Fusion Mask and export
```

Search is **raw-first**. The provider's raw candidate is authoritative for the Critic, ranking, historical Global Winner, human review, and prompt refinement. Automatic rounds always rebase on the immutable source and never feed a previous candidate back as an image input. When a user explicitly selects a current-round candidate, that raw image may be supplied only as a visual reference for the next prompt revision; the immutable source remains image 0 and the Guidance Mask remains fixed.

Fusion and Local Fix are separate operations and never feed back into Search.

## Repository status

This is a greenfield implementation; it does not import the former ComfyUI/depth/matting prototype. The current MVP includes:

- content-addressed image assets and immutable source manifests;
- explicit LangGraph Search, Prompt Refiner, Critic, Planner, and Local Fix graphs;
- deterministic candidate ranking, historical Global Winner tracking, and stop policies;
- SQLite business persistence and LangGraph checkpoints containing references rather than image bytes;
- REST and SSE APIs with idempotent paid/external side effects;
- a React/TypeScript editing workbench with local Guidance and Fusion brushes;
- raw-candidate review with all eight Critic dimensions, structured issue evidence, human-decision context, prompt lineage, and a photo timeline whose nodes remain the sole candidate selector; ambiguous score scales and score/verdict contradictions are surfaced and excluded from automatic ranking, while the live Critic receives a source/raw comparison sheet for framing and global-rendering drift checks;
- full-resolution PNG/JPEG export with best-effort ICC and EXIF preservation.

## Stack

```text
frontend/   React, TypeScript, Vite, TanStack Query, React Flow
backend/    Python 3.12+, FastAPI, Pydantic, Pillow
graphs/     LangGraph StateGraph workflows
storage/    SQLite plus content-addressed local assets
providers/  Official OpenAI Python SDK
```

## Quick start

Requirements:

- [uv](https://docs.astral.sh/uv/) and Python 3.12+;
- Node.js 20.19+ or 22.12+;
- pnpm.

Install dependencies:

```bash
cp .env.example .env

cd backend
uv sync --locked --dev

cd ../frontend
pnpm install --frozen-lockfile

cd ..
```

Start the API, worker, and frontend from the repository root:

```bash
./scripts/dev.sh
```

Default endpoints:

- Workbench: `http://127.0.0.1:5173`
- FastAPI: `http://127.0.0.1:8000`
- OpenAPI: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/api/v1/health`

## Tests

Run the complete deterministic test suite:

```bash
./scripts/test.sh
```

It runs Ruff, mypy, pytest, TypeScript type checking, Vitest, and the Vite production build. The harness forces fake providers and clears live credentials, so normal tests cannot trigger paid API calls.

Generate coverage reports and enforce the configured thresholds with:

```bash
./scripts/coverage.sh
```

## Provider configuration

Safe defaults in `.env.example` keep every external provider disabled:

```dotenv
FAKE_GENERATOR=1
FAKE_CRITIC=1
FAKE_PROMPT_REFINER=1
RUN_OPENAI_LIVE_TESTS=0
RUN_INLINE=0
OPENAI_API_KEY=
OPENAI_BASE_URL=
```

Set the relevant `FAKE_*` switch to `0` to enable its official SDK path. `OPENAI_BASE_URL` is optional and backend-only. A compatible relay must independently support Image edits and Responses Structured Outputs; support for one endpoint does not imply support for the others.

Never commit `.env`. Local environment files, databases, generated assets, coverage output, and build output are ignored by Git.

See [QA and live smoke testing](docs/QA_AND_LIVE_SMOKE.md) before using a real credential.
The current implementation and verification evidence are tracked in the
[capability matrix](docs/CAPABILITIES.md).

## Main API surface

All business endpoints use `/api/v1`:

- `POST /projects` — create a project and upload source/reference images;
- `POST /projects/{project_id}/guidance-masks` — register a Guidance Mask;
- `POST /projects/{project_id}/searches` — start an idempotent search;
- `GET /searches/{search_id}` — retrieve state, candidates, Critic results, and prompt history;
- `GET /searches/{search_id}/events` — replayable SSE timeline;
- `POST /searches/{search_id}/resume` — accept, continue with feedback, or cancel;
- `POST /searches/{search_id}/fusion-masks` and `/fusions` — optional local blending;
- `POST /searches/{search_id}/local-fixes` — isolated Local Fix with maximum depth 2;
- `POST /searches/{search_id}/export` — full-resolution PNG/JPEG delivery;
- `GET /assets/{asset_id}` — validated image assets.

## Documentation

Read these documents in order when contributing:

1. [Project overview](docs/PROJECT_OVERVIEW.md)
2. [Codex task](CODEX_TASK.md)
3. [GPT Image 2 + LangGraph implementation guide](docs/CODEX_GPT_IMAGE_2_LANGGRAPH_GREENFIELD_IMPLEMENTATION_GUIDE.md)
4. [Repository agent rules](AGENTS.md)

## Current limitations

- Fake Generator, Critic, and Prompt Refiner modes remain the safe defaults. On 2026-08-21, a real single-candidate, single-round smoke passed through the locally configured OpenAI-compatible endpoint for Prompt Refiner, Image edits, and Critic. This does not validate direct `api.openai.com` access, multi-round search, or photographic quality.
- The Feedback Planner is deterministic and offline; it does not yet use a live LLM provider.
- Local Fix has a backend API but no complete frontend workflow.
- Production authentication, object storage, distributed queues, and cross-host coordination are not implemented.
- This is not a complete Photoshop replacement, a node-based general AI workflow editor, or a video compositor.

The architectural invariants remain mandatory: immutable-source rebase, raw-first review, historical-best winner tracking, bounded automatic regeneration, idempotent side effects, reference-only checkpoints, and Local Fix depth no greater than 2.
