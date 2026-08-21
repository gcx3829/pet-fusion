import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FusionEditor } from "../src/features/fusion/FusionEditor";
import { createFusion, uploadFusionMask } from "../src/lib/api";
import type { PlacementIntent, SearchSnapshot } from "../src/types";

vi.mock("../src/lib/api", () => ({
  createFusion: vi.fn(),
  uploadFusionMask: vi.fn(),
}));

const placement: PlacementIntent = {
  x: 0.6,
  y: 0.5,
  width: 0.2,
  height: 0.3,
  coordinate_space: "normalized",
  pose: "sitting",
  facing: "camera",
  contact_surface: null,
};

function snapshot(status: SearchSnapshot["status"] = "accepted"): SearchSnapshot {
  return {
    search_id: "search-a",
    status,
    round_index: 0,
    candidates: [
      {
        candidate_id: "candidate-selected",
        round_index: 0,
        variant_index: 0,
        image_url: "/api/v1/assets/raw-a",
        score: 88,
        is_round_winner: false,
        is_global_winner: false,
      },
      {
        candidate_id: "candidate-winner",
        round_index: 0,
        variant_index: 1,
        image_url: "/api/v1/assets/raw-b",
        score: 92,
        is_round_winner: true,
        is_global_winner: true,
      },
    ],
    global_winner_id: "candidate-winner",
    global_winner_score: 92,
    prompt_history: [],
    active_directives: [],
  };
}

describe("FusionEditor", () => {
  beforeEach(() => {
    vi.mocked(createFusion).mockResolvedValue({
      fusion_key: "f".repeat(64),
      search_id: "search-a",
      candidate_id: "candidate-selected",
      source_manifest_hash: "a".repeat(64),
      raw_asset: { asset_id: "raw-a", asset_url: "/api/v1/assets/raw-a" },
      fusion_asset: { asset_id: "fused-a", asset_url: "/api/v1/assets/fused-a" },
      mask_asset: { asset_id: "mask-a", asset_url: "/api/v1/assets/mask-a" },
      feather_radius_px: 8,
      box: { x: 0.6, y: 0.5, width: 0.2, height: 0.3 },
    });
    vi.mocked(uploadFusionMask).mockReset();
  });

  it("未接受搜索时锁定 Fusion", () => {
    render(<FusionEditor snapshot={snapshot("waiting_for_human")} placement={placement} />);

    expect(screen.getByText(/接受一张候选图后/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /生成融合预览/ })).not.toBeInTheDocument();
  });

  it("优先融合有效的人工所选候选，否则使用 Global Winner", async () => {
    const user = userEvent.setup();
    const view = render(
      <FusionEditor
        snapshot={snapshot()}
        selectedCandidateId="candidate-selected"
        placement={placement}
      />,
    );
    await user.click(screen.getByRole("button", { name: /生成融合预览/ }));
    await waitFor(() => expect(createFusion).toHaveBeenCalledWith(
      "search-a",
      expect.objectContaining({ candidate_id: "candidate-selected" }),
    ));

    vi.mocked(createFusion).mockClear();
    view.rerender(
      <FusionEditor
        snapshot={snapshot()}
        selectedCandidateId="candidate-missing"
        placement={placement}
      />,
    );
    await user.click(screen.getByRole("button", { name: /生成融合预览/ }));
    await waitFor(() => expect(createFusion).toHaveBeenCalledWith(
      "search-a",
      expect.objectContaining({ candidate_id: "candidate-winner" }),
    ));
  });

  it("矩形越界时在本地阻止请求", async () => {
    const user = userEvent.setup();
    render(<FusionEditor snapshot={snapshot()} placement={placement} />);

    const xInput = screen.getByLabelText("X");
    await user.clear(xInput);
    await user.type(xInput, "0.9");

    expect(screen.getByRole("alert")).toHaveTextContent("矩形选区超出原片边界");
    expect(screen.getByRole("button", { name: /生成融合预览/ })).toBeDisabled();
    expect(createFusion).not.toHaveBeenCalled();
  });
});
