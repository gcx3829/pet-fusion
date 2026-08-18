import { type DragEvent, type RefObject } from "react";
import { Icon } from "../../components/Icon";
import { FusionEditor, type FusionEditorHandle, type FusionEditorState } from "../fusion/FusionEditor";
import {
  GuidanceMaskEditor,
  type GuidanceMaskEditorHandle,
  type GuidanceMaskEditorState,
} from "../placement/GuidanceMaskEditor";
import { RawCandidateViewer } from "../candidates/RawCandidateViewer";
import type { MaskBrushEditorHandle, MaskHistoryState } from "../mask/MaskBrushEditor";
import type { FusionResult, PlacementIntent, SearchSnapshot } from "../../types";
import type { MaskBrushSettings } from "../../lib/maskDocument";
import type { BrushTool, WorkerMode } from "./useWorkbenchUi";
import { EditorGestureViewport } from "./EditorGestureViewport";

interface WorkerViewportProps {
  mode: WorkerMode;
  backgroundUrl: string | null;
  backgroundWidth?: number;
  backgroundHeight?: number;
  placement: PlacementIntent;
  guidanceEditorRef: RefObject<GuidanceMaskEditorHandle | null>;
  guidanceState: GuidanceMaskEditorState | null;
  onGuidanceStateChange: (state: GuidanceMaskEditorState) => void;
  guidanceDisabled?: boolean;
  snapshot?: SearchSnapshot;
  selectedCandidateId?: string | null;
  expectedCandidateCount?: number;
  guidanceLocked?: boolean;
  onBackgroundDrop?: (event: DragEvent<HTMLDivElement>) => void;
  brushTool?: BrushTool;
  brushSettings?: Partial<MaskBrushSettings>;
  zoom?: number;
  onZoomChange?: (zoom: number) => void;
  onFusionBrushHandleChange?: (handle: MaskBrushEditorHandle | null) => void;
  onFusionBrushHistoryChange?: (state: MaskHistoryState) => void;
  fusionEditorRef?: RefObject<FusionEditorHandle | null>;
  onFusionStateChange?: (state: FusionEditorState) => void;
  fusionDemoMode?: boolean;
  restoredFusionResult?: FusionResult | null;
}

export function WorkerViewport({
  mode,
  backgroundUrl,
  backgroundWidth,
  backgroundHeight,
  placement,
  guidanceEditorRef,
  guidanceState: _guidanceState,
  onGuidanceStateChange,
  guidanceDisabled = false,
  snapshot,
  selectedCandidateId,
  expectedCandidateCount,
  guidanceLocked = false,
  onBackgroundDrop,
  brushTool,
  brushSettings,
  zoom = 100,
  onZoomChange,
  onFusionBrushHandleChange,
  onFusionBrushHistoryChange,
  fusionEditorRef,
  onFusionStateChange,
  fusionDemoMode = false,
  restoredFusionResult = null,
}: WorkerViewportProps) {
  if (mode === "create") {
    return backgroundUrl ? (
      <div className="worker-drop-surface" onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }} onDrop={onBackgroundDrop} data-testid="worker-drop-surface">
      <EditorGestureViewport zoom={zoom} panEnabled={brushTool === "hand"} onZoomChange={onZoomChange}>
        <div className="worker-viewport worker-viewport--create">
          <GuidanceMaskEditor
            ref={guidanceEditorRef}
            backgroundSrc={backgroundUrl}
            width={backgroundWidth}
            height={backgroundHeight}
            placement={placement}
            disabled={guidanceDisabled || brushTool === "hand"}
            locked={guidanceLocked}
            controlledTool={brushTool === "hand" ? undefined : brushTool}
            controlledBrush={brushSettings}
            onStateChange={onGuidanceStateChange}
          />
        </div>
      </EditorGestureViewport>
      </div>
    ) : (
      <div className="worker-viewport worker-empty" role="status" onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }} onDrop={onBackgroundDrop} data-testid="worker-drop-surface">
        <span className="worker-empty-mark"><Icon name="image" /></span>
        <strong>把素材拖到这里作为底片</strong>
        <p>也可以在左侧素材项选择“底片”。</p>
      </div>
    );
  }

  if (!snapshot) {
    return (
      <div className="worker-viewport worker-empty" role="status">
        <span className="worker-empty-mark"><Icon name={mode === "fusion" ? "lock" : "aperture"} /></span>
        <strong>{mode === "fusion" ? "Fusion 需要已接受的 Raw" : "候选正在进入暗房"}</strong>
        <p>{mode === "fusion" ? "完成人工接受后，显式进入 Fusion 才能编辑融合范围。" : "生成状态和 Raw 候选会在这里稳定显示。"}</p>
      </div>
    );
  }

  if (mode === "generation") {
    return (
      <EditorGestureViewport zoom={zoom} panEnabled onZoomChange={onZoomChange}>
        <div className="worker-viewport worker-viewport--generation">
          <RawCandidateViewer
            snapshot={snapshot}
            selectedCandidateId={selectedCandidateId}
            expectedCount={expectedCandidateCount}
            sourceWidth={backgroundWidth}
            sourceHeight={backgroundHeight}
          />
        </div>
      </EditorGestureViewport>
    );
  }

  return (
    <EditorGestureViewport zoom={zoom} panEnabled={brushTool === "hand"} onZoomChange={onZoomChange}>
      <div className="worker-viewport worker-viewport--fusion">
        <FusionEditor
          ref={fusionEditorRef}
          snapshot={snapshot}
          selectedCandidateId={selectedCandidateId}
          backgroundSrc={backgroundUrl}
          backgroundWidth={backgroundWidth}
          backgroundHeight={backgroundHeight}
          controlledTool={brushTool === "hand" ? undefined : brushTool}
          interactionDisabled={brushTool === "hand"}
          controlledBrush={brushSettings}
          demoMode={fusionDemoMode}
          restoredResult={restoredFusionResult}
          showChrome={false}
          onBrushHandleChange={onFusionBrushHandleChange}
          onBrushHistoryChange={onFusionBrushHistoryChange}
          onStateChange={onFusionStateChange}
        />
      </div>
    </EditorGestureViewport>
  );
}
