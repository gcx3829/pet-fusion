import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  MaskBrushEditor,
  type MaskBrushEditorHandle,
} from "../mask/MaskBrushEditor";
import {
  createPlacementGuidanceDocument,
  maskDocumentFingerprint,
  type MaskDocument,
} from "../../lib/maskDocument";
import type { PlacementIntent } from "../../types";

export interface GuidanceMaskEditorState {
  document: MaskDocument;
  documentHash: string;
  dirty: boolean;
}

export interface GuidanceMaskEditorHandle {
  getDocument: () => MaskDocument;
  getState: () => GuidanceMaskEditorState;
  exportMaskFile: (fileName?: string) => Promise<File>;
  resetToPlacement: () => void;
}

interface GuidanceMaskEditorProps {
  backgroundSrc: string;
  width?: number;
  height?: number;
  placement: PlacementIntent;
  disabled?: boolean;
  locked?: boolean;
  onStateChange?: (state: GuidanceMaskEditorState) => void;
}

interface ImageSize {
  width: number;
  height: number;
}

function validDimension(value: number | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function loadImageSize(source: string): Promise<ImageSize | null> {
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => image.naturalWidth && image.naturalHeight
      ? resolve({ width: image.naturalWidth, height: image.naturalHeight })
      : resolve(null);
    image.onerror = () => resolve(null);
    image.src = source;
  });
}

function dimensionsOf(
  suppliedWidth: number | undefined,
  suppliedHeight: number | undefined,
  loaded: ImageSize | null,
): ImageSize | null {
  if (validDimension(suppliedWidth) && validDimension(suppliedHeight)) {
    return { width: suppliedWidth, height: suppliedHeight };
  }
  return loaded;
}

