import { createRef, useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { rawCandidateUrl } from "../src/lib/raw";
import { buildTimelineGraph } from "../src/features/timeline/buildTimelineGraph";
import { layoutTimelineGraph } from "../src/features/timeline/timelineLayout";
import { TimelineCanvas } from "../src/features/timeline/TimelineCanvas";
import { WorkerToolbar } from "../src/features/workbench/WorkerToolbar";
import { WorkerViewport } from "../src/features/workbench/WorkerViewport";
import type { GuidanceMaskEditorHandle } from "../src/features/placement/GuidanceMaskEditor";
import { CriticInspector } from "../src/features/review/CriticInspector";
import { RawCandidateViewer } from "../src/features/candidates/RawCandidateViewer";
import { useWorkbenchUi } from "../src/features/workbench/useWorkbenchUi";
import { shouldClearCandidateAfterResume } from "../src/features/review/selectionPolicy";
import type { SearchCandidate, SearchSnapshot } from "../src/types";

const candidate = (id: string, round = 0): SearchCandidate => ({
  candidate_id: id,
  round_index: round,
  variant_index: 0,
  image_url: `/legacy/${id}`,
  raw_image_url: `/raw/${id}`,
  raw_asset_url: `/raw-asset/${id}`,
  protected_asset_url: `/protected/${id}`,
  is_round_winner: true,
  is_global_winner: round === 0,
});

function snapshot(status: SearchSnapshot["status"] = "waiting_for_human"): SearchSnapshot {
  return {
    search_id: "search-graph",
    status,
    round_index: 1,
    candidates: [candidate("a", 0), candidate("b", 1)],
    global_winner_id: "a",
    prompt_history: [],
    active_directives: [],
  };
}

describe("workbench raw-first graph", () => {
  it("续跑期间保留人工选择，直到新一轮真正出现", () => {
    expect(shouldClearCandidateAfterResume("continue_one_round")).toBe(false);
    expect(shouldClearCandidateAfterResume("accept_candidate")).toBe(true);
    expect(shouldClearCandidateAfterResume("cancel")).toBe(true);
  });
  it("resolves raw fields before generic aliases and never protected fields", () => {
    expect(rawCandidateUrl(candidate("one"))).toBe("/raw/one");
    expect(rawCandidateUrl({ image_url: "/legacy/only" })).toBe("/legacy/only");
    expect(rawCandidateUrl({ protected_asset_url: "/protected/only", image_url: "" } as unknown as SearchCandidate)).toBe("");
  });

  it("只生成照片节点，并以 continuation 串联每轮选择", () => {
    const graph = buildTimelineGraph([], snapshot(), { sourceImageUrl: "/source.jpg", guidanceActive: true });
    expect(graph.nodes.every((node) => ["source", "candidate", "final"].includes(node.kind) && Boolean(node.imageUrl))).toBe(true);
    expect(graph.edges.some((edge) => edge.from === "candidate:a" && edge.to === "candidate:b" && edge.relation === "continue")).toBe(true);
    expect(graph.nodes.find((node) => node.id === "candidate:b")?.imageUrl).toBe("/raw/b");
    expect(graph.edges.some((edge) => edge.from === "source" && edge.to === "candidate:a")).toBe(true);
    expect(graph.edges.some((edge) => edge.from === "source" && edge.to === "candidate:b")).toBe(false);
    expect(graph.nodes.some((node) => node.kind === "final")).toBe(false);
    expect(graph.edges.some((edge) => edge.to === "final")).toBe(false);
  });

  it("deduplicates replayed durable events before deriving node state", () => {
    const event = { id: "17", type: "round.generation.started", data: { round_index: 1 } };
    const graph = buildTimelineGraph([event, event], snapshot("running"));
    expect(new Set(graph.nodes.map((node) => node.id)).size).toBe(graph.nodes.length);
    expect(new Set(graph.edges.map((edge) => edge.id)).size).toBe(graph.edges.length);
  });

  it("Timeline adapter 使用中心坐标、确定性 edge id 和受限 edge type", () => {
    const graph = buildTimelineGraph([], snapshot("accepted"), {
      sourceImageUrl: "/source.jpg",
      acceptedCandidateId: "b",
    });
    const layout = layoutTimelineGraph(graph);
    expect(layout.positions.source.y).toBe(0);
    expect(layout.positions.final.y).toBe(0);
    expect(layout.positions["candidate:a"].x).toBeLessThan(layout.positions["candidate:b"].x);
    expect(layout.positions.final.x - layout.positions["candidate:b"].x).toBe(214);
    expect(layout.edges.map((edge) => edge.id)).toEqual([
      "source__candidate:a",
      "candidate:a__candidate:b",
      "candidate:b__final",
    ]);
    expect(layout.edges.every((edge) => edge.type === "straight" || edge.type === "default")).toBe(true);
  });

  it("没有 source 图片时，过程事件不会伪造照片节点", () => {
    const graph = buildTimelineGraph([
      { id: "g", type: "round.generation.started", data: { round_index: 2 } },
      { id: "c", type: "round.critic.started", data: { round_index: 2 } },
    ]);
    expect(graph.nodes).toEqual([]);
  });

  it("生成期间按候选数量创建照片占位节点，并从底图画出连线", () => {
    const running = { ...snapshot("running"), candidates: [] };
    const graph = buildTimelineGraph([], running, {
      sourceImageUrl: "/source.jpg",
      expectedCandidateCount: 3,
      generationActive: true,
      currentRound: 1,
    });

    const pending = graph.nodes.filter((node) => node.kind === "candidate" && node.placeholder);
    expect(pending.map((node) => node.id)).toEqual(["pending:1:0", "pending:1:1", "pending:1:2"]);
    expect(pending.every((node) => node.imageUrl === "/source.jpg" && node.progress === "indeterminate")).toBe(true);
    expect(graph.edges.map((edge) => `${edge.from}->${edge.to}`)).toEqual([
      "source->pending:1:0",
      "source->pending:1:1",
      "source->pending:1:2",
    ]);
    expect(graph.nodes.find((node) => node.id === "final")).toBeUndefined();
  });

  it("接受候选后才创建最终节点，并按当前真实深度重新布局", () => {
    const beforeAccept = buildTimelineGraph([], snapshot(), { sourceImageUrl: "/source.jpg" });
    expect(beforeAccept.nodes.some((node) => node.id === "final")).toBe(false);
    const beforeLayout = layoutTimelineGraph(beforeAccept);
    expect(beforeLayout.positions["candidate:a"].x - beforeLayout.positions.source.x).toBe(214);
    expect(beforeLayout.positions["candidate:b"].x - beforeLayout.positions["candidate:a"].x).toBe(214);

    const afterAccept = buildTimelineGraph([], snapshot("accepted"), {
      sourceImageUrl: "/source.jpg",
      acceptedCandidateId: "b",
    });
    const layout = layoutTimelineGraph(afterAccept);
    expect(afterAccept.nodes.find((node) => node.id === "final")).toMatchObject({
      label: "已接受",
      detail: "等待 Fusion Mask",
      candidateId: "b",
    });
    expect(afterAccept.edges).toContainEqual(expect.objectContaining({
      from: "candidate:b",
      to: "final",
      relation: "accept",
    }));
    expect(layout.positions.final.x - layout.positions["candidate:b"].x).toBe(214);
  });

  it("接受历史 Global Winner 时由真实候选连接最终节点，不伪造末轮谱系", () => {
    const graph = buildTimelineGraph([], snapshot("accepted"), {
      sourceImageUrl: "/source.jpg",
      acceptedCandidateId: "a",
    });
    const layout = layoutTimelineGraph(graph);

    expect(graph.nodes.find((node) => node.id === "final")).toMatchObject({
      candidateId: "a",
      imageUrl: "/raw/a",
    });
    expect(graph.edges).toContainEqual(expect.objectContaining({
      from: "candidate:a",
      to: "final",
      relation: "accept",
    }));
    expect(graph.edges.some((edge) => edge.from === "candidate:b" && edge.to === "final")).toBe(false);
    expect(layout.positions.final.x).toBeGreaterThan(layout.positions["candidate:b"].x);
    expect(layout.positions.final.y).toBeGreaterThan(layout.positions["candidate:b"].y);
    expect(layout.edges).toContainEqual(expect.objectContaining({
      id: "candidate:a__final",
      source: "candidate:a",
      target: "final",
      type: "default",
    }));
  });

  it("allows Fusion only after accepted and only through an explicit action", () => {
    function Probe({ hasSearch, accepted }: { hasSearch: boolean; accepted: boolean }) {
      const [state, actions] = useWorkbenchUi({ hasSearch, accepted, roundIndex: 0 });
      return (
        <div>
          <output data-testid="mode">{state.workerMode}</output>
          <output data-testid="zoom">{state.zoom}</output>
          <button type="button" onClick={actions.enterFusion}>enter fusion</button>
          <button type="button" onClick={() => actions.setZoom(175)}>set zoom</button>
        </div>
      );
    }
    const view = render(<Probe hasSearch={false} accepted={false} />);
    expect(screen.getByTestId("mode")).toHaveTextContent("create");
    fireEvent.click(screen.getByRole("button", { name: "enter fusion" }));
    expect(screen.getByTestId("mode")).toHaveTextContent("create");
    fireEvent.click(screen.getByRole("button", { name: "set zoom" }));
    expect(screen.getByTestId("zoom")).toHaveTextContent("175");
    view.rerender(<Probe hasSearch accepted={false} />);
    expect(screen.getByTestId("mode")).toHaveTextContent("generation");
    expect(screen.getByTestId("zoom")).toHaveTextContent("100");
    view.rerender(<Probe hasSearch accepted />);
    fireEvent.click(screen.getByRole("button", { name: "enter fusion" }));
    expect(screen.getByTestId("mode")).toHaveTextContent("fusion");
  });

  it("hover 放大照片，并可点击底图或 Raw 候选切换主工作区", () => {
    const context = {
      setTransform: vi.fn(), clearRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), bezierCurveTo: vi.fn(),
      stroke: vi.fn(), closePath: vi.fn(), lineTo: vi.fn(), fill: vi.fn(), setLineDash: vi.fn(),
    } as unknown as CanvasRenderingContext2D;
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context);
    const onSelect = vi.fn();
    const onSelectSource = vi.fn();
    render(
      <TimelineCanvas
        events={[]}
        snapshot={snapshot()}
        sourceImageUrl="/source.jpg"
        onSelectCandidate={onSelect}
        onSelectSource={onSelectSource}
      />,
    );
    const node = screen.getByRole("button", { name: /R0 · 1/ });
    fireEvent.pointerEnter(node, { clientX: 40, clientY: 30 });
    const preview = screen.getByRole("status");
    expect(preview).toHaveTextContent("R0 · 1");
    expect(preview.querySelector(".timeline-preview-zoom")).not.toBeNull();
    expect(preview.querySelector(".timeline-preview-navigator i")).not.toBeNull();
    fireEvent.click(node);
    expect(onSelect).toHaveBeenCalledWith("a");

    fireEvent.click(screen.getByRole("button", { name: /底图/ }));
    expect(onSelectSource).toHaveBeenCalledTimes(1);
  });

  it("Timeline、Review 与主工作区共享唯一候选选择，而不是各自维护状态", () => {
    function SelectionHarness() {
      const [selected, setSelected] = useState<string | null>("a");
      const current = snapshot();
      return <>
        <CriticInspector
          snapshot={current}
          status={current.status}
          expectedCount={2}
          selectedCandidateId={selected}
        />
        <RawCandidateViewer snapshot={current} selectedCandidateId={selected} />
        <TimelineCanvas
          events={[]}
          snapshot={current}
          selectedNodeId={selected ? `candidate:${selected}` : null}
          onSelectCandidate={setSelected}
        />
      </>;
    }

    render(<SelectionHarness />);
    expect(screen.getByAltText("Raw candidate a")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /第 1 轮，第 1 张候选 Raw/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /R1 · 1/ }));

    expect(screen.getByAltText("Raw candidate b")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "当前 Timeline 候选 R1 V1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /R1 · 1/ })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: /R0 · 1/ }));
    expect(screen.getByAltText("Raw candidate a")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "当前 Timeline 候选 R0 V1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /R0 · 1/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("工具栏提供独立手型工具，避免平移时在 Mask 上落笔", () => {
    const onToolChange = vi.fn();
    render(
      <WorkerToolbar
        mode="create"
        zoom={100}
        brushTool="paint"
        brushSize={72}
        brushFlow={18}
        brushFeather={35}
        onBrushToolChange={onToolChange}
        onBrushSizeChange={() => undefined}
        onBrushFlowChange={() => undefined}
        onBrushFeatherChange={() => undefined}
        onZoomChange={() => undefined}
        onFit={() => undefined}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "手型工具" }));
    expect(onToolChange).toHaveBeenCalledWith("hand");
  });

  it("主工作区与 Timeline 收口到同一个 xyflow 手势 provider", () => {
    const { container } = render(<>
      <WorkerViewport
        mode="generation"
        backgroundUrl={null}
        placement={{
          x: 0,
          y: 0,
          width: 1,
          height: 1,
          coordinate_space: "normalized",
          pose: "sitting",
          facing: "camera",
          contact_surface: null,
        }}
        guidanceEditorRef={createRef<GuidanceMaskEditorHandle>()}
        guidanceState={null}
        onGuidanceStateChange={() => undefined}
        snapshot={snapshot()}
        selectedCandidateId="a"
        zoom={100}
      />
      <TimelineCanvas events={[]} snapshot={snapshot()} sourceImageUrl="/source.jpg" />
    </>);

    const worker = container.querySelector<HTMLElement>(".worker-gesture-flow");
    const timeline = container.querySelector<HTMLElement>(".timeline-flow");
    expect(worker).toHaveAttribute("data-gesture-provider", "xyflow");
    expect(timeline).toHaveAttribute("data-gesture-provider", "xyflow");
    expect(worker).toHaveClass("is-pan-enabled");
    expect(worker?.querySelector(":scope > .react-flow")).not.toBeNull();
    expect(timeline?.querySelector(":scope > .react-flow")).not.toBeNull();
    expect(container.querySelector(".react-transform-wrapper")).toBeNull();
  });

  it("创建模式的底片画布也由全尺寸 xyflow 手势层承载", () => {
    const { container } = render(
      <WorkerViewport
        mode="create"
        backgroundUrl="/source.jpg"
        backgroundWidth={900}
        backgroundHeight={1200}
        placement={{
          x: 0,
          y: 0,
          width: 1,
          height: 1,
          coordinate_space: "normalized",
          pose: "sitting",
          facing: "camera",
          contact_surface: null,
        }}
        guidanceEditorRef={createRef<GuidanceMaskEditorHandle>()}
        guidanceState={null}
        onGuidanceStateChange={() => undefined}
        brushTool="hand"
        zoom={100}
      />,
    );

    const dropSurface = screen.getByTestId("worker-drop-surface");
    const gesture = dropSurface.querySelector<HTMLElement>(".worker-gesture-flow");
    expect(gesture).toHaveAttribute("data-gesture-provider", "xyflow");
    expect(gesture).toHaveClass("is-pan-enabled");
    expect(container.querySelector(".guidance-editor-surface")).not.toBeNull();
  });

  it("生成中的主工作区展示完整画布占位和全部候选槽位", () => {
    const current = { ...snapshot("running"), candidates: [] };
    render(<RawCandidateViewer snapshot={current} expectedCount={3} sourceWidth={1200} sourceHeight={800} />);

    expect(screen.getByRole("status")).toHaveClass("worker-generation-placeholder");
    expect(screen.getByText("正在生成 R1")).toBeInTheDocument();
    expect(screen.getByLabelText("3 个候选生成槽位").children).toHaveLength(3);
    expect(screen.getByLabelText("候选 3 正在生成")).toBeInTheDocument();
    expect(screen.getByRole("status").style.getPropertyValue("--source-ratio")).toBe("1.5");
    expect(screen.getByText("正在生成 R1").closest(".generation-placeholder-stage")).not.toBeNull();
  });
});
