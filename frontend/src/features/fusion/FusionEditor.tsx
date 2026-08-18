import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { Icon } from "../../components/Icon";
import { createFusion, uploadFusionMask } from "../../lib/api";
import type { MaskBrushSettings, MaskStrokeTool } from "../../lib/maskDocument";
import { rawCandidateUrl } from "../../lib/raw";
import type { FusionBox, FusionResult, PlacementIntent, SearchSnapshot } from "../../types";
import { MaskBrushEditor, type MaskBrushEditorHandle } from "../mask/MaskBrushEditor";

export interface FusionEditorState { pending: boolean; error: string | null; result: FusionResult | null; ready: boolean; }
export interface FusionEditorHandle { apply: () => Promise<void>; }
type FusionMode = "brush" | "box" | "alpha";

interface FusionEditorProps {
  snapshot: SearchSnapshot;
  selectedCandidateId?: string | null;
  placement?: PlacementIntent;
  backgroundSrc?: string | null;
  backgroundWidth?: number;
  backgroundHeight?: number;
  controlledTool?: MaskStrokeTool;
  controlledBrush?: Partial<MaskBrushSettings>;
  onBrushHandleChange?: (handle: MaskBrushEditorHandle | null) => void;
  onBrushHistoryChange?: (state: { canUndo: boolean; canRedo: boolean }) => void;
  interactionDisabled?: boolean;
  onStateChange?: (state: FusionEditorState) => void;
  /** False in the main worker; legacy/embedded use can keep controls outside that shell. */
  showChrome?: boolean;
}

const fallbackPlacement: PlacementIntent = { x: .6, y: .5, width: .2, height: .3, coordinate_space: "normalized", pose: "sitting", facing: "camera", contact_surface: null };
function defaultBox(placement: PlacementIntent): FusionBox { return { x: placement.x, y: placement.y, width: placement.width, height: placement.height }; }
function validateBox(box: FusionBox): string | null {
  if (![box.x, box.y, box.width, box.height].every(Number.isFinite)) return "矩形选区必须填写有效数字";
  if (box.x < 0 || box.y < 0 || box.width <= 0 || box.height <= 0) return "X/Y 不能小于 0，宽高必须大于 0";
  if (box.x + box.width > 1 || box.y + box.height > 1) return "矩形选区超出原片边界，请减小宽高或调整 X/Y";
  return null;
}
function assetUrl(result: FusionResult): string { return result.fusion_asset.asset_url ?? result.fusion_asset.content_url ?? result.fusion_asset.url ?? ""; }

