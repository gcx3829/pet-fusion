/**
 * A Fusion/Guidance mask is represented as intent, not as a full-resolution
 * bitmap.  Points are normalized to the source image so the same document can
 * be rasterized for a small preview or an export-sized PNG without changing
 * the artist's strokes.
 */

export const MASK_DOCUMENT_VERSION = 1 as const;

export type MaskStrokeTool = "paint" | "erase";

export interface NormalizedPoint {
  /** Horizontal position in the source image, clamped to [0, 1]. */
  x: number;
  /** Vertical position in the source image, clamped to [0, 1]. */
  y: number;
}

export interface MaskBrushSettings {
  /** Brush diameter in source-image pixels. */
  size: number;
  /** Per-stamp opacity in [0, 1]. Repeated stamps accumulate. */
  flow: number;
  /** Fraction of the brush radius used for the soft edge, in [0, 1]. */
  feather: number;
}

export interface MaskStroke {
  tool: MaskStrokeTool;
  points: NormalizedPoint[];
  settings: MaskBrushSettings;
}

export interface MaskDocument {
  version: typeof MASK_DOCUMENT_VERSION;
  width: number;
  height: number;
  strokes: MaskStroke[];
}

/**
 * A stable, content-addressable representation of the editable mask intent.
 * The document is deliberately small (normalized strokes, never pixels), so
 * this is suitable for client-side upload de-duplication and dirty checks.
 */
export function maskDocumentFingerprint(document: MaskDocument): string {
  return JSON.stringify(createMaskDocument(document.width, document.height, document.strokes));
}

/**
 * Create a soft-brush document that approximates the existing placement-only
 * Guidance Mask fallback.  The placement is expanded so the model has room
 * for paws, fur edges and contact shadows, matching the backend's legacy
 * placement guidance window. Users can paint/erase from this starting point.
 */
export function createPlacementGuidanceDocument(
  width: number,
  height: number,
  placement: {
    x: number;
    y: number;
    width: number;
    height: number;
  },
  padding = { x: 0.045, y: 0.065 },
): MaskDocument {
  const sourceWidth = Math.max(1, Math.round(width));
  const sourceHeight = Math.max(1, Math.round(height));
  const left = clampUnit(placement.x - padding.x);
  const top = clampUnit(placement.y - padding.y);
  const right = clampUnit(placement.x + placement.width + padding.x);
  const bottom = clampUnit(placement.y + placement.height + padding.y);

  // Fill the rectangle with overlapping horizontal soft-brush lines. Keeping
  // the seed as ordinary strokes means undo/redo, erase and export all share
  // exactly the same rasterization path as hand-painted guidance.
  const brushSize = Math.max(1, Math.min(sourceWidth, sourceHeight) / 24);
  const spacing = Math.max(1, brushSize * 0.62);
  const topPx = top * sourceHeight;
  const bottomPx = bottom * sourceHeight;
  const leftPx = left * sourceWidth;
  const rightPx = right * sourceWidth;
  const strokes: MaskStroke[] = [];
  for (let y = topPx + brushSize / 2; y <= bottomPx; y += spacing) {
    const normalizedY = clampUnit(y / sourceHeight);
    strokes.push({
      tool: "paint",
      points: [
        { x: clampUnit((leftPx + brushSize / 2) / sourceWidth), y: normalizedY },
        { x: clampUnit((rightPx - brushSize / 2) / sourceWidth), y: normalizedY },
      ],
      settings: {
        size: Math.max(1, Math.min(brushSize, Math.min(sourceWidth, sourceHeight))),
        // Keep the placement seed soft so it behaves like a Photoshop base
        // wash: repeated low-flow strokes can still build it up instead of
        // starting at a saturated alpha=255 plateau.
        flow: 0.18,
        feather: 0.2,
      },
    });
  }
  // Extremely short boxes still need one dab rather than an empty document.
  if (!strokes.length) {
    strokes.push({
      tool: "paint",
      points: [{ x: clampUnit((left + right) / 2), y: clampUnit((top + bottom) / 2) }],
      settings: {
        size: Math.max(1, Math.min(sourceWidth, sourceHeight) / 24),
        flow: 0.18,
        feather: 0.2,
      },
    });
  }
  return createMaskDocument(sourceWidth, sourceHeight, strokes);
}

export const DEFAULT_MASK_BRUSH: MaskBrushSettings = {
  size: 180,
  flow: 0.18,
  feather: 0.35,
};

