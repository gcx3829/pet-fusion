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

  it("pointer capture 意外丢失时回滚未完成笔划并允许继续绘制", async () => {
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
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(0));

    fireEvent.pointerDown(canvas, { pointerId: 5, button: 0, clientX: 30, clientY: 30 });
    fireEvent.pointerUp(canvas, { pointerId: 5, clientX: 40, clientY: 40 });
    await waitFor(() => expect(documents.at(-1)?.strokes).toHaveLength(1));
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
