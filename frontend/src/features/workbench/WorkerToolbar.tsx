import { Icon } from "../../components/Icon";
import type { BrushTool, WorkerMode } from "./useWorkbenchUi";

interface WorkerToolbarProps {
  mode: WorkerMode;
  zoom: number;
  brushTool: BrushTool;
  brushSize: number;
  brushFlow: number;
  brushFeather: number;
  canUndo?: boolean;
  canRedo?: boolean;
  fusionUnlocked?: boolean;
  onUndo?: () => void;
  onRedo?: () => void;
  onBrushToolChange: (tool: BrushTool) => void;
  onBrushSizeChange: (size: number) => void;
  onBrushFlowChange: (flow: number) => void;
  onBrushFeatherChange: (feather: number) => void;
  onZoomChange: (zoom: number) => void;
  onFit: () => void;
  onEnterFusion?: () => void;
}

export function WorkerToolbar({
  mode,
  zoom,
  brushTool,
  brushSize,
  brushFlow,
  brushFeather,
  canUndo = false,
  canRedo = false,
  fusionUnlocked = false,
  onUndo,
  onRedo,
  onBrushToolChange,
  onBrushSizeChange,
  onBrushFlowChange,
  onBrushFeatherChange,
  onZoomChange,
  onFit,
  onEnterFusion,
}: WorkerToolbarProps) {
  const canEdit = mode === "create" || mode === "fusion";
  return (
    <div className="worker-toolbar" aria-label="工作区工具栏">
      <div className="toolbar-cluster toolbar-history" aria-label="历史操作">
        <button className="toolbar-button" type="button" aria-label="撤销" disabled={!canUndo || !canEdit} onClick={onUndo}>
          <Icon name="undo" />
        </button>
        <button className="toolbar-button" type="button" aria-label="重做" disabled={!canRedo || !canEdit} onClick={onRedo}>
          <Icon name="redo" />
        </button>
      </div>

      <span className="toolbar-divider" aria-hidden="true" />

      <div className="toolbar-cluster" aria-label="画笔工具">
        <button
          className="toolbar-button toolbar-tool"
          type="button"
          aria-label="手型工具"
          aria-pressed={brushTool === "hand"}
          onClick={() => onBrushToolChange("hand")}
        >
          <Icon name="hand" />
        </button>
        <button
          className="toolbar-button toolbar-tool"
          type="button"
          aria-label="画笔"
          aria-pressed={brushTool === "paint"}
          disabled={!canEdit}
          onClick={() => onBrushToolChange("paint")}
        >
          <Icon name="brush" />
        </button>
        <button
          className="toolbar-button toolbar-tool toolbar-erase"
          type="button"
          aria-label="擦除"
          aria-pressed={brushTool === "erase"}
          disabled={!canEdit}
          onClick={() => onBrushToolChange("erase")}
        >
          <span className="erase-glyph" aria-hidden="true" />
        </button>
      </div>

      <div className="toolbar-sliders" aria-label="画笔参数">
        <label className="toolbar-range">
          <span>SIZE <output>{brushSize}</output></span>
          <input type="range" min="4" max="512" step="4" value={brushSize} disabled={!canEdit || brushTool === "hand"} onChange={(event) => onBrushSizeChange(Number(event.target.value))} />
        </label>
        <label className="toolbar-range">
          <span>FLOW <output>{brushFlow}%</output></span>
          <input type="range" min="1" max="100" step="1" value={brushFlow} disabled={!canEdit || brushTool === "hand"} onChange={(event) => onBrushFlowChange(Number(event.target.value))} />
        </label>
        <label className="toolbar-range">
          <span>FEATHER <output>{brushFeather}%</output></span>
          <input type="range" min="0" max="100" step="1" value={brushFeather} disabled={!canEdit || brushTool === "hand"} onChange={(event) => onBrushFeatherChange(Number(event.target.value))} />
        </label>
      </div>

      <span className="toolbar-divider toolbar-divider--loose" aria-hidden="true" />

      <div className="toolbar-cluster toolbar-zoom" aria-label="画布缩放">
        <button className="toolbar-button toolbar-fit" type="button" aria-label="适合窗口" onClick={onFit}><Icon name="fit" /></button>
        <button className="toolbar-button toolbar-zoom-step" type="button" aria-label="缩小" onClick={() => onZoomChange(zoom - 10)} disabled={zoom <= 25}>−</button>
        <output className="zoom-output" aria-label="当前缩放">{zoom}%</output>
        <button className="toolbar-button toolbar-zoom-step" type="button" aria-label="放大" onClick={() => onZoomChange(zoom + 10)} disabled={zoom >= 400}>＋</button>
      </div>

      {fusionUnlocked && mode !== "fusion" && (
        <button className="toolbar-fusion-button" type="button" onClick={onEnterFusion}>
          <Icon name="spark" /> Fusion
        </button>
      )}
    </div>
  );
}
