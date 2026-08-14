import { describe, expect, it, vi } from "vitest";
import {
  fitImageDimensions,
  prepareImageForUpload,
  uploadPreparationLabel,
} from "../src/lib/files";

function sizedFile(name: string, type: string, size: number): File {
  const file = new File(["fixture"], name, { type, lastModified: 123 });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

describe("upload image preparation", () => {
  it("按像素和最长边等比约束尺寸", () => {
    expect(fitImageDimensions(8_000, 6_000, 12_000_000, 8_192)).toEqual({
      width: 4_000,
      height: 3_000,
    });
    expect(fitImageDimensions(1_920, 1_080, 12_000_000, 8_192)).toEqual({
      width: 1_920,
      height: 1_080,
    });
  });

  it("合规小图保留原文件，不重复编码", async () => {
    const file = sizedFile("small.jpg", "image/jpeg", 2 * 1024 * 1024);
    const encode = vi.fn();
    const result = await prepareImageForUpload(file, "background", {
      decode: vi.fn(async () => ({
        source: {} as CanvasImageSource,
        width: 2_000,
        height: 1_500,
      })),
      encode,
    });

    expect(result.file).toBe(file);
    expect(result.compressed).toBe(false);
    expect(encode).not.toHaveBeenCalled();
    expect(uploadPreparationLabel(file)).toBe("2.0 MB");
  });

  it("超限 PNG 转为较小的 WebP 上传副本", async () => {
    const file = sizedFile("IMG_20251229_110914.png", "image/png", 30 * 1024 * 1024);
    const close = vi.fn();
    const encode = vi.fn(async (
      _image: unknown,
      _width: number,
      _height: number,
      mimeType: string,
    ) => new Blob(["compressed"], { type: mimeType }));

    const result = await prepareImageForUpload(file, "background", {
      decode: vi.fn(async () => ({
        source: {} as CanvasImageSource,
        width: 8_000,
        height: 6_000,
        close,
      })),
      encode,
    });

    expect(result.compressed).toBe(true);
    expect(result.file.name).toBe("IMG_20251229_110914.webp");
    expect(result.file.type).toBe("image/webp");
    expect(result.width * result.height).toBeLessThanOrEqual(32_000_000);
    expect(uploadPreparationLabel(result.file)).toContain("30.0 MB →");
    expect(close).toHaveBeenCalledOnce();
  });
});
