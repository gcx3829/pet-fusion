import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MaskBrushEditor,
  drawGeneratedCandidate,
  type MaskBrushEditorHandle,
} from "../src/features/mask/MaskBrushEditor";
import type { MaskDocument } from "../src/lib/maskDocument";

const context = {
  clearRect: vi.fn(),
  fillRect: vi.fn(),
  drawImage: vi.fn(),
  getImageData: vi.fn((width: number, height: number) => ({
    data: new Uint8ClampedArray(width * height * 4),
  })),
  putImageData: vi.fn(),
  beginPath: vi.fn(),
  ellipse: vi.fn(),
  stroke: vi.fn(),
  createImageData: vi.fn((width: number, height: number) => ({
    data: new Uint8ClampedArray(width * height * 4),
  })),
};

class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 0;
  }
}

describe("MaskBrushEditor", () => {
  beforeEach(() => {
    vi.stubGlobal("PointerEvent", TestPointerEvent);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockImplementation(() => context as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob")
      .mockImplementation((callback) => callback(new Blob(["mask"], { type: "image/png" })));
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({
        width: 100,
        height: 100,
        top: 0,
        left: 0,
        right: 100,
        bottom: 100,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("工具栏切换绘制/擦除后，紧接着落笔也同步使用最新工具和参数", async () => {
    const documents: MaskDocument[] = [];
    const view = render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        controlledTool="paint"
        controlledBrush={{ size: 18, flow: 0.12, feather: 0.08 }}
        onDocumentChange={(document) => documents.push(document)}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    view.rerender(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        controlledTool="erase"
        controlledBrush={{ size: 44, flow: 0.07, feather: 0.61 }}
        onDocumentChange={(document) => documents.push(document)}
      />,
    );
    fireEvent.pointerDown(canvas, { pointerId: 21, button: 0, clientX: 30, clientY: 30 });
    fireEvent.pointerUp(canvas, { pointerId: 21, button: 0, clientX: 32, clientY: 32 });

    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));
    expect(documents.at(-1)?.strokes[0]).toMatchObject({
      tool: "erase",
      settings: { size: 44, flow: 0.07, feather: 0.61 },
    });
  });

  it("手型/禁用状态只使用原生 grab 光标，不绘制画笔圆环", () => {
    context.ellipse.mockClear();
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        disabled
        controlledTool="paint"
        controlledBrush={{ size: 48, flow: 0.2, feather: 0.3 }}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    fireEvent.pointerEnter(canvas, { pointerId: 31, clientX: 40, clientY: 40 });
    fireEvent.pointerMove(canvas, { pointerId: 31, clientX: 60, clientY: 60 });

    expect(context.ellipse).not.toHaveBeenCalled();
    expect(canvas).toHaveAttribute("aria-disabled", "true");
  });

  it("按笔划 undo/redo，且画笔编辑本身不触发 fetch", async () => {
    const documents: MaskDocument[] = [];
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        onDocumentChange={(document) => documents.push(document)}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 20, clientY: 50 });
    fireEvent.pointerMove(canvas, { pointerId: 1, clientX: 80, clientY: 50 });
    fireEvent.pointerUp(canvas, { pointerId: 1, clientX: 80, clientY: 50 });
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "撤销笔划" }));
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(0));
    fireEvent.click(screen.getByRole("button", { name: "重做笔划" }));
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));
  });

  it("抬笔坐标会补入轨迹，pointer cancel 则回滚未完成笔划", async () => {
    const documents: MaskDocument[] = [];
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        onDocumentChange={(document) => documents.push(document)}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    fireEvent.pointerDown(canvas, { pointerId: 2, button: 0, clientX: 10, clientY: 50 });
    fireEvent.pointerUp(canvas, { pointerId: 2, clientX: 90, clientY: 50 });
    await waitFor(() => expect(documents.at(-1)?.strokes[0]?.points.length).toBeGreaterThan(2));

    fireEvent.pointerDown(canvas, { pointerId: 3, button: 0, clientX: 20, clientY: 20 });
    fireEvent.pointerMove(canvas, { pointerId: 3, clientX: 70, clientY: 20 });
    fireEvent.pointerCancel(canvas, { pointerId: 3, clientX: 70, clientY: 20 });
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));
  });

  it("pointer capture 意外丢失时提交已经可见的笔划并允许继续绘制", async () => {
    const documents: MaskDocument[] = [];
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        onDocumentChange={(document) => documents.push(document)}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    fireEvent.pointerDown(canvas, { pointerId: 4, button: 0, clientX: 10, clientY: 10 });
    fireEvent.lostPointerCapture(canvas, { pointerId: 4, clientX: 20, clientY: 20 });
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));

    fireEvent.pointerDown(canvas, { pointerId: 5, button: 0, clientX: 30, clientY: 30 });
    fireEvent.pointerUp(canvas, { pointerId: 5, clientX: 40, clientY: 40 });
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(2));
  });

  it("React Flow 吞掉 canvas pointerup 时由 window capture 收尾并写入操作历史", async () => {
    const documents: MaskDocument[] = [];
    const historyStates: Array<{ canUndo: boolean; undoDepth: number }> = [];
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        onDocumentChange={(document) => documents.push(document)}
        onHistoryChange={(state) => historyStates.push(state)}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    fireEvent.pointerDown(canvas, { pointerId: 41, button: 0, clientX: 15, clientY: 20 });
    fireEvent.pointerMove(canvas, { pointerId: 41, clientX: 70, clientY: 75 });
    fireEvent.pointerUp(window, { pointerId: 41, clientX: 85, clientY: 80 });

    await waitFor(() => expect(historyStates.at(-1)).toMatchObject({ canUndo: true, undoDepth: 1 }));
    expect(documents.at(-1)?.strokes).toHaveLength(1);
  });

  it("只收到原生 mouseup 且鼠标复用同一 pointerId 时仍逐笔提交", async () => {
    const historyStates: Array<{ undoDepth: number }> = [];
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        onHistoryChange={(state) => historyStates.push(state)}
      />,
    );
    const canvas = screen.getByLabelText("Mask 画布，按住鼠标绘制");

    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 15, clientY: 20 });
    fireEvent.pointerMove(canvas, { pointerId: 1, buttons: 1, clientX: 45, clientY: 45 });
    fireEvent.mouseUp(window, { clientX: 45, clientY: 45 });
    await waitFor(() => expect(historyStates.at(-1)?.undoDepth).toBe(1));

    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 55, clientY: 60 });
    fireEvent.pointerMove(canvas, { pointerId: 1, buttons: 1, clientX: 80, clientY: 80 });
    fireEvent.mouseUp(window, { clientX: 80, clientY: 80 });
    await waitFor(() => expect(historyStates.at(-1)?.undoDepth).toBe(2));
  });

  it("通过 ref 导出原图尺寸的本地 PNG 文件", async () => {
    const editorRef = { current: null as MaskBrushEditorHandle | null };
    render(
      <MaskBrushEditor
        ref={editorRef}
        originalSrc={null}
        generatedSrc={null}
        width={320}
        height={180}
      />,
    );

    const file = await editorRef.current?.exportMaskFile("guidance-mask")
      ?? new File([], "missing.png");

    expect(file.name).toBe("guidance-mask.png");
    expect(file.type).toBe("image/png");
  });

  it("键盘可移动笔刷并绘制一个可撤销的 dab", async () => {
    const documents: MaskDocument[] = [];
    render(
      <MaskBrushEditor
        originalSrc={null}
        generatedSrc={null}
        width={100}
        height={100}
        onDocumentChange={(document) => documents.push(document)}
      />,
    );
    const canvas = screen.getByRole("application", { name: "Mask 画布，按住鼠标绘制" });

    fireEvent.keyDown(canvas, { key: "ArrowRight" });
    fireEvent.keyDown(canvas, { key: " " });
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));
    expect(documents.at(-1)?.strokes[0]?.points[0]?.x).toBeCloseTo(0.51);

    fireEvent.click(screen.getByRole("button", { name: "撤销笔划" }));
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(0));
  });

  it("按后端 crop mapping 把 raw 内容放回原图坐标，缺 mapping 时拒绝拉伸 crop", () => {
    const context = { drawImage: vi.fn() } as unknown as CanvasRenderingContext2D;
    const croppedImage = {
      naturalWidth: 120,
      naturalHeight: 100,
      width: 120,
      height: 100,
    } as HTMLImageElement;
    const mapping = {
      schema_version: "crop-mapping/v1" as const,
      full_width: 400,
      full_height: 300,
      crop_box: { x: 100, y: 75, width: 200, height: 150 },
      canvas_width: 120,
      canvas_height: 100,
      padding: { left: 10, top: 5, right: 10, bottom: 5 },
    };

    expect(drawGeneratedCandidate(context, croppedImage, mapping, 400, 300, 800, 600)).toBe(true);
    expect(context.drawImage).toHaveBeenCalledWith(
      croppedImage,
      10,
      5,
      100,
      90,
      200,
      150,
      400,
      300,
    );

    const fallbackContext = { drawImage: vi.fn() } as unknown as CanvasRenderingContext2D;
    expect(drawGeneratedCandidate(fallbackContext, croppedImage, undefined, 400, 300, 800, 600)).toBe(false);
    expect(fallbackContext.drawImage).not.toHaveBeenCalled();

    const fullImage = { naturalWidth: 400, naturalHeight: 300, width: 400, height: 300 } as HTMLImageElement;
    expect(drawGeneratedCandidate(fallbackContext, fullImage, undefined, 400, 300, 800, 600)).toBe(true);
    expect(fallbackContext.drawImage).toHaveBeenCalledWith(fullImage, 0, 0, 800, 600);

    const legacyMappingContext = { drawImage: vi.fn() } as unknown as CanvasRenderingContext2D;
    expect(drawGeneratedCandidate(
      legacyMappingContext,
      fullImage,
      { ...mapping, canvas_width: 400, canvas_height: 300 },
      400,
      300,
      800,
      600,
    )).toBe(true);
    expect(legacyMappingContext.drawImage).toHaveBeenCalledWith(fullImage, 0, 0, 800, 600);
  });
});
