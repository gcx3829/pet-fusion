import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createFusion,
  createProject,
  getSearch,
  normalizePromptHistory,
  resumeSearch,
  startSearch,
  uploadFusionMask,
} from "../src/lib/api";
import type { PlacementIntent, SourceDraft } from "../src/types";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const placement: PlacementIntent = {
  x: 0.58,
  y: 0.61,
  width: 0.18,
  height: 0.29,
  coordinate_space: "normalized",
  pose: "sitting",
  facing: "slightly_left",
  contact_surface: "stone pavement",
};

describe("API client", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

  it("以重复 cat_references 字段创建不可变项目", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      project_id: "project-01",
      source_manifest: {
        manifest_hash: "abc123",
        background: { asset_id: "bg", asset_url: "/api/v1/assets/bg" },
        cat_references: [
          { asset_id: "ref-1", asset_url: "/api/v1/assets/ref-1" },
          { asset_id: "ref-2", asset_url: "/api/v1/assets/ref-2" },
        ],
      },
    }, 201));
    const draft: SourceDraft = {
      background: new File(["background"], "travel.png", { type: "image/png" }),
      references: [
        new File(["one"], "cat-1.png", { type: "image/png" }),
        new File(["two"], "cat-2.png", { type: "image/png" }),
      ],
    };

    const project = await createProject(draft);

    expect(project.project_id).toBe("project-01");
    expect(project.source_manifest?.manifest_hash).toBe("abc123");
    const [, init] = fetchMock.mock.calls[0];
    const form = init?.body as FormData;
    expect(form.get("background")).toBe(draft.background);
    expect(form.getAll("cat_references")).toEqual(draft.references);
    expect(form.get("cat_name")).toBeNull();
  });

  it("只发送后端搜索 schema 允许的控制字段", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      search_id: "search-01",
      thread_id: "search-01",
      status: "queued",
      events_url: "/api/v1/searches/search-01/events",
    }, 201));

    await startSearch("project-01", placement, "自然坐在这里", {
      candidate_count: 3,
      max_rounds: 2,
      budget_usd: 1.5,
      review_each_round: false,
    }, "stable-web-request");

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get("Idempotency-Key")).toBe("stable-web-request");
    expect(JSON.parse(String(init?.body))).toEqual({
      placement,
      user_intent: "自然坐在这里",
      candidate_count: 3,
      max_rounds: 2,
      budget_usd: 1.5,
      review_each_round: false,
    });
  });

  it("有自定义 Guidance Mask 时才把已注册资产 ID 放进 Search payload", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      search_id: "search-guidance",
      thread_id: "search-guidance",
      status: "queued",
      guidance_mask_asset_id: "ast-guidance",
      events_url: "/api/v1/searches/search-guidance/events",
    }, 201));

    await startSearch("project-01", placement, "自然坐在这里", {
      candidate_count: 3,
      max_rounds: 2,
      budget_usd: 1.5,
      review_each_round: false,
    }, "stable-guidance-request", "ast-guidance");

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      guidance_mask_asset_id: "ast-guidance",
    });
  });

  it("上传 Guidance Mask 只在本地 PNG 校验通过后发起 project-scoped 请求", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      project_id: "project-01",
      source_manifest_hash: "a".repeat(64),
      asset: { asset_id: "ast-guidance", asset_url: "/api/v1/assets/ast-guidance" },
    }, 201));

    const { uploadGuidanceMask } = await import("../src/lib/api");
    const registration = await uploadGuidanceMask(
      "project-01",
      new File(["mask"], "guidance.png", { type: "image/png" }),
    );

    expect(registration.asset.asset_id).toBe("ast-guidance");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/projects/project-01/guidance-masks");
    expect((fetchMock.mock.calls[0][1]?.body as FormData).get("mask")).toBeInstanceOf(File);
  });

  it("将后端 asset_url 候选规范为 Gallery 可用的数据", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({
      search_id: "search-01",
      status: "waiting_for_human",
      round_index: 0,
      candidates: [{
        candidate_id: "candidate-01",
        round_index: 0,
        variant_index: 1,
        asset_id: "asset-01",
        asset_url: "/api/v1/assets/asset-01",
        raw_width: 120,
        raw_height: 100,
        crop_mapping: {
          schema_version: "crop-mapping/v1",
          full_width: 400,
          full_height: 300,
          crop_box: { x: 100, y: 75, width: 200, height: 150 },
          canvas_width: 120,
          canvas_height: 100,
          padding: { left: 10, top: 5, right: 10, bottom: 5 },
        },
        generation_depth: 0,
        model: "fake-gpt-image-2",
      }],
      global_winner_id: null,
      prompt_history: [{
        round_index: 0,
        canonical_prompt: "基准 prompt",
        canonical_prompt_hash: "a".repeat(64),
        generation_prompt: "初始生成 prompt",
        generation_prompt_hash: "b".repeat(64),
        active_directives: [],
        tuned: false,
      }],
      active_directives: [],
      stop_reason: "mock_round_complete",
    }));

    const snapshot = await getSearch("search-01");

    expect(snapshot.status).toBe("waiting_for_human");
    expect(snapshot.candidates[0]).toMatchObject({
      candidate_id: "candidate-01",
      image_url: "/api/v1/assets/asset-01",
      model: "fake-gpt-image-2",
      is_global_winner: false,
      raw_width: 120,
      raw_height: 100,
      crop_mapping: {
        schema_version: "crop-mapping/v1",
        full_width: 400,
        full_height: 300,
        crop_box: { x: 100, y: 75, width: 200, height: 150 },
        canvas_width: 120,
        canvas_height: 100,
        padding: { left: 10, top: 5, right: 10, bottom: 5 },
      },
    });
    expect(snapshot.prompt_history).toEqual([expect.objectContaining({
      round_index: 0,
      generation_prompt: "初始生成 prompt",
      tuned: false,
    })]);
  });

  it("归一化新 PromptVersion 字段并保留 visual anchor 与专业计划", () => {
    const history = normalizePromptHistory([{
      round_index: 1,
      canonical_prompt: "原图场景",
      generation_prompt: "基于所选 Raw 缩小主体",
      prompt_version_id: "pv_revision",
      prompt_version_hash: "a".repeat(64),
      based_on_prompt_version_id: "pv_initial",
      refinement_mode: "revision",
      generation_mode: "candidate_anchored_rebase",
      prompt_model: "gpt-5.6-luna",
      generation_model: "gpt-image-2",
      prompt_schema_version: "professional-prompt-plan/v1",
      prompt_template_version: "multimodal-prompt-refiner/v1",
      professional_prompt_plan: {
        role_of_inputs: ["Image 1 是底片"],
        task: "加入同一只猫",
        identity_invariants: ["保留眼睛和毛色"],
        change_from_anchor: ["主体缩小"],
        ignored_reasoning: { secret: "不要显示" },
      },
      visual_anchor: {
        candidate_id: "candidate-raw",
        round_index: 0,
        raw_asset: {
          asset_id: "ast-raw",
          asset_url: "/api/v1/assets/ast-raw",
          mime_type: "image/png",
        },
      },
      active_directives: [],
      tuned: true,
    }]);

    expect(history[0]).toMatchObject({
      prompt_version_id: "pv_revision",
      prompt_version_hash: "a".repeat(64),
      version_hash: "a".repeat(64),
      based_on_prompt_version_id: "pv_initial",
      parent_prompt_version_id: "pv_initial",
      refinement_mode: "revision",
      generation_mode: "candidate_anchored_rebase",
      prompt_model: "gpt-5.6-luna",
      professional_prompt_plan: {
        task: "加入同一只猫",
        identity_invariants: ["保留眼睛和毛色"],
      },
      visual_anchor: {
        candidate_id: "candidate-raw",
        raw_asset_id: "ast-raw",
        raw_asset_url: "/api/v1/assets/ast-raw",
      },
    });
    expect(history[0].professional_prompt_plan).not.toHaveProperty("ignored_reasoning");
  });

  it("旧 Prompt 记录缺少新字段时安全降级，错误对象不会被字符串化", () => {
    const history = normalizePromptHistory([{
      round_index: 0,
      prompt: "兼容旧 prompt",
      active_directives: [{ instruction: { malicious: true } }],
    }]);

    expect(history).toHaveLength(1);
    expect(history[0]).toMatchObject({
      canonical_prompt: "兼容旧 prompt",
      generation_prompt: "兼容旧 prompt",
      refinement_mode: "initial",
      generation_mode: "source_rebase",
      active_directives: [],
    });
    expect(JSON.stringify(history)).not.toContain("[object Object]");
  });

  it("不把 PromptVersion envelope schema 误标为专业计划 schema", () => {
    const [entry] = normalizePromptHistory([{
      schema_version: "prompt-version/v1",
      round_index: 0,
      canonical_prompt: "基准 prompt",
      generation_prompt: "生成 prompt",
      active_directives: [],
    }]);

    expect(entry.prompt_schema_version).toBeUndefined();
  });

  it("限制异常 Prompt 历史和结构化数组的渲染规模", () => {
    const history = normalizePromptHistory(Array.from({ length: 100 }, (_, roundIndex) => ({
      round_index: roundIndex,
      canonical_prompt: `canonical-${roundIndex}`,
      generation_prompt: `generation-${roundIndex}`,
      professional_prompt_plan: {
        task: "加入同一只猫",
        output: "真实照片",
        identity_invariants: Array.from({ length: 80 }, (_, index) => `identity-${index}`),
      },
      active_directives: [],
    })));

    expect(history).toHaveLength(64);
    expect(history[0].professional_prompt_plan?.identity_invariants).toHaveLength(24);
  });

  it("候选同时包含 raw 与 protected 资产时始终选择 raw 审片图", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({
      search_id: "search-raw",
      status: "waiting_for_human",
      round_index: 1,
      candidates: [{
        candidate_id: "candidate-raw",
        round_index: 1,
        variant_index: 0,
        raw_asset_id: "raw-asset",
        raw_asset_url: "/api/v1/assets/raw-asset",
        raw_image_url: "/api/v1/assets/raw-image-alias",
        protected_asset_id: "protected-asset",
        protected_asset_url: "/api/v1/assets/protected-asset",
        asset_url: "/api/v1/assets/generic-alias",
      }],
      prompt_history: [],
      active_directives: [],
    }));

    const snapshot = await getSearch("search-raw");

    expect(snapshot.candidates[0]).toMatchObject({
      image_url: "/api/v1/assets/raw-asset",
      raw_image_url: "/api/v1/assets/raw-asset",
      raw_asset_id: "raw-asset",
      protected_asset_id: "protected-asset",
      review_asset_kind: "raw",
    });
  });

  it("旧 protected URL 只作为兼容回退且不会被误当作 asset ID", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({
      search_id: "search-legacy",
      status: "waiting_for_human",
      candidates: [{
        candidate_id: "candidate-legacy",
        round_index: 0,
        variant_index: 0,
        protected_image_url: "/api/v1/assets/legacy-protected",
      }],
      prompt_history: [],
      active_directives: [],
    }));

    const snapshot = await getSearch("search-legacy");

    expect(snapshot.candidates[0].image_url).toBe("/api/v1/assets/legacy-protected");
  });

  it("人工接受只按 action 提交权威候选 ID", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async () => jsonResponse({ status: "accepted" }));

    await resumeSearch("search-01", "accept_candidate", "candidate-raw");
    await resumeSearch("search-01", "accept_global_winner", "ignored-candidate");

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      action: "accept_candidate",
      updated_user_intent: null,
      selected_candidate_id: "candidate-raw",
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      action: "accept_global_winner",
      updated_user_intent: null,
    });
  });

  it("继续搜索时提交所选候选与人工反馈，并立即返回新的状态", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      search_id: "search-01",
      status: "queued",
      round_index: 1,
      candidates: [],
      prompt_history: [],
      active_directives: [],
    }));

    const snapshot = await resumeSearch(
      "search-01",
      "continue_one_round",
      "candidate-raw",
      "猫再小一点，保留当前眼睛和毛色。",
      0,
    );

    expect(snapshot.status).toBe("queued");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      action: "continue_one_round",
      updated_user_intent: null,
      selected_candidate_id: "candidate-raw",
      human_feedback: "猫再小一点，保留当前眼睛和毛色。",
      reviewed_round_index: 0,
    });
  });

  it("没有 Timeline 候选时继续搜索显式省略 selected_candidate_id", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      search_id: "search-01",
      status: "queued",
      round_index: 1,
      candidates: [],
      prompt_history: [],
      active_directives: [],
    }));

    await resumeSearch("search-01", "continue_one_round", undefined, "只调整光线", 0);

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      action: "continue_one_round",
      updated_user_intent: null,
      human_feedback: "只调整光线",
      reviewed_round_index: 0,
    });
  });

  it("显示 FastAPI detail，而不是吞掉服务端错误", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ detail: "参考图数量必须为 1 到 5" }, 422));
    await expect(startSearch("project", placement, "意图", {
      candidate_count: 3,
      max_rounds: 3,
      budget_usd: 2,
      review_each_round: false,
    })).rejects.toThrow("参考图数量必须为 1 到 5");
  });

  it("上传并创建 Fusion 时保留 raw 与独立融合资产引用", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(jsonResponse({
        search_id: "search-fusion",
        source_manifest_hash: "a".repeat(64),
        asset: { asset_id: "ast-mask", asset_url: "/api/v1/assets/ast-mask" },
      }, 201))
      .mockResolvedValueOnce(jsonResponse({
        fusion_key: "b".repeat(64),
        search_id: "search-fusion",
        candidate_id: "cand-raw",
        source_manifest_hash: "a".repeat(64),
        raw_asset: { asset_id: "ast-raw", asset_url: "/api/v1/assets/ast-raw" },
        fusion_asset: { asset_id: "ast-fused", asset_url: "/api/v1/assets/ast-fused" },
        mask_asset: { asset_id: "ast-derived-mask", asset_url: "/api/v1/assets/ast-derived-mask" },
        input_mask_asset: { asset_id: "ast-mask", asset_url: "/api/v1/assets/ast-mask" },
        feather_radius_px: 8,
        box: null,
      }, 201));

    const registered = await uploadFusionMask(
      "search-fusion",
      new File(["mask"], "mask.png", { type: "image/png" }),
    );
    const result = await createFusion("search-fusion", {
      candidate_id: "cand-raw",
      mask_asset_id: registered.asset.asset_id,
      feather_radius_px: 8,
    });

    expect(registered.asset.asset_id).toBe("ast-mask");
    expect(result.raw_asset.asset_id).toBe("ast-raw");
    expect(result.fusion_asset.asset_id).toBe("ast-fused");
    const [, uploadInit] = fetchMock.mock.calls[0];
    expect((uploadInit?.body as FormData).get("mask")).toBeInstanceOf(File);
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
      candidate_id: "cand-raw",
      mask_asset_id: "ast-mask",
      feather_radius_px: 8,
    });
  });

  it("在发请求前拒绝非 PNG mask 与越界矩形", async () => {
    await expect(uploadFusionMask(
      "search-fusion",
      new File(["jpeg"], "mask.jpg", { type: "image/jpeg" }),
    )).rejects.toThrow("必须是带透明通道的 PNG 图片");
    await expect(createFusion("search-fusion", {
      box: { x: 0.9, y: 0.2, width: 0.2, height: 0.2 },
      feather_radius_px: 8,
    })).rejects.toThrow("完整位于原片内");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("422 校验错误显示首个字段详情", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({
      error: {
        code: "VALIDATION_FAILED",
        message: "Request validation failed",
        details: [{ field: "body.box", message: "fusion box out of bounds" }],
      },
    }, 422));

    await expect(createFusion("search-fusion", {
      box: { x: 0.2, y: 0.2, width: 0.2, height: 0.2 },
      feather_radius_px: 8,
    })).rejects.toThrow("body.box: fusion box out of bounds");
  });

  it("兼容统一 error envelope 的 message", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({
      error: { code: "invalid_asset", message: "图片无法解码", details: {} },
    }, 422));
    await expect(startSearch("project", placement, "意图", {
      candidate_count: 3,
      max_rounds: 3,
      budget_usd: 2,
      review_each_round: false,
    })).rejects.toThrow("图片无法解码");
  });
});
