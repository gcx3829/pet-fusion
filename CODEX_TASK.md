# Codex task: build Pet Fusion from scratch

Work in the current repository, intended to live at:

```text
/Users/cxg/gitWorkspace/pet-fusion
```

This is a **greenfield rewrite**. Do not modify the previous Cat Travel Compositor repository and do not import its old source tree.

## Read first

Read these documents in full before implementing:

1. `README.md`
2. `docs/PROJECT_OVERVIEW.md`
3. `AGENTS.md`
4. `docs/CODEX_GPT_IMAGE_2_LANGGRAPH_GREENFIELD_IMPLEMENTATION_GUIDE.md`

The project overview explains the original photography-pipeline idea and why the architecture changed. The implementation guide is the authoritative engineering specification.

## Primary task

Build a runnable MVP in this repository with:

- React + TypeScript placement and candidate-review UI;
- FastAPI project, asset, search, local-fix and export APIs;
- direct GPT Image 2 multi-reference generation;
- LangGraph Critic and Feedback Planner subgraphs;
- deterministic candidate ranking and historical Global Winner;
- immutable-source rebase on every automatic search round;
- checkpoint persistence, interrupt/resume and idempotent provider calls;
- optional user-authored Fusion Mask compositing and background protection;
- full-resolution export with ICC/EXIF preservation where technically possible;
- complete backend tests for the architectural invariants.

## First implementation milestone

Start with the repository scaffold and one thin vertical slice:

```text
create project
→ upload source and reference assets
→ save immutable source manifest
→ submit one mocked generation round
→ persist LangGraph state
→ return candidates through API/SSE
→ render them in the frontend
```

Use provider interfaces and test doubles before making the live OpenAI path mandatory. After the vertical slice passes, implement the real generator, Critic, Ranker, Planner and stop policy in the staged order defined by the guide.

## Hard constraints

- Do not use a generic ReAct/prebuilt agent in place of the explicit StateGraph.
- Do not use ComfyUI or local diffusion models in the active path.
- Do not feed a previous candidate into the next automatic generation round.
- Do not let LLM output directly decide candidate ranking or stop conditions without deterministic validation.
- Do not use JPEG/WebP as an internal generation lineage format.
- The model Guidance Mask is not a pixel lock. Search/Critic/user review must use
  raw candidates; only an explicit user Fusion Mask may perform final local
  compositing, and it must not feed back into Search or Critic.
- Do not put image bytes or Base64 in LangGraph checkpoint state.
- Do not expose the OpenAI API key to the browser.
- Do not copy Nodaro source-available/enterprise code or proprietary prompts.

## Delivery expectations

Implement code, not another plan. Keep commits or logical milestones small and testable. Update the README as startup commands become real. Run all available tests before reporting completion.