export function clampUnit(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

export function normalizePoint(point: NormalizedPoint): NormalizedPoint {
  return { x: clampUnit(point.x), y: clampUnit(point.y) };
}

export function normalizeBrushSettings(
  settings: MaskBrushSettings,
  fallback: MaskBrushSettings = DEFAULT_MASK_BRUSH,
): MaskBrushSettings {
  const size = Number.isFinite(settings.size) && settings.size > 0
    ? settings.size
    : fallback.size;
  return {
    size: Math.max(0.5, size),
    flow: clampUnit(settings.flow),
    feather: clampUnit(settings.feather),
  };
}

export function createMaskDocument(
  width: number,
  height: number,
  strokes: readonly MaskStroke[] = [],
): MaskDocument {
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0) {
    throw new Error("Mask 文档尺寸必须为正数");
  }
  return {
    version: MASK_DOCUMENT_VERSION,
    width: Math.max(1, Math.round(width)),
    height: Math.max(1, Math.round(height)),
    strokes: strokes.map(cloneStroke),
  };
}

export function cloneStroke(stroke: MaskStroke): MaskStroke {
  return {
    tool: stroke.tool,
    settings: normalizeBrushSettings(stroke.settings),
    points: stroke.points.map(normalizePoint),
  };
}

export function cloneMaskDocument(document: MaskDocument): MaskDocument {
  return createMaskDocument(document.width, document.height, document.strokes);
}

/**
 * Rebase normalized stroke intent onto another source resolution.
 *
 * Project creation can normalize EXIF orientation or resize a large upload.
 * Points are already normalized, but brush diameter is expressed in source
 * pixels and therefore needs the same uniform scale as the image. Keeping this
 * conversion explicit prevents a locally authored mask from being rejected by
 * the project-scoped upload boundary because it still has the pre-upload size.
 */
export function resizeMaskDocument(
  document: MaskDocument,
  width: number,
  height: number,
): MaskDocument {
  const target = createMaskDocument(width, height);
  const targetWidth = target.width;
  const targetHeight = target.height;
  if (targetWidth === document.width && targetHeight === document.height) {
    return cloneMaskDocument(document);
  }
  const scale = Math.min(targetWidth / document.width, targetHeight / document.height);
  return createMaskDocument(
    targetWidth,
    targetHeight,
    document.strokes.map((stroke) => ({
      ...cloneStroke(stroke),
      settings: normalizeBrushSettings({
        ...stroke.settings,
        size: stroke.settings.size * scale,
      }),
    })),
  );
}

/**
 * Add points between two pointer samples.  A pointer event stream is allowed
 * to skip several pixels, especially on a trackpad.  Interpolating by source
 * pixel distance keeps a stroke continuous at every preview scale.
 */
export function appendInterpolatedPoints(
  points: readonly NormalizedPoint[],
  nextPoint: NormalizedPoint,
  width: number,
  height: number,
  settings: Pick<MaskBrushSettings, "size">,
): NormalizedPoint[] {
  const next = normalizePoint(nextPoint);
  if (!points.length) return [next];
  const previous = points[points.length - 1];
  const distance = Math.hypot(
    (next.x - previous.x) * width,
    (next.y - previous.y) * height,
  );
  if (distance === 0) return [...points];

  // 18% overlap prevents visible pinholes even when the browser coalesces
  // pointer events. A minimum of one source pixel keeps tiny brushes usable.
  const spacing = Math.max(1, Math.max(0.5, settings.size) * 0.18);
  const steps = Math.max(1, Math.ceil(distance / spacing));
  const result = [...points];
  for (let index = 1; index <= steps; index += 1) {
    const progress = index / steps;
    result.push({
      x: previous.x + (next.x - previous.x) * progress,
      y: previous.y + (next.y - previous.y) * progress,
    });
  }
  return result;
}

export function appendStrokePoint(
  stroke: MaskStroke,
  point: NormalizedPoint,
  width: number,
  height: number,
): MaskStroke {
  const settings = normalizeBrushSettings(stroke.settings);
  return {
    ...stroke,
    settings,
    points: appendInterpolatedPoints(stroke.points, point, width, height, settings),
  };
}

export function replaceStroke(
  document: MaskDocument,
  strokeIndex: number,
  stroke: MaskStroke,
): MaskDocument {
  if (strokeIndex < 0 || strokeIndex >= document.strokes.length) return document;
  const strokes = document.strokes.slice();
  strokes[strokeIndex] = cloneStroke(stroke);
  return { ...document, strokes };
}
