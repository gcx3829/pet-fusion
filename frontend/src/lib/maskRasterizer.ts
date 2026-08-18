import {
  appendInterpolatedPoints,
  clampUnit,
  normalizeBrushSettings,
  type MaskDocument,
  type MaskStroke,
  type NormalizedPoint,
} from "./maskDocument";

export interface RasterizedMask {
  width: number;
  height: number;
  /** One alpha value per pixel. 0 means original, 255 means generated. */
  alpha: Uint8ClampedArray;
}

export const MAX_MASK_DIMENSION = 16_384;
export const MAX_MASK_PIXELS = 40_000_000;

function safeRasterDimension(value: number, name: string): number {
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${name}必须为正数`);
  }
  const rounded = Math.max(1, Math.round(value));
  if (rounded > MAX_MASK_DIMENSION) {
    throw new Error(`${name}超过浏览器 Canvas 安全限制`);
  }
  return rounded;
}

function smoothstep(value: number): number {
  const clamped = Math.min(1, Math.max(0, value));
  return clamped * clamped * (3 - 2 * clamped);
}

function stamp(
  alpha: Float32Array,
  width: number,
  height: number,
  point: NormalizedPoint,
  stroke: MaskStroke,
  sourceWidth: number,
  sourceHeight: number,
): void {
  const settings = normalizeBrushSettings(stroke.settings);
  const centerX = clampUnit(point.x) * width;
  const centerY = clampUnit(point.y) * height;
  const scaleX = width / sourceWidth;
  const scaleY = height / sourceHeight;
  const radiusX = Math.max(0.5, settings.size * scaleX / 2);
  const radiusY = Math.max(0.5, settings.size * scaleY / 2);
  const feather = settings.feather;
  const left = Math.max(0, Math.floor(centerX - radiusX - 1));
  const right = Math.min(width - 1, Math.ceil(centerX + radiusX + 1));
  const top = Math.max(0, Math.floor(centerY - radiusY - 1));
  const bottom = Math.min(height - 1, Math.ceil(centerY + radiusY + 1));

  for (let y = top; y <= bottom; y += 1) {
    const dy = (y + 0.5 - centerY) / radiusY;
    for (let x = left; x <= right; x += 1) {
      const dx = (x + 0.5 - centerX) / radiusX;
      const distance = Math.hypot(dx, dy);
      if (distance >= 1) continue;

      const edgeCoverage = feather <= 0
        ? 1
        : distance <= 1 - feather
          ? 1
          : smoothstep((1 - distance) / feather);
      const coverage = edgeCoverage * settings.flow;
      if (coverage <= 0) continue;
      const index = y * width + x;
      const previous = alpha[index]!;
      const next = stroke.tool === "paint"
        ? previous + (1 - previous) * coverage
        : previous * (1 - coverage);
      // Keep the accumulator in float space until the complete document has
      // been replayed. Quantizing every stamp to 8-bit makes a 1% flow brush
      // appear stuck on an already bright mask: the sub-gray-step increment
      // is rounded away on every pass.
      alpha[index] = Math.min(1, Math.max(0, next));
    }
  }
}

function rasterizeStroke(
  alpha: Float32Array,
  width: number,
  height: number,
  stroke: MaskStroke,
  sourceWidth: number,
  sourceHeight: number,
): void {
  if (!stroke.points.length) return;
  const settings = normalizeBrushSettings(stroke.settings);
  stamp(alpha, width, height, stroke.points[0], stroke, sourceWidth, sourceHeight);
  for (let index = 1; index < stroke.points.length; index += 1) {
    const previous = stroke.points[index - 1];
    const next = stroke.points[index];
    const samples = appendInterpolatedPoints(
      [previous],
      next,
      sourceWidth,
      sourceHeight,
      settings,
    );
    for (let sampleIndex = 1; sampleIndex < samples.length; sampleIndex += 1) {
      stamp(alpha, width, height, samples[sampleIndex], stroke, sourceWidth, sourceHeight);
    }
  }
}

/**
 * Rasterize only when a preview or export needs pixels.  The document itself
 * remains a compact normalized stroke list, so pointer events never allocate
 * a source-sized bitmap or make a network request.
 */
export function rasterizeMask(
  document: MaskDocument,
  targetWidth = document.width,
  targetHeight = document.height,
): RasterizedMask {
  const width = safeRasterDimension(targetWidth, "Mask 宽度");
  const height = safeRasterDimension(targetHeight, "Mask 高度");
  if (width * height > MAX_MASK_PIXELS) {
    throw new Error(`Mask 超过 ${MAX_MASK_PIXELS} 像素安全限制`);
  }
  const accumulated = new Float32Array(width * height);
  for (const stroke of document.strokes) {
    rasterizeStroke(accumulated, width, height, stroke, document.width, document.height);
  }
  const alpha = new Uint8ClampedArray(accumulated.length);
  for (let index = 0; index < accumulated.length; index += 1) {
    alpha[index] = Math.round(accumulated[index]! * 255);
  }
  return { width, height, alpha };
}

export function maskAlphaAt(
  mask: RasterizedMask,
  x: number,
  y: number,
): number {
  const column = Math.min(mask.width - 1, Math.max(0, Math.floor(x)));
  const row = Math.min(mask.height - 1, Math.max(0, Math.floor(y)));
  return mask.alpha[row * mask.width + column];
}

/** White RGB with the editable/reveal amount in alpha. */
export function maskToRgbaPixels(mask: RasterizedMask): Uint8ClampedArray {
  const rgba = new Uint8ClampedArray(mask.width * mask.height * 4);
  for (let index = 0; index < mask.alpha.length; index += 1) {
    const offset = index * 4;
    rgba[offset] = 255;
    rgba[offset + 1] = 255;
    rgba[offset + 2] = 255;
    rgba[offset + 3] = mask.alpha[index];
  }
  return rgba;
}

/**
 * Encode an export-sized RGBA PNG in the browser. This is intentionally a
 * local canvas operation; it never calls fetch or a backend endpoint.
 */
export async function exportMaskPng(maskDocument: MaskDocument): Promise<Blob> {
  if (typeof document === "undefined") {
    throw new Error("只能在浏览器中导出 Mask PNG");
  }
  const mask = rasterizeMask(maskDocument);
  const canvas = document.createElement("canvas");
  canvas.width = mask.width;
  canvas.height = mask.height;
  const context = canvas.getContext("2d", { alpha: true });
  if (!context) throw new Error("当前浏览器无法创建 Mask 画布");

  const imageData = context.createImageData(mask.width, mask.height);
  for (let index = 0; index < mask.alpha.length; index += 1) {
    const offset = index * 4;
    imageData.data[offset] = 255;
    imageData.data[offset + 1] = 255;
    imageData.data[offset + 2] = 255;
    imageData.data[offset + 3] = mask.alpha[index];
  }
  context.putImageData(imageData, 0, 0);
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("Mask PNG 编码失败")),
      "image/png",
    );
  });
}

export async function exportMaskFile(
  maskDocument: MaskDocument,
  fileName = "pet-fusion-mask.png",
): Promise<File> {
  const blob = await exportMaskPng(maskDocument);
  const normalizedName = fileName.toLowerCase().endsWith(".png") ? fileName : `${fileName}.png`;
  return new File([blob], normalizedName, { type: "image/png" });
}