export const FusionEditor = forwardRef<FusionEditorHandle, FusionEditorProps>(function FusionEditor({
  snapshot, selectedCandidateId, placement = fallbackPlacement, backgroundSrc, backgroundWidth, backgroundHeight,
  controlledTool, controlledBrush, onBrushHandleChange, onBrushHistoryChange, interactionDisabled = false, onStateChange, showChrome = true,
}, ref) {
  const hasBrush = Boolean(backgroundSrc && backgroundWidth && backgroundHeight);
  const [mode, setMode] = useState<FusionMode>(hasBrush ? "brush" : "box");
  const [box, setBox] = useState<FusionBox>(() => defaultBox(placement));
  const [feather, setFeather] = useState(8);
  const [maskFile, setMaskFile] = useState<File | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<FusionResult | null>(null);
  const brushRef = useRef<MaskBrushEditorHandle | null>(null);
  const epochRef = useRef(0);
  const candidate = useMemo(() => snapshot.candidates.find((item) => item.candidate_id === selectedCandidateId)
    ?? snapshot.candidates.find((item) => item.candidate_id === snapshot.global_winner_id)
    ?? snapshot.candidates.find((item) => item.is_global_winner), [selectedCandidateId, snapshot.candidates, snapshot.global_winner_id]);
  const accepted = snapshot.status === "accepted";
  const effectiveMode = hasBrush ? mode : mode === "brush" ? "box" : mode;
  const boxError = effectiveMode === "box" ? validateBox(box) : null;
  const ready = accepted && Boolean(candidate) && !boxError && (effectiveMode !== "alpha" || Boolean(maskFile));

  useEffect(() => {
    epochRef.current += 1;
    setPending(false); setError(null); setResult(null); setMaskFile(null); setBox(defaultBox(placement));
  }, [candidate?.candidate_id, placement.height, placement.width, placement.x, placement.y, snapshot.search_id]);
  useEffect(() => onStateChange?.({ pending, error, result, ready }), [error, onStateChange, pending, ready, result]);

  const apply = useCallback(async () => {
    if (!ready || pending || !candidate) return;
    const epoch = ++epochRef.current;
    setPending(true); setError(null);
    try {
      let maskAssetId: string | undefined;
      if (effectiveMode === "brush") {
        if (!brushRef.current?.getDocument().strokes.length) throw new Error("请先在图片上绘制 Fusion 区域");
        const file = await brushRef.current.exportMaskFile(`${candidate.candidate_id}-fusion-mask.png`);
        if (epochRef.current !== epoch) return;
        maskAssetId = (await uploadFusionMask(snapshot.search_id, file)).asset.asset_id;
      } else if (effectiveMode === "alpha") {
        if (!maskFile) throw new Error("请先选择 PNG alpha Fusion Mask");
        maskAssetId = (await uploadFusionMask(snapshot.search_id, maskFile)).asset.asset_id;
      }
      if (epochRef.current !== epoch) return;
      const next = await createFusion(snapshot.search_id, { candidate_id: candidate.candidate_id, mask_asset_id: maskAssetId, box: effectiveMode === "box" ? box : undefined, feather_radius_px: effectiveMode === "brush" ? 0 : feather });
      if (epochRef.current === epoch) setResult(next);
    } catch (cause) {
      if (epochRef.current === epoch) setError(cause instanceof Error ? cause.message : "Fusion 生成失败");
    } finally {
      if (epochRef.current === epoch) setPending(false);
    }
  }, [box, candidate, effectiveMode, feather, maskFile, pending, ready, snapshot.search_id]);
  useImperativeHandle(ref, () => ({ apply }), [apply]);

  const canvas = result
    ? <img className="worker-image" src={assetUrl(result)} alt="用户 Fusion Mask 预览" />
    : effectiveMode === "brush" && hasBrush && candidate
      ? <div className="fusion-editor-surface"><MaskBrushEditor key={`${snapshot.search_id}:${candidate.candidate_id}`} ref={(handle) => { brushRef.current = handle; onBrushHandleChange?.(handle); }} originalSrc={backgroundSrc} generatedSrc={rawCandidateUrl(candidate)} generatedCropMapping={candidate.crop_mapping} width={backgroundWidth ?? 1} height={backgroundHeight ?? 1} disabled={pending || interactionDisabled} controlledTool={controlledTool} controlledBrush={controlledBrush} showChrome={showChrome} onHistoryChange={onBrushHistoryChange} onDocumentChange={() => { setResult(null); setError(null); }} /></div>
      : null;

  if (!showChrome) {
    if (!accepted || !candidate || !hasBrush) return <div className="canvas-image-empty" role="status"><Icon name="lock" /><span>接受一个 Raw candidate 后可编辑 Fusion</span></div>;
    return canvas;
  }
  if (!accepted) return <div className="fusion-disabled"><Icon name="lock" /> 接受一个 Raw candidate 后再选择融合范围。</div>;
  return <section className="panel fusion-editor">
    <div className="fusion-candidate-line"><span>当前 Raw</span><strong>{candidate?.candidate_id ?? "未选择"}</strong></div>
    <div className="fusion-mode-tabs" role="tablist" aria-label="Fusion Mask 类型">
      {hasBrush && <button type="button" role="tab" aria-selected={effectiveMode === "brush"} onClick={() => setMode("brush")}>画笔实时融合</button>}
      <button type="button" role="tab" aria-selected={effectiveMode === "box"} onClick={() => setMode("box")}>矩形选区</button>
      <button type="button" role="tab" aria-selected={effectiveMode === "alpha"} onClick={() => setMode("alpha")}>PNG Alpha Mask</button>
    </div>
    {canvas}
    {effectiveMode === "box" && <div className="fusion-box-grid">{(["x", "y", "width", "height"] as const).map((key) => <label key={key}><span>{key.toUpperCase()}</span><input aria-label={key.toUpperCase()} type="number" min="0" max="1" step="0.01" value={box[key]} onChange={(event) => setBox((current) => ({ ...current, [key]: Number(event.target.value) }))} /></label>)}</div>}
    {effectiveMode === "alpha" && <label className="fusion-file-input"><span>上传 PNG Alpha Mask</span><input type="file" accept="image/png" onChange={(event) => setMaskFile(event.currentTarget.files?.[0] ?? null)} /></label>}
    {effectiveMode !== "brush" && <label className="fusion-feather"><span>边缘羽化 <strong>{feather}px</strong></span><input type="range" min="0" max="64" value={feather} onChange={(event) => setFeather(Number(event.target.value))} /></label>}
    <button className="primary-button" type="button" disabled={!ready || pending} onClick={() => void apply()}>{pending ? "融合中…" : "生成 Fusion 预览"}</button>
    {(boxError || error) && <p className="fusion-error" role="alert">{boxError ?? error}</p>}
  </section>;
});
