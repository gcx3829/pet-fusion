import { beforeEach, describe, expect, it, vi } from "vitest";
import { createProject, getSearch, startSearch } from "../src/lib/api";
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
      catName: "栗子",
      catTraits: "白色口鼻",
    };

    const project = await createProject(draft);

    expect(project.project_id).toBe("project-01");
    expect(project.source_manifest?.manifest_hash).toBe("abc123");
    const [, init] = fetchMock.mock.calls[0];
    const form = init?.body as FormData;
    expect(form.get("background")).toBe(draft.background);
    expect(form.getAll("cat_references")).toEqual(draft.references);
    expect(form.get("cat_name")).toBe("栗子");
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
        generation_depth: 0,
        model: "fake-gpt-image-2",
      }],
      global_winner_id: null,
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
