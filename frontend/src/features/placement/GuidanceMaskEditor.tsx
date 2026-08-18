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
import type { MaskBrushSettings, MaskStrokeTool } from "../../lib/maskDocument";
import {
  createMaskDocument,
  maskDocumentFingerprint,
  type MaskDocument,
} from "../../lib/maskDocument";
import type { PlacementIntent } from "../../types";

export interface GuidanceMaskEditorState {
  document: MaskDocument;
  documentHash: string;
  dirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
}

export interface GuidanceMaskEditorHandle {
  getDocument: () => MaskDocument;
  getState: () => GuidanceMaskEditorState;
  exportMaskFile: (fileName?: string) => Promise<File>;
  resetToPlacement: () => void;
  undo: () => void;
  redo: () => void;
}

interface GuidanceMaskEditorProps {
  backgroundSrc: string;
  width?: number;
  height?: number;
  placement: PlacementIntent;
  disabled?: boolean;
  locked?: boolean;
  onStateChange?: (state: GuidanceMaskEditorState) => void;
  controlledTool?: MaskStrokeTool;
  controlledBrush?: Partial<MaskBrushSettings>;
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
    disabled = false,
    locked = false,
    onStateChange,
    controlledTool,
    controlledBrush,
  }, ref) {
    const [loadedSize, setLoadedSize] = useState<ImageSize | null>(null);
    const [baseline, setBaseline] = useState<MaskDocument | null>(null);
    const [present, setPresent] = useState<MaskDocument | null>(null);
    const [dirty, setDirty] = useState(false);
    const [historyState, setHistoryState] = useState({ canUndo: false, canRedo: false });
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
      return createMaskDocument(dimensions.width, dimensions.height);
    }, [dimensions?.height, dimensions?.width]);

    // Guidance begins empty. Placement remains only a legacy API fallback and
    // must never materialize as an invisible, pre-painted mask in the editor.
    useEffect(() => {
      if (!defaultDocument) return;
      if (!dirty || !baseline) {
        setBaseline(defaultDocument);
      }
    }, [baseline, defaultDocument, dirty]);

    useEffect(() => {
      if (!present || !baseline) return;
      onStateChange?.({
        document: present,
        documentHash: maskDocumentFingerprint(present),
        dirty,
        ...historyState,
      });
    }, [baseline, dirty, historyState, onStateChange, present]);

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
            canUndo: false,
            canRedo: false,
          };
        }
        return {
          document,
          documentHash: maskDocumentFingerprint(document),
          dirty,
          ...historyState,
        };
      },
      exportMaskFile: (fileName?: string) => {
        if (!brushRef.current) return Promise.reject(new Error("Guidance Mask 画布尚未准备好"));
        return brushRef.current.exportMaskFile(fileName);
      },
      resetToPlacement,
      undo: () => brushRef.current?.undo(),
      redo: () => brushRef.current?.redo(),
    }), [defaultDocument, dirty, height, historyState, present, resetToPlacement, width]);

    return (
      <div className="guidance-editor-surface" aria-label="Guidance Mask 画布">
        {!dimensions ? (
          <div className="canvas-status-overlay" role="status">正在读取图片…</div>
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
            controlledTool={controlledTool}
            controlledBrush={controlledBrush}
            showChrome={false}
            onHistoryChange={setHistoryState}
            onDocumentChange={handleDocumentChange}
            onUserEdit={handleUserEdit}
          />
        )}
        {locked && <span className="canvas-status-overlay">MASK LOCKED</span>}
      </div>
    );
  },
);

export function GuidanceMaskEditorPlaceholder({ children }: { children?: ReactNode }) {
  return <div className="guidance-editor-placeholder">{children ?? "选择原片后编辑 Guidance Mask"}</div>;
}