export const GuidanceMaskEditor = forwardRef<GuidanceMaskEditorHandle, GuidanceMaskEditorProps>(
  function GuidanceMaskEditor({
    backgroundSrc,
    width,
    height,
    placement,
    disabled = false,
    locked = false,
    onStateChange,
  }, ref) {
    const [loadedSize, setLoadedSize] = useState<ImageSize | null>(null);
    const [baseline, setBaseline] = useState<MaskDocument | null>(null);
    const [present, setPresent] = useState<MaskDocument | null>(null);
    const [dirty, setDirty] = useState(false);
    const [placementWarning, setPlacementWarning] = useState(false);
    const [resetVersion, setResetVersion] = useState(0);
    const brushRef = useRef<MaskBrushEditorHandle | null>(null);
    const userEditPendingRef = useRef(false);
    const baselineRef = useRef<MaskDocument | null>(baseline);
    baselineRef.current = baseline;

    useEffect(() => {
      let cancelled = false;
      void loadImageSize(backgroundSrc).then((size) => {
        if (!cancelled) setLoadedSize(size);
      });
      return () => {
        cancelled = true;
      };
    }, [backgroundSrc]);

    const dimensions = dimensionsOf(width, height, loadedSize);
    const defaultDocument = useMemo(() => {
      if (!dimensions) return null;
      return createPlacementGuidanceDocument(dimensions.width, dimensions.height, placement);
    }, [dimensions?.height, dimensions?.width, placement.height, placement.width, placement.x, placement.y]);
    const defaultHash = defaultDocument ? maskDocumentFingerprint(defaultDocument) : null;
    const baselineHash = baseline ? maskDocumentFingerprint(baseline) : null;

    // Before a user paints, placement edits update the seed mask so the old
    // placement-only Search payload remains behaviorally equivalent. Once a
    // hand edit exists, preserve it and surface an explicit reset affordance.
    useEffect(() => {
      if (!defaultDocument) return;
      if (!dirty || !baseline) {
        setBaseline(defaultDocument);
        setPlacementWarning(false);
        return;
      }
      if (defaultHash !== baselineHash) setPlacementWarning(true);
    }, [baseline, baselineHash, defaultDocument, defaultHash, dirty]);

    useEffect(() => {
      if (!present || !baseline) return;
      onStateChange?.({
        document: present,
        documentHash: maskDocumentFingerprint(present),
        dirty,
      });
    }, [baseline, dirty, onStateChange, present]);

    const handleDocumentChange = useCallback((document: MaskDocument) => {
      setPresent(document);
      if (!userEditPendingRef.current) return;
      userEditPendingRef.current = false;
      const currentBaseline = baselineRef.current;
      setDirty(Boolean(currentBaseline && maskDocumentFingerprint(document) !== maskDocumentFingerprint(currentBaseline)));
    }, []);

    const handleUserEdit = useCallback(() => {
      userEditPendingRef.current = true;
    }, []);

    const resetToPlacement = () => {
      if (!defaultDocument) return;
      userEditPendingRef.current = false;
      setBaseline(defaultDocument);
      setPresent(defaultDocument);
      setDirty(false);
      setPlacementWarning(false);
      setResetVersion((value) => value + 1);
    };

    useImperativeHandle(ref, () => ({
      getDocument: () => brushRef.current?.getDocument() ?? present ?? defaultDocument ?? {
        version: 1,
        width: Math.max(1, Math.round(width ?? 1)),
        height: Math.max(1, Math.round(height ?? 1)),
        strokes: [],
      },
      getState: () => {
        const document = brushRef.current?.getDocument() ?? present ?? defaultDocument;
        if (!document) {
          return {
            document: { version: 1, width: 1, height: 1, strokes: [] },
            documentHash: "empty",
            dirty: false,
          };
        }
        return {
          document,
          documentHash: maskDocumentFingerprint(document),
          dirty,
        };
      },
      exportMaskFile: (fileName?: string) => {
        if (!brushRef.current) return Promise.reject(new Error("Guidance Mask 画布尚未准备好"));
        return brushRef.current.exportMaskFile(fileName);
      },
      resetToPlacement,
    }), [defaultDocument, dirty, height, present, resetToPlacement, width]);

    const lockedLabel = locked
      ? "搜索已启动，Guidance Mask 已锁定；如需修改请新建 Search。"
      : "画笔编辑只保存在浏览器本地；点击开始 Search 时才会上传自定义 Mask。";

    return (
      <section className="panel guidance-editor" aria-labelledby="guidance-heading">
        <div className="panel-heading guidance-heading-row">
          <div>
            <p className="eyebrow">03 / MODEL GUIDANCE</p>
            <h2 id="guidance-heading">Guidance Mask 画笔</h2>
          </div>
          <span className={`guidance-status ${locked ? "is-locked" : dirty ? "is-dirty" : ""}`}>
            {locked ? "SEARCH LOCKED" : dirty ? "CUSTOM MASK" : "BRUSH READY"}
          </span>
        </div>
        <p className="guidance-explainer">
          用画笔标出模型可以重新生成的区域：alpha 0 表示尽量保持原图，alpha 255 表示允许编辑。
          这是软引导，不是像素锁；GPT Image 2 仍会根据上下文处理毛发、接触阴影和光线。
        </p>
        <p className="guidance-lock-note" role="status">{lockedLabel}</p>
        {!dimensions ? (
          <div className="guidance-loading" role="status">正在读取原片尺寸…</div>
        ) : (
          <MaskBrushEditor
            key={`guidance-brush-${resetVersion}`}
            ref={brushRef}
            originalSrc={backgroundSrc}
            generatedSrc={backgroundSrc}
            width={dimensions.width}
            height={dimensions.height}
            initialDocument={baseline ?? defaultDocument ?? undefined}
            mode="guidance"
            showExportButton={false}
            disabled={disabled || locked}
            onDocumentChange={handleDocumentChange}
            onUserEdit={handleUserEdit}
          />
        )}
        {placementWarning && !locked ? (
          <div className="guidance-warning" role="alert">
            <span>已保留你的手绘区域；位置框变化不会静默覆盖它。</span>
            <button type="button" onClick={resetToPlacement}>重置为当前位置</button>
          </div>
        ) : null}
        <div className="guidance-footer">
          <span>{baseline ? `${baseline.width} × ${baseline.height}px` : "等待原片"}</span>
          <span>{dirty ? "自定义 Mask 将随 Search 提交" : "尚未绘制时沿用兼容默认区域，不上传 Mask"}</span>
          {present && <span>{present.strokes.length} 个笔划</span>}
        </div>
      </section>
    );
  },
);

export function GuidanceMaskEditorPlaceholder({ children }: { children?: ReactNode }) {
  return <div className="guidance-editor-placeholder">{children ?? "选择原片后编辑 Guidance Mask"}</div>;
}
