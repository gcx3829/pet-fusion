import {
  forwardRef,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  appendStrokePoint,
  cloneMaskDocument,
  createMaskDocument,
  DEFAULT_MASK_BRUSH,
  normalizeBrushSettings,
  normalizePoint,
  replaceStroke,
  resizeMaskDocument,
  type MaskBrushSettings,
  type MaskDocument,
  type MaskStroke,
  type MaskStrokeTool,
  type NormalizedPoint,
} from "../../lib/maskDocument";
import { exportMaskFile, exportMaskPng, rasterizeMask } from "../../lib/maskRasterizer";
import type { CropMapping } from "../../types";

export interface MaskBrushEditorProps {
  /** The immutable source image shown in the upper layer. */
  originalSrc?: string | null;
  /** The generated image shown below the source image. */
  generatedSrc?: string | null;
  /**
   * Mapping for a model-sized raw candidate. When present, only the mapping's
   * content box is drawn and it is placed into the full-resolution crop box;
   * the candidate is never stretched over the whole source photograph.
   */
  generatedCropMapping?: CropMapping | null;
  /** Original image dimensions. Strokes and export use this coordinate space. */
  width: number;
  height: number;
  initialDocument?: MaskDocument;
  /** Guidance uses the same local editor but renders a colored editable-area overlay. */
  mode?: "fusion" | "guidance";
  /** Hide the local PNG download action when a parent owns the upload boundary. */
  showExportButton?: boolean;
  disabled?: boolean;
  className?: string;
  onDocumentChange?: (document: MaskDocument) => void;
  /** Fires only for completed user edits, never for placement-driven defaults. */
  onUserEdit?: (action: "stroke" | "undo" | "redo" | "clear" | "reset") => void;
  /** Optional local hand-off. This callback must decide whether to upload. */
  onExportFile?: (file: File) => void | Promise<void>;
  onHistoryChange?: (state: { canUndo: boolean; canRedo: boolean }) => void;
  /** Optional shell-level controls. Internal controls remain available for legacy embeddings. */
  controlledTool?: MaskStrokeTool;
  controlledBrush?: Partial<MaskBrushSettings>;
  /** Workbench mode keeps every control in the shared top toolbar. */
  showChrome?: boolean;
}

export interface MaskBrushEditorHandle {
  getDocument: () => MaskDocument;
  exportMaskPng: () => Promise<Blob>;
  exportMaskFile: (fileName?: string) => Promise<File>;
  undo: () => void;
  redo: () => void;
  clear: () => void;
  reset: () => void;
}

interface HistoryState {
  past: MaskDocument[];
  present: MaskDocument;
  future: MaskDocument[];
}

interface ActiveStroke {
  pointerId: number;
  strokeIndex: number;
  before: MaskDocument;
}

const MAX_HISTORY_STEPS = 100;

function appendHistoryStep(
  documents: readonly MaskDocument[],
  document: MaskDocument,
): MaskDocument[] {
  return [...documents, document].slice(-MAX_HISTORY_STEPS);
}

function positiveDimension(value: number): number {
  return Number.isFinite(value) && value > 0 ? Math.max(1, Math.round(value)) : 1;
}

