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
- Search/Critic/user review are raw-first. Background protection is an optional,
  user-triggered Fusion Mask operation; it must not be applied automatically to
  Search candidates or used as the Critic's image.
- The automatic Critic/Planner loop remains source-only: it may apply bounded
  local directives, but it never feeds a previous candidate into the next
  generation. If a user explicitly selects a current-round raw candidate and
  continues, that raw asset may be passed as image[1] as a visual reference
  only; image[0] and the Guidance Mask remain the immutable source/base. This
  is a `candidate_anchored_rebase`, not candidate editing. Initial and human
  revision prompts are produced by the multimodal Prompt Refiner; Fusion and
  Local Fix stay outside Search.
- Local Fix is separate from Search and has maximum generation depth 2.
- The OpenAI API key stays on the backend.
- Use the official OpenAI API directly.

## Current Raw-first product decision

The GPT Image 2 Guidance Mask remains part of every Search generation request.
It tells the model where to focus, but it is not a pixel lock. Search stores the
raw provider output as the authoritative candidate: Critic, Ranker, Global Winner,
human review, and prompt rebase decisions all use that raw asset. A Fusion Mask
is an optional user-authored rectangle/alpha selection with feathering applied
only for a final preview or export. Legacy `protected_asset`/`composite` fields
remain readable for Local Fix and old SQLite rows, but are not the default Search
source of truth.

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
