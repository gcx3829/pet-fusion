import { forwardRef, useImperativeHandle } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createFusion, uploadFusionMask } from "../src/lib/api";
import { FusionEditor, type FusionEditorState } from "../src/features/fusion/FusionEditor";
import { renderLocalFusion } from "../src/features/fusion/renderLocalFusion";
import type { MaskBrushEditorHandle } from "../src/features/mask/MaskBrushEditor";
import type { PlacementIntent, SearchSnapshot } from "../src/types";

vi.mock("../src/lib/api", () => ({
  createFusion: vi.fn(),
  uploadFusionMask: vi.fn(),
}));
vi.mock("../src/features/fusion/renderLocalFusion", () => ({
  renderLocalFusion: vi.fn(),
}));

let brushProps: Record<string, unknown> | null = null;
vi.mock("../src/features/mask/MaskBrushEditor", () => ({
  MaskBrushEditor: forwardRef<MaskBrushEditorHandle, Record<string, unknown>>((props, ref) => {
    brushProps = props;
    useImperativeHandle(ref, () => ({
      getDocument: () => ({
        version: 1,
        width: Number(props.width),
        height: Number(props.height),
        strokes: [{
          tool: "paint",
          points: [{ x: 0.5, y: 0.5 }],
          settings: { size: 20, flow: 1, feather: 0.2 },
        }],
      }),
      exportMaskPng: async () => new Blob(["mask"], { type: "image/png" }),
      exportMaskFile: async () => new File(["mask"], "fusion-mask.png", { type: "image/png" }),
      undo: vi.fn(),
      redo: vi.fn(),
      clear: vi.fn(),
      reset: vi.fn(),
    }));
    return <div data-testid="brush-editor">本地画布</div>;
  }),
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

const mapping = {
  schema_version: "crop-mapping/v1" as const,
  full_width: 400,
  full_height: 300,
  crop_box: { x: 100, y: 75, width: 200, height: 150 },
  canvas_width: 120,
  canvas_height: 100,
  padding: { left: 10, top: 5, right: 10, bottom: 5 },
};

function snapshot(): SearchSnapshot {
  return {
    search_id: "search-brush",
    status: "accepted",
    round_index: 0,
    candidates: [{
      candidate_id: "candidate-brush",
      round_index: 0,
      variant_index: 0,
      image_url: "/api/v1/assets/raw",
      raw_image_url: "/api/v1/assets/raw",
      raw_width: 120,
      raw_height: 100,
      crop_mapping: mapping,
      is_round_winner: true,
      is_global_winner: true,
    }],
    global_winner_id: "candidate-brush",
    global_winner_score: 91,
    prompt_history: [],
    active_directives: [],
  };
}

describe("FusionEditor brush mode", () => {
  beforeEach(() => {
    brushProps = null;
    vi.mocked(renderLocalFusion).mockResolvedValue(new Blob(["local-fusion"], { type: "image/png" }));
    vi.mocked(uploadFusionMask).mockResolvedValue({
      search_id: "search-brush",
      source_manifest_hash: "a".repeat(64),
      asset: { asset_id: "mask-upload", asset_url: "/api/v1/assets/mask-upload" },
    });
    vi.mocked(createFusion).mockResolvedValue({
      fusion_key: "f".repeat(64),
      search_id: "search-brush",
      candidate_id: "candidate-brush",
      source_manifest_hash: "a".repeat(64),
      raw_asset: { asset_id: "raw", asset_url: "/api/v1/assets/raw" },
      fusion_asset: { asset_id: "fused", asset_url: "/api/v1/assets/fused" },
      mask_asset: { asset_id: "mask", asset_url: "/api/v1/assets/mask" },
      feather_radius_px: 0,
    });
  });

  it("demo 模式在浏览器本地合成，不上传 mask 或调用后端 Fusion", async () => {
    const user = userEvent.setup();
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:fusion-demo-result");
    render(
      <FusionEditor
        demoMode
        snapshot={snapshot()}
        placement={placement}
        backgroundSrc="/mock/fusion-source.svg"
        backgroundWidth={400}
        backgroundHeight={300}
      />,
    );

    await user.click(screen.getByRole("button", { name: /生成融合预览/ }));

    await waitFor(() => expect(renderLocalFusion).toHaveBeenCalledTimes(1));
    expect(renderLocalFusion).toHaveBeenCalledWith(expect.objectContaining({
      originalSrc: "/mock/fusion-source.svg",
      generatedSrc: "/api/v1/assets/raw",
      width: 400,
      height: 300,
      mask: expect.any(File),
    }));
    expect(uploadFusionMask).not.toHaveBeenCalled();
    expect(createFusion).not.toHaveBeenCalled();
    expect(await screen.findByAltText("局部融合预览")).toHaveAttribute("src", "blob:fusion-demo-result");
  });

  it("从 Timeline 离开再返回 Fusion 时恢复已完成的最终图而不是空白画布", async () => {
    const user = userEvent.setup();
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:fusion-restored-result");
    let latestState: FusionEditorState | undefined;
    const firstView = render(
      <FusionEditor
        demoMode
        snapshot={snapshot()}
        placement={placement}
        backgroundSrc="/mock/fusion-source.svg"
        backgroundWidth={400}
        backgroundHeight={300}
        onStateChange={(state) => { latestState = state; }}
      />,
    );
    await user.click(screen.getByRole("button", { name: /生成融合预览/ }));
    await waitFor(() => expect(latestState?.result?.fusion_asset.asset_url).toBe("blob:fusion-restored-result"));
    const restoredResult = latestState?.result;
    firstView.unmount();

    render(
      <FusionEditor
        demoMode
        restoredResult={restoredResult}
        snapshot={snapshot()}
        placement={placement}
        backgroundSrc="/mock/fusion-source.svg"
        backgroundWidth={400}
        backgroundHeight={300}
      />,
    );

    expect(await screen.findByAltText("局部融合预览")).toHaveAttribute("src", "blob:fusion-restored-result");
    expect(screen.queryByTestId("brush-editor")).not.toBeInTheDocument();
  });

  it("笔刷保存只在点击提交时发出 upload + create 两个请求，并固定服务端羽化为 0", async () => {
    const user = userEvent.setup();
    render(
      <FusionEditor
        snapshot={snapshot()}
        placement={placement}
        backgroundSrc="/api/v1/assets/background"
        backgroundWidth={400}
        backgroundHeight={300}
      />,
    );

    expect(screen.getByTestId("brush-editor")).toBeInTheDocument();
    expect(brushProps?.generatedCropMapping).toEqual(mapping);
    expect(uploadFusionMask).not.toHaveBeenCalled();
    expect(createFusion).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /生成融合预览/ }));
    await waitFor(() => expect(createFusion).toHaveBeenCalledTimes(1));
    expect(uploadFusionMask).toHaveBeenCalledTimes(1);
    expect(createFusion).toHaveBeenCalledWith("search-brush", {
      candidate_id: "candidate-brush",
      mask_asset_id: "mask-upload",
      box: undefined,
      feather_radius_px: 0,
    });
  });

  it("候选切换后丢弃旧候选的延迟 Fusion 结果", async () => {
    const user = userEvent.setup();
    let resolveUpload: ((value: Awaited<ReturnType<typeof uploadFusionMask>>) => void) | undefined;
    vi.mocked(uploadFusionMask).mockImplementationOnce(() => new Promise((resolve) => {
      resolveUpload = resolve;
    }));
    const first = snapshot();
    first.candidates.push({
      ...first.candidates[0],
      candidate_id: "candidate-second",
      raw_image_url: "/api/v1/assets/raw-second",
      raw_asset_url: "/api/v1/assets/raw-second",
      is_global_winner: false,
    });
    const view = render(
      <FusionEditor
        snapshot={first}
        selectedCandidateId="candidate-brush"
        placement={placement}
        backgroundSrc="/api/v1/assets/background"
        backgroundWidth={400}
        backgroundHeight={300}
      />,
    );

    await user.click(screen.getByRole("button", { name: /生成融合预览/ }));
    expect(screen.getByRole("button", { name: /融合中/ })).toBeDisabled();

    view.rerender(
      <FusionEditor
        snapshot={first}
        selectedCandidateId="candidate-second"
        placement={placement}
        backgroundSrc="/api/v1/assets/background"
        backgroundWidth={400}
        backgroundHeight={300}
      />,
    );
    await waitFor(() => expect(screen.getByText("candidate-second")).toBeInTheDocument());
    resolveUpload?.({
      search_id: "search-brush",
      source_manifest_hash: "a".repeat(64),
      asset: { asset_id: "mask-old", asset_url: "/api/v1/assets/mask-old" },
    });

    await waitFor(() => expect(uploadFusionMask).toHaveBeenCalledTimes(1));
    expect(createFusion).not.toHaveBeenCalled();
    expect(screen.queryByAltText("局部融合预览")).not.toBeInTheDocument();
  });
});