function fitPreview(width: number, height: number): { width: number; height: number } {
  const scale = Math.min(1, 960 / width, 700 / height);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

function documentForSource(
  sourceWidth: number,
  sourceHeight: number,
  initialDocument?: MaskDocument,
): MaskDocument {
  if (!initialDocument) return createMaskDocument(sourceWidth, sourceHeight);
  return resizeMaskDocument(initialDocument, sourceWidth, sourceHeight);
}

function drawCheckerboard(
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  const cell = Math.max(8, Math.round(Math.min(width, height) / 18));
  context.fillStyle = "#171411";
  context.fillRect(0, 0, width, height);
  context.fillStyle = "rgba(235, 220, 190, 0.08)";
  for (let y = 0; y < height; y += cell) {
    for (let x = 0; x < width; x += cell) {
      if ((x / cell + y / cell) % 2 === 0) context.fillRect(x, y, cell, cell);
    }
  }
}

function renderAlphaCanvas(
  mask: ReturnType<typeof rasterizeMask>,
  existing: HTMLCanvasElement | null,
): HTMLCanvasElement | null {
  const canvas = existing ?? document.createElement("canvas");
  canvas.width = mask.width;
  canvas.height = mask.height;
  const context = canvas.getContext("2d", { alpha: true });
  if (!context) return null;
  const pixels = context.createImageData(mask.width, mask.height);
  for (let index = 0; index < mask.alpha.length; index += 1) {
    pixels.data[index * 4 + 3] = mask.alpha[index];
  }
  context.putImageData(pixels, 0, 0);
  return canvas;
}

function renderGuidanceOverlay(
  mask: ReturnType<typeof rasterizeMask>,
  existing: HTMLCanvasElement | null,
): HTMLCanvasElement | null {
  const canvas = existing ?? document.createElement("canvas");
  canvas.width = mask.width;
  canvas.height = mask.height;
  const context = canvas.getContext("2d", { alpha: true });
  if (!context) return null;
  const pixels = context.createImageData(mask.width, mask.height);
  for (let index = 0; index < mask.alpha.length; index += 1) {
    const offset = index * 4;
    pixels.data[offset] = 75;
    pixels.data[offset + 1] = 211;
    pixels.data[offset + 2] = 214;
    // Keep the overlay legible without obscuring the source photograph.
    pixels.data[offset + 3] = Math.round(mask.alpha[index] * 0.42);
  }
  context.putImageData(pixels, 0, 0);
  return canvas;
}

function loadImage(source: string | null | undefined): Promise<HTMLImageElement | null> {
  if (!source) return Promise.resolve(null);
  return new Promise((resolve) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = source;
  });
}

export function drawGeneratedCandidate(
  context: CanvasRenderingContext2D,
  image: HTMLImageElement,
  mapping: CropMapping | null | undefined,
  sourceWidth: number,
  sourceHeight: number,
  previewWidth: number,
  previewHeight: number,
): boolean {
  const imageWidth = image.naturalWidth || image.width;
  const imageHeight = image.naturalHeight || image.height;

  // Keep the same precedence as CompositeFloorService._candidate_on_full_canvas:
  // a full-resolution raw is already authoritative and must not be cropped a
  // second time merely because a legacy row also carries crop_mapping.
  if (imageWidth === sourceWidth && imageHeight === sourceHeight) {
    context.drawImage(image, 0, 0, previewWidth, previewHeight);
    return true;
  }
  if (mapping) {
    const contentWidth = mapping.canvas_width - mapping.padding.left - mapping.padding.right;
    const contentHeight = mapping.canvas_height - mapping.padding.top - mapping.padding.bottom;
    const mappingValid = (
      mapping.full_width === sourceWidth
      && mapping.full_height === sourceHeight
      && mapping.canvas_width === imageWidth
      && mapping.canvas_height === imageHeight
      && contentWidth > 0
      && contentHeight > 0
      && mapping.crop_box.x >= 0
      && mapping.crop_box.y >= 0
      && mapping.crop_box.x + mapping.crop_box.width <= sourceWidth
      && mapping.crop_box.y + mapping.crop_box.height <= sourceHeight
    );
    if (!mappingValid) return false;
    context.drawImage(
      image,
      mapping.padding.left,
      mapping.padding.top,
      contentWidth,
      contentHeight,
      mapping.crop_box.x * previewWidth / sourceWidth,
      mapping.crop_box.y * previewHeight / sourceHeight,
      mapping.crop_box.width * previewWidth / sourceWidth,
      mapping.crop_box.height * previewHeight / sourceHeight,
    );
    return true;
  }

  // A candidate without a crop mapping is valid only when it is already a
  // full-resolution image. Stretching a model crop here would mis-register
  // the subject and make the local Fusion preview materially misleading.
  return false;
}

function pointerToPoint(
  event: Pick<PointerEvent, "clientX" | "clientY">,
  canvas: HTMLCanvasElement,
): NormalizedPoint {
  const bounds = canvas.getBoundingClientRect();
  const width = bounds.width || canvas.width || 1;
  const height = bounds.height || canvas.height || 1;
  return normalizePoint({
    x: (event.clientX - bounds.left) / width,
    y: (event.clientY - bounds.top) / height,
  });
}

function safeCapture(canvas: HTMLCanvasElement, pointerId: number): void {
  if (typeof canvas.setPointerCapture === "function") {
    try {
      canvas.setPointerCapture(pointerId);
    } catch {
      // jsdom and a few embedded webviews expose the method without capture.
    }
  }
}

