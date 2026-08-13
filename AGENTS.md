# AGENTS.md

## Repository purpose

This repository is the greenfield implementation of **Pet Fusion**, a photography-first pet compositing workbench.

The target working directory is:

```text
/Users/cxg/gitWorkspace/pet-fusion
```

## Required reading order

Before changing code, read these files in full:

1. `README.md`
2. `docs/PROJECT_OVERVIEW.md`
3. `CODEX_TASK.md`
4. `docs/CODEX_GPT_IMAGE_2_LANGGRAPH_GREENFIELD_IMPLEMENTATION_GUIDE.md`

## Greenfield rule

Do not import or copy the old `cat-travel-compositor-mvp` implementation. It was a disposable validation prototype built around ComfyUI, matting, depth estimation, deterministic compositing, and a static frontend. The new repository starts clean.

General ideas may be retained, especially crop mapping, composite-floor protection, asset hashing, and high-resolution export. Reimplement them against the new architecture and tests.

## Non-negotiable architecture

- GPT Image 2 is the primary visual generator.
- Critic and feedback planning are explicit LangGraph subgraphs.
- The search graph uses immutable source assets and rebases every automatic round.
- Previous candidates never become automatic next-round image inputs.
- Global Winner means historical best, not last output.
- Only blocking issues can trigger automatic regeneration.
- All paid or externally visible side effects are idempotent.
- LangGraph checkpoints contain references and structured state, not image bytes.
- Background protection is enforced locally with a composite floor.
- Local Fix is separate from Search and has maximum generation depth 2.
- The OpenAI API key stays on the backend.
- Use the official OpenAI API directly.

## Default stack

- Backend: Python 3.12+, FastAPI, Pydantic, LangGraph, SQLite
- Frontend: React, TypeScript, Vite
- Images: Pillow, NumPy, OpenCV only where useful
- Tests: pytest for backend, Vitest for frontend, Playwright only after the core flow is stable

## Implementation style

- Prefer explicit domain types over loosely shaped dictionaries.
- Keep LangGraph node functions thin; put provider and image logic in services.
- Keep deterministic policies outside LLM prompts.
- Use stable asset IDs, request keys, prompt versions, and schema versions.
- Do not log API keys, image Base64, or complete sensitive prompts by default.
- Add tests with each milestone; do not postpone all tests to the end.
- Keep the repository runnable at each commit.

## Completion reporting

When reporting work, include:

- implemented milestone;
- files changed;
- commands run;
- test results;
- remaining limitations;
- any deviation from the implementation guide and the reason.
