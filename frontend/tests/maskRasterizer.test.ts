import { describe, expect, it, vi } from "vitest";
import {
  appendInterpolatedPoints,
  createPlacementGuidanceDocument,
  createMaskDocument,
  resizeMaskDocument,
  type MaskStroke,
} from "../src/lib/maskDocument";
import {
  exportMaskPng,
  maskToRgbaPixels,
  maskAlphaAt,
  rasterizeMask,
} from "../src/lib/maskRasterizer";

function dab(
  x: number,
  y: number,
  overrides: Partial<MaskStroke["settings"]> = {},
  tool: MaskStroke["tool"] = "paint",
): MaskStroke {
  return {
    tool,
    points: [{ x, y }],
    settings: { size: 20, flow: 0.5, feather: 0, ...overrides },
  };
}

describe("mask rasterizer", () => {
  it("同一个位置重复绘制会按流量累积，而不是覆盖成一次固定不透明度", () => {
    const once = rasterizeMask(createMaskDocument(100, 100, [dab(0.5, 0.5)]));
    const twice = rasterizeMask(createMaskDocument(100, 100, [
      dab(0.5, 0.5),
      dab(0.5, 0.5),
    ]));

    expect(maskAlphaAt(once, 50, 50)).toBeGreaterThan(100);
    expect(maskAlphaAt(twice, 50, 50)).toBeGreaterThan(maskAlphaAt(once, 50, 50));
    expect(maskAlphaAt(twice, 50, 50)).toBeLessThan(255);
  });

  it("极低流量也能叠加到已有的非零遮罩，而不是被已有笔划挡住", () => {
    const seed = dab(0.5, 0.5, { flow: 0.18 });
    const before = rasterizeMask(createMaskDocument(100, 100, [seed]));
    const after = rasterizeMask(createMaskDocument(100, 100, [
      seed,
      dab(0.5, 0.5, { flow: 0.01 }),
    ]));

    expect(maskAlphaAt(before, 50, 50)).toBeGreaterThan(0);
    expect(maskAlphaAt(after, 50, 50)).toBeGreaterThan(maskAlphaAt(before, 50, 50));

    const layered = [seed];
    const samples: number[] = [];
    for (let index = 0; index < 8; index += 1) {
      layered.push(dab(0.5, 0.5, { flow: 0.01 }));
      samples.push(maskAlphaAt(rasterizeMask(createMaskDocument(100, 100, layered)), 50, 50));
    }
    expect(samples.every((value, index) => index === 0 || value > samples[index - 1]!)).toBe(true);
  });

  it("高 alpha 遮罩上的低流量重复笔划不会因 8-bit 量化而卡住", () => {
    const strokes = [dab(0.5, 0.5, { flow: 0.95 })];
    const samples: number[] = [];
    for (let index = 0; index < 8; index += 1) {
      strokes.push(dab(0.5, 0.5, { flow: 0.01 }));
      samples.push(maskAlphaAt(rasterizeMask(createMaskDocument(100, 100, strokes)), 50, 50));
    }

    expect(samples[0]).toBeGreaterThan(240);
    expect(samples.at(-1)).toBeGreaterThan(samples[0]!);
  });

  it("默认 Guidance seed 保留可叠加的余量，不从 alpha=255 起步", () => {
    const document = createPlacementGuidanceDocument(100, 100, {
      x: 0.4,
      y: 0.4,
      width: 0.2,
      height: 0.2,
    });
    const mask = rasterizeMask(document);
    const center = maskAlphaAt(mask, 50, 50);

    expect(center).toBeGreaterThan(0);
    expect(center).toBeLessThan(255);
  });

  it("导出的 RGBA alpha 使用 0=原图、255=生成图语义", () => {
    const empty = maskToRgbaPixels(rasterizeMask(createMaskDocument(20, 20)));
    const painted = maskToRgbaPixels(rasterizeMask(createMaskDocument(20, 20, [
      dab(0.5, 0.5, { flow: 1 }),
    ])));
    const centerAlpha = (10 * 20 + 10) * 4 + 3;

    expect(empty[centerAlpha]).toBe(0);
    expect(painted[centerAlpha]).toBe(255);
    expect(Array.from(painted.slice(centerAlpha - 3, centerAlpha))).toEqual([255, 255, 255]);
  });

  it("擦除笔划按流量降低已有 alpha", () => {
    const mask = rasterizeMask(createMaskDocument(100, 100, [
      dab(0.5, 0.5, { flow: 1 }),
      dab(0.5, 0.5, { flow: 0.5 }, "erase"),
    ]));

    expect(maskAlphaAt(mask, 50, 50)).toBeGreaterThan(100);
    expect(maskAlphaAt(mask, 50, 50)).toBeLessThan(180);
  });

  it("羽化使边缘逐渐衰减，画笔外保持原图 alpha=0", () => {
    const mask = rasterizeMask(createMaskDocument(100, 100, [
      dab(0.5, 0.5, { flow: 1, feather: 0.5 }),
    ]));
    const center = maskAlphaAt(mask, 50, 50);
    const edge = maskAlphaAt(mask, 59, 50);

    expect(center).toBe(255);
    expect(edge).toBeGreaterThan(0);
    expect(edge).toBeLessThan(center);
    expect(maskAlphaAt(mask, 70, 50)).toBe(0);
  });

  it("插值后的笔划在稀疏 pointer event 之间没有断点", () => {
    const points = appendInterpolatedPoints(
      [{ x: 0.05, y: 0.5 }],
      { x: 0.95, y: 0.5 },
      100,
      100,
      { size: 16 },
    );
    const mask = rasterizeMask(createMaskDocument(100, 100, [{
      tool: "paint",
      points,
      settings: { size: 16, flow: 1, feather: 0 },
    }]));

    expect(points.length).toBeGreaterThan(20);
    for (const x of [15, 25, 35, 45, 55, 65, 75, 85]) {
      expect(maskAlphaAt(mask, x, 50)).toBeGreaterThan(0);
    }
  });

  it("同一归一化笔划在预览缩放后保持相同的覆盖轮廓", () => {
    const document = createMaskDocument(100, 100, [dab(0.5, 0.5, { flow: 1 })]);
    const small = rasterizeMask(document, 100, 100);
    const large = rasterizeMask(document, 200, 200);

    for (const offset of [0, 4, 8, 10, 12, 16]) {
      expect(
        Math.abs(
          maskAlphaAt(small, 50 + offset, 50)
          - maskAlphaAt(large, 100 + offset * 2, 100),
        ),
      ).toBeLessThanOrEqual(4);
    }
  });

  it("项目规范化尺寸变化后保持归一化位置并按比例缩放笔刷", () => {
    const source = createMaskDocument(4000, 3000, [{
      tool: "paint",
      points: [{ x: 0.25, y: 0.75 }],
      settings: { size: 400, flow: 0.7, feather: 0.4 },
    }]);

    const resized = resizeMaskDocument(source, 2000, 1500);

    expect(resized).toMatchObject({ width: 2000, height: 1500 });
    expect(resized.strokes[0]?.points[0]).toEqual({ x: 0.25, y: 0.75 });
    expect(resized.strokes[0]?.settings.size).toBe(200);
    expect(source.strokes[0]?.settings.size).toBe(400);
  });

  it("导出 RGBA PNG 只使用本地 Canvas，不调用 fetch", async () => {
    const context = {
      createImageData: vi.fn((width: number, height: number) => ({
        data: new Uint8ClampedArray(width * height * 4),
      })),
      putImageData: vi.fn(),
    };
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockImplementation(() => context as unknown as CanvasRenderingContext2D);
    const toBlob = vi
      .spyOn(HTMLCanvasElement.prototype, "toBlob")
      .mockImplementation((callback) => callback(new Blob(["mask"], { type: "image/png" })));
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const blob = await exportMaskPng(createMaskDocument(37, 19, [dab(0.5, 0.5)]));

    expect(blob.type).toBe("image/png");
    expect(getContext).toHaveBeenCalledWith("2d", { alpha: true });
    expect(context.createImageData).toHaveBeenCalledWith(37, 19);
    expect(context.putImageData).toHaveBeenCalled();
    expect(toBlob).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("拒绝创建超过浏览器安全限制的导出位图", () => {
    expect(() => rasterizeMask(createMaskDocument(20_000, 20_000)))
      .toThrow("Canvas 安全限制");
    expect(() => rasterizeMask(createMaskDocument(8_000, 8_000)))
      .toThrow("像素安全限制");
  });
});