function safeRelease(canvas: HTMLCanvasElement, pointerId: number): void {
  if (typeof canvas.releasePointerCapture === "function") {
    try {
      canvas.releasePointerCapture(pointerId);
    } catch {
      // See safeCapture: releasing a missing capture is harmless.
    }
  }
}

export const MaskBrushEditor = forwardRef<MaskBrushEditorHandle, MaskBrushEditorProps>(
  function MaskBrushEditor({
    originalSrc,
    generatedSrc,
    generatedCropMapping,
    width,
    height,
    initialDocument,
    mode = "fusion",
    showExportButton = true,
    disabled = false,
    className = "",
    onDocumentChange,
    onUserEdit,
    onExportFile,
    onHistoryChange,
    controlledTool,
    controlledBrush,
    showChrome = true,
  }, ref) {
    const sourceWidth = positiveDimension(width);
    const sourceHeight = positiveDimension(height);
    const instructionsId = useId();
    const previewSize = useMemo(
      () => fitPreview(sourceWidth, sourceHeight),
      [sourceHeight, sourceWidth],
    );
    const initialRef = useRef(documentForSource(sourceWidth, sourceHeight, initialDocument));
    const sourceDimensionsRef = useRef({ width: sourceWidth, height: sourceHeight });
    const initialDocumentKey = JSON.stringify(initialDocument ?? null);
    const initialDocumentKeyRef = useRef(initialDocumentKey);
    const [history, setHistory] = useState<HistoryState>(() => ({
      past: [],
      present: cloneMaskDocument(initialRef.current),
      future: [],
    }));
    const presentRef = useRef(history.present);
    presentRef.current = history.present;
    const [tool, setTool] = useState<MaskStrokeTool>("paint");
    const [brush, setBrush] = useState<MaskBrushSettings>(() => normalizeBrushSettings({
      ...DEFAULT_MASK_BRUSH,
      size: Math.max(4, Math.min(DEFAULT_MASK_BRUSH.size, Math.max(sourceWidth, sourceHeight) * 0.16)),
    }));
    // Shell controls are authoritative for the current stroke. Reading them
    // synchronously avoids the one-render race where a fast erase/paint
    // gesture could still use the previously selected tool or brush values.
    const effectiveTool = controlledTool ?? tool;
    const effectiveBrush = useMemo(
      () => normalizeBrushSettings({ ...brush, ...controlledBrush }),
      [brush, controlledBrush?.feather, controlledBrush?.flow, controlledBrush?.size],
    );
    const [cursor, setCursor] = useState<NormalizedPoint | null>(null);
    const [imageRevision, setImageRevision] = useState(0);
    const [exporting, setExporting] = useState(false);
    const [exportError, setExportError] = useState<string | null>(null);

    useEffect(() => {
      if (disabled) setCursor(null);
    }, [disabled]);

    const stageRef = useRef<HTMLDivElement>(null);
    const generatedCanvasRef = useRef<HTMLCanvasElement>(null);
    const originalCanvasRef = useRef<HTMLCanvasElement>(null);
    const guidanceOverlayCanvasRef = useRef<HTMLCanvasElement | null>(null);
    const pointerCanvasRef = useRef<HTMLCanvasElement>(null);
    const generatedImageRef = useRef<HTMLImageElement | null>(null);
    const originalImageRef = useRef<HTMLImageElement | null>(null);
    const alphaCanvasRef = useRef<HTMLCanvasElement | null>(null);
    const activeStrokeRef = useRef<ActiveStroke | null>(null);

    useEffect(() => {
      const previous = sourceDimensionsRef.current;
      const dimensionsChanged = previous.width !== sourceWidth || previous.height !== sourceHeight;
      const initialChanged = initialDocumentKeyRef.current !== initialDocumentKey;
      if (!dimensionsChanged && !initialChanged) return;
      sourceDimensionsRef.current = { width: sourceWidth, height: sourceHeight };
      const nextInitial = documentForSource(sourceWidth, sourceHeight, initialDocument);
      initialRef.current = nextInitial;
      initialDocumentKeyRef.current = initialDocumentKey;
      if (activeStrokeRef.current && pointerCanvasRef.current) {
        safeRelease(pointerCanvasRef.current, activeStrokeRef.current.pointerId);
      }
      activeStrokeRef.current = null;
      setHistory({ past: [], present: cloneMaskDocument(nextInitial), future: [] });
    }, [initialDocument, initialDocumentKey, sourceHeight, sourceWidth]);

    useEffect(() => {
      let cancelled = false;
      void Promise.all([loadImage(originalSrc), loadImage(generatedSrc)])
        .then(([original, generated]) => {
          if (cancelled) return;
          originalImageRef.current = original;
          generatedImageRef.current = generated;
          setImageRevision((revision) => revision + 1);
        });
      return () => {
        cancelled = true;
      };
    }, [generatedSrc, originalSrc]);

    useEffect(() => {
      onDocumentChange?.(cloneMaskDocument(history.present));
    }, [history.present, onDocumentChange]);

    useEffect(() => {
      const generatedCanvas = generatedCanvasRef.current;
      const originalCanvas = originalCanvasRef.current;
      const pointerCanvas = pointerCanvasRef.current;
      if (!generatedCanvas || !originalCanvas || !pointerCanvas) return;

      for (const canvas of [generatedCanvas, originalCanvas, pointerCanvas]) {
        canvas.width = previewSize.width;
        canvas.height = previewSize.height;
      }
      const generatedContext = generatedCanvas.getContext("2d");
      const originalContext = originalCanvas.getContext("2d");
      if (!generatedContext || !originalContext) return;
      const mask = rasterizeMask(history.present, previewSize.width, previewSize.height);

      generatedContext.clearRect(0, 0, previewSize.width, previewSize.height);
      let generatedRendered = false;
      if (generatedImageRef.current) {
        // The backend's crop rebase starts from the immutable background and
        // overlays the model crop onto it. Mirroring that base here prevents
        // painting outside the raw crop from revealing an artificial void.
        if (generatedCropMapping && originalImageRef.current) {
          generatedContext.drawImage(
            originalImageRef.current,
            0,
            0,
            previewSize.width,
            previewSize.height,
          );
        }
        generatedRendered = drawGeneratedCandidate(
          generatedContext,
          generatedImageRef.current,
          generatedCropMapping,
          sourceWidth,
          sourceHeight,
          previewSize.width,
          previewSize.height,
        );
      }
      if (!generatedRendered && !(generatedCropMapping && originalImageRef.current)) {
        drawCheckerboard(generatedContext, previewSize.width, previewSize.height);
      }

      originalContext.clearRect(0, 0, previewSize.width, previewSize.height);
      if (originalImageRef.current) {
        originalContext.drawImage(
          originalImageRef.current,
          0,
          0,
          previewSize.width,
          previewSize.height,
        );
        if (mode === "guidance") {
          // Guidance is a soft model hint, not a pixel lock: keep the source
          // photo visible and tint only the painted editable region.
          const overlay = renderGuidanceOverlay(mask, guidanceOverlayCanvasRef.current);
          if (overlay) {
            guidanceOverlayCanvasRef.current = overlay;
            originalContext.drawImage(overlay, 0, 0);
          }
        } else {
          // Remove the painted alpha from the upper source layer. Using Canvas
          // compositing avoids sampling source pixels, so API-hosted images do
          // not make the live preview fail when the canvas is origin-tainted.
          const alphaCanvas = renderAlphaCanvas(mask, alphaCanvasRef.current);
          if (alphaCanvas) {
            alphaCanvasRef.current = alphaCanvas;
            const previousOperation = originalContext.globalCompositeOperation;
            originalContext.globalCompositeOperation = "destination-out";
            originalContext.drawImage(alphaCanvas, 0, 0);
            originalContext.globalCompositeOperation = previousOperation;
          }
        }
      } else {
        originalContext.fillStyle = "rgba(233, 223, 199, 0.08)";
        originalContext.fillRect(0, 0, previewSize.width, previewSize.height);
      }
    }, [
      generatedCropMapping,
      history.present,
      imageRevision,
      mode,
      previewSize.height,
      previewSize.width,
      sourceHeight,
      sourceWidth,
    ]);

    useEffect(() => {
      const canvas = pointerCanvasRef.current;
      if (!canvas) return;
      const context = canvas.getContext("2d");
      if (!context) return;
      context.clearRect(0, 0, previewSize.width, previewSize.height);
      if (disabled || !cursor) return;
      const radiusX = effectiveBrush.size * previewSize.width / sourceWidth / 2;
      const radiusY = effectiveBrush.size * previewSize.height / sourceHeight / 2;
      context.beginPath();
      context.ellipse(
        cursor.x * previewSize.width,
        cursor.y * previewSize.height,
        Math.max(2, radiusX),
        Math.max(2, radiusY),
        0,
        0,
        Math.PI * 2,
      );
      context.strokeStyle = effectiveTool === "paint" ? "rgba(221, 169, 95, 0.95)" : "rgba(141, 174, 176, 0.95)";
      context.lineWidth = 1.5;
      context.stroke();
    }, [cursor, disabled, effectiveBrush.size, effectiveTool, previewSize.height, previewSize.width, sourceHeight, sourceWidth]);

    const finishStroke = (event: ReactPointerEvent<HTMLCanvasElement>) => {
      const active = activeStrokeRef.current;
      if (!active || active.pointerId !== event.pointerId) return;
      activeStrokeRef.current = null;
      safeRelease(event.currentTarget, event.pointerId);
      const finalPoint = pointerToPoint(event, event.currentTarget);
      setHistory((current) => {
        const currentStroke = current.present.strokes[active.strokeIndex];
        const present = currentStroke
          ? replaceStroke(
              current.present,
              active.strokeIndex,
              appendStrokePoint(currentStroke, finalPoint, sourceWidth, sourceHeight),
            )
          : current.present;
        return {
          past: appendHistoryStep(current.past, active.before),
          present,
          future: [],
        };
      });
      onUserEdit?.("stroke");
    };

    const cancelPointerStroke = (event: ReactPointerEvent<HTMLCanvasElement>) => {
      const active = activeStrokeRef.current;
      if (!active || active.pointerId !== event.pointerId) return;
      activeStrokeRef.current = null;
      safeRelease(event.currentTarget, event.pointerId);
      setHistory((current) => ({ ...current, present: active.before }));
    };

    const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
      // A second touch belongs to a pinch gesture. Roll back the provisional
      // first-finger stroke so zooming never leaves an accidental mask dab.
      if (event.pointerType === "touch" && event.isPrimary === false) {
        cancelActiveStroke();
        return;
      }
      // Some test harnesses and embedded pointer implementations omit
      // `button` for a primary pointer. Only an explicit non-primary button
      // should be ignored.
      if (
        disabled
        || activeStrokeRef.current
        || (event.button !== undefined && event.button !== 0)
      ) return;
      event.preventDefault();
      const canvas = event.currentTarget;
      const point = pointerToPoint(event, canvas);
      const stroke: MaskStroke = {
        tool: effectiveTool,
        points: [point],
        settings: effectiveBrush,
      };
      // Mask documents are updated immutably. Keeping the previous reference
      // makes undo O(1) here and avoids deep-copying all old strokes per dab.
      const before = presentRef.current;
      const strokeIndex = before.strokes.length;
      activeStrokeRef.current = {
        pointerId: event.pointerId,
        strokeIndex,
        before,
      };
      setHistory((current) => ({
        ...current,
        present: {
          ...current.present,
          strokes: [...current.present.strokes, stroke],
        },
      }));
      setCursor(point);
      safeCapture(canvas, event.pointerId);
    };

    const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
      const nativeEvent = event.nativeEvent;
      const coalesced = typeof nativeEvent.getCoalescedEvents === "function"
        ? nativeEvent.getCoalescedEvents()
        : [];
      const points = [...coalesced, nativeEvent].map((sample) => pointerToPoint(sample, event.currentTarget));
      const point = points[points.length - 1] ?? pointerToPoint(event, event.currentTarget);
      setCursor(point);
      const active = activeStrokeRef.current;
      if (!active || active.pointerId !== event.pointerId) return;
      event.preventDefault();
      setHistory((current) => {
        const currentStroke = current.present.strokes[active.strokeIndex];
        if (!currentStroke) return current;
        const nextStroke = points.reduce(
          (stroke, sample) => appendStrokePoint(stroke, sample, sourceWidth, sourceHeight),
          currentStroke,
        );
        return {
          ...current,
          present: replaceStroke(current.present, active.strokeIndex, nextStroke),
        };
      });
    };

    const handleCanvasKeyDown = (event: ReactKeyboardEvent<HTMLCanvasElement>) => {
      if (disabled) return;
      const currentCursor = cursor ?? { x: 0.5, y: 0.5 };
      const stepPixels = event.shiftKey ? 10 : 1;
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault();
        setCursor(normalizePoint({
          x: currentCursor.x
            + (event.key === "ArrowLeft" ? -stepPixels / sourceWidth : 0)
            + (event.key === "ArrowRight" ? stepPixels / sourceWidth : 0),
          y: currentCursor.y
            + (event.key === "ArrowUp" ? -stepPixels / sourceHeight : 0)
            + (event.key === "ArrowDown" ? stepPixels / sourceHeight : 0),
        }));
        return;
      }
      if (event.key !== " " && event.key !== "Enter") return;
      event.preventDefault();
      const stroke: MaskStroke = {
        tool: effectiveTool,
        points: [currentCursor],
        settings: effectiveBrush,
      };
      setHistory((current) => ({
        past: appendHistoryStep(current.past, current.present),
        present: {
          ...current.present,
          strokes: [...current.present.strokes, stroke],
        },
        future: [],
      }));
      onUserEdit?.("stroke");
    };

    const cancelActiveStroke = () => {
      const active = activeStrokeRef.current;
      if (!active) return;
      activeStrokeRef.current = null;
      if (pointerCanvasRef.current) {
        safeRelease(pointerCanvasRef.current, active.pointerId);
      }
      setHistory((current) => ({ ...current, present: active.before }));
    };

    const undo = () => {
      cancelActiveStroke();
      setHistory((current) => {
        const previous = current.past[current.past.length - 1];
        if (!previous) return current;
        return {
          past: current.past.slice(0, -1),
          present: previous,
          future: [current.present, ...current.future],
        };
      });
      if (canUndo) onUserEdit?.("undo");
    };

    const redo = () => {
      cancelActiveStroke();
      setHistory((current) => {
        const next = current.future[0];
        if (!next) return current;
        return {
          past: appendHistoryStep(current.past, current.present),
          present: next,
          future: current.future.slice(1),
        };
      });
      if (canRedo) onUserEdit?.("redo");
    };

    const clear = () => {
      cancelActiveStroke();
      setHistory((current) => {
        if (!current.present.strokes.length) return current;
        return {
          past: appendHistoryStep(current.past, current.present),
          present: createMaskDocument(sourceWidth, sourceHeight),
          future: [],
        };
      });
      if (history.present.strokes.length) onUserEdit?.("clear");
    };

    const reset = () => {
      cancelActiveStroke();
      setHistory({
        past: [],
        present: cloneMaskDocument(initialRef.current),
        future: [],
      });
      onUserEdit?.("reset");
    };

    useImperativeHandle(ref, () => ({
      getDocument: () => cloneMaskDocument(presentRef.current),
      exportMaskPng: () => exportMaskPng(presentRef.current),
      exportMaskFile: (fileName?: string) => exportMaskFile(presentRef.current, fileName),
      undo,
      redo,
      clear,
      reset,
    }));

    const handleExport = async () => {
      setExporting(true);
      setExportError(null);
      try {
        const file = await exportMaskFile(presentRef.current);
        if (onExportFile) {
          await onExportFile(file);
        } else {
          const objectUrl = URL.createObjectURL(file);
          const anchor = document.createElement("a");
          anchor.href = objectUrl;
          anchor.download = file.name;
          anchor.click();
          URL.revokeObjectURL(objectUrl);
        }
      } catch (error) {
        setExportError(error instanceof Error ? error.message : "Mask PNG 导出失败");
      } finally {
        setExporting(false);
      }
    };

    const updateBrush = (key: keyof MaskBrushSettings, value: number) => {
      setBrush((current) => normalizeBrushSettings({ ...current, [key]: value }));
    };
    const maxSize = Math.max(4, Math.round(Math.max(sourceWidth, sourceHeight)));
    const canUndo = history.past.length > 0;
    const canRedo = history.future.length > 0;

    useEffect(() => {
      onHistoryChange?.({ canUndo, canRedo });
    }, [canRedo, canUndo, onHistoryChange]);

    return (
      <div className={`mask-brush-editor ${className}`.trim()}>
        {showChrome && <div className="mask-brush-toolbar" aria-label="Mask 画笔工具">
          <div className="mask-tool-group" role="group" aria-label="绘制模式">
            <button
              type="button"
              className="mask-tool-button"
              aria-pressed={effectiveTool === "paint"}
              onClick={() => setTool("paint")}
              disabled={disabled}
            >
              绘制
            </button>
            <button
              type="button"
              className="mask-tool-button"
              aria-pressed={effectiveTool === "erase"}
              onClick={() => setTool("erase")}
              disabled={disabled}
            >
              擦除
            </button>
          </div>
          <div className="mask-history-group" role="group" aria-label="笔划历史">
            <button type="button" aria-label="撤销笔划" onClick={undo} disabled={disabled || !canUndo}>撤销</button>
            <button type="button" aria-label="重做笔划" onClick={redo} disabled={disabled || !canRedo}>重做</button>
            <button type="button" aria-label="清空 Mask" onClick={clear} disabled={disabled || !history.present.strokes.length}>清空</button>
          </div>
        </div>}

        {showChrome && <div className="mask-brush-controls">
          <label>
            <span>笔刷大小 <output>{Math.round(effectiveBrush.size)} px</output></span>
            <input
              aria-label="笔刷大小"
              type="range"
              min="1"
              max={maxSize}
              step="1"
              value={Math.min(maxSize, effectiveBrush.size)}
              onChange={(event) => updateBrush("size", Number(event.target.value))}
              disabled={disabled}
            />
          </label>
          <label>
            <span>流量 <output>{Math.round(effectiveBrush.flow * 100)}%</output></span>
            <input
              aria-label="笔刷流量"
              type="range"
              min="1"
              max="100"
              step="1"
              value={Math.round(effectiveBrush.flow * 100)}
              onChange={(event) => updateBrush("flow", Number(event.target.value) / 100)}
              disabled={disabled}
            />
          </label>
          <label>
            <span>羽化 <output>{Math.round(effectiveBrush.feather * 100)}%</output></span>
            <input
              aria-label="笔刷羽化"
              type="range"
              min="0"
              max="100"
              step="1"
              value={Math.round(effectiveBrush.feather * 100)}
              onChange={(event) => updateBrush("feather", Number(event.target.value) / 100)}
              disabled={disabled}
            />
          </label>
          {showExportButton && (
            <button
              type="button"
              className="mask-export-button"
              onClick={() => void handleExport()}
              disabled={disabled || exporting}
            >
              {exporting ? "编码中…" : "导出本地 Mask PNG"}
            </button>
          )}
        </div>}

        <div
          ref={stageRef}
          className="mask-brush-stage"
          style={{
            aspectRatio: `${previewSize.width} / ${previewSize.height}`,
            width: `${previewSize.width}px`,
            height: `${previewSize.height}px`,
            minHeight: 0,
            "--mask-preview-width": `${previewSize.width}px`,
            "--mask-preview-ratio": previewSize.width / previewSize.height,
          } as CSSProperties}
          data-mask-width={sourceWidth}
          data-mask-height={sourceHeight}
        >
          <canvas ref={generatedCanvasRef} className="mask-layer mask-layer-generated" aria-hidden="true" />
          <canvas ref={originalCanvasRef} className="mask-layer mask-layer-original" aria-hidden="true" />
          <canvas
            ref={pointerCanvasRef}
            className="mask-layer mask-layer-input"
            aria-label="Mask 画布，按住鼠标绘制"
            onPointerDown={handlePointerDown}
            onPointerMove={disabled ? undefined : handlePointerMove}
            onPointerUp={finishStroke}
            onPointerCancel={cancelPointerStroke}
            onLostPointerCapture={cancelPointerStroke}
            onPointerLeave={() => setCursor(null)}
            onPointerEnter={disabled ? undefined : (event) => setCursor(pointerToPoint(event, event.currentTarget))}
            onKeyDown={handleCanvasKeyDown}
            role="application"
            aria-describedby={instructionsId}
            aria-disabled={disabled}
            tabIndex={disabled ? -1 : 0}
          />
          {showChrome && <div className="mask-layer-label mask-layer-label-bottom">
            {mode === "guidance" ? "原片 · 观察底图" : "生成图 · alpha 255"}
          </div>}
          {showChrome && <div className="mask-layer-label mask-layer-label-top">
            {mode === "guidance" ? "Guidance · 可编辑区域" : "原图 · alpha 0"}
          </div>}
        </div>
        {showChrome && <div className="mask-brush-footer">
          <span id={instructionsId}>按住鼠标或触控笔绘制；键盘方向键移动，空格绘制；笔迹只保存在本地浏览器内存</span>
          <span>{mode === "guidance" ? "绘制后：0 = 保持原图，255 = 模型可编辑" : "绘制后：0 = 原图，255 = 生成图"}</span>
          <span>{history.present.strokes.length} 个笔划</span>
        </div>}
        {exportError ? <p className="mask-export-error" role="alert">{exportError}</p> : null}
      </div>
    );
  },
);
