import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { Icon } from "../../components/Icon";
import { createFusion, uploadFusionMask } from "../../lib/api";
import type { MaskBrushSettings, MaskStrokeTool } from "../../lib/maskDocument";
import { rawCandidateUrl } from "../../lib/raw";
import type { FusionBox, FusionResult, PlacementIntent, SearchSnapshot } from "../../types";
import { MaskBrushEditor, type MaskBrushEditorHandle, type MaskHistoryState } from "../mask/MaskBrushEditor";
import { renderLocalFusion } from "./renderLocalFusion";

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
  onBrushHistoryChange?: (state: MaskHistoryState) => void;
  interactionDisabled?: boolean;
  onStateChange?: (state: FusionEditorState) => void;
  /** Keeps a completed Fusion visible after Timeline navigation remounts the editor. */
  restoredResult?: FusionResult | null;
  /** Development-only local compositor used by ?demo=fusion. */
  demoMode?: boolean;
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
  demoMode = false, restoredResult = null,
}, ref) {
  const hasBrush = Boolean(backgroundSrc && backgroundWidth && backgroundHeight);
  const [mode, setMode] = useState<FusionMode>(hasBrush ? "brush" : "box");
  const [box, setBox] = useState<FusionBox>(() => defaultBox(placement));
  const [feather, setFeather] = useState(8);
  const [maskFile, setMaskFile] = useState<File | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<FusionResult | null>(restoredResult);
  const brushRef = useRef<MaskBrushEditorHandle | null>(null);
  const epochRef = useRef(0);
  const demoResultUrlRef = useRef<string | null>(null);
  const candidate = useMemo(() => snapshot.candidates.find((item) => item.candidate_id === selectedCandidateId)
    ?? snapshot.candidates.find((item) => item.candidate_id === snapshot.global_winner_id)
    ?? snapshot.candidates.find((item) => item.is_global_winner), [selectedCandidateId, snapshot.candidates, snapshot.global_winner_id]);
  const accepted = snapshot.status === "accepted";
  const effectiveMode = hasBrush ? mode : mode === "brush" ? "box" : mode;
  const boxError = effectiveMode === "box" ? validateBox(box) : null;
  const ready = accepted && Boolean(candidate) && !boxError && (effectiveMode !== "alpha" || Boolean(maskFile));

  const revokeDemoResult = useCallback(() => {
    if (!demoResultUrlRef.current) return;
    URL.revokeObjectURL(demoResultUrlRef.current);
    demoResultUrlRef.current = null;
  }, []);
  const bindBrushHandle = useCallback((handle: MaskBrushEditorHandle | null) => {
    brushRef.current = handle;
    onBrushHandleChange?.(handle);
  }, [onBrushHandleChange]);
  const clearCompletedResult = useCallback(() => {
    revokeDemoResult();
    setResult(null);
    setError(null);
  }, [revokeDemoResult]);

  useEffect(() => {
    epochRef.current += 1;
    revokeDemoResult();
    setPending(false); setError(null);
    setResult((current) => current?.search_id === snapshot.search_id && current.candidate_id === candidate?.candidate_id
      ? current
      : null);
    setMaskFile(null); setBox(defaultBox(placement));
  }, [candidate?.candidate_id, placement.height, placement.width, placement.x, placement.y, revokeDemoResult, snapshot.search_id]);
  useEffect(() => {
    if (
      restoredResult
      && restoredResult.search_id === snapshot.search_id
      && restoredResult.candidate_id === candidate?.candidate_id
    ) setResult(restoredResult);
  }, [candidate?.candidate_id, restoredResult, snapshot.search_id]);
  useEffect(() => onStateChange?.({ pending, error, result, ready }), [error, onStateChange, pending, ready, result]);

  const apply = useCallback(async () => {
    if (!ready || pending || !candidate) return;
    const epoch = ++epochRef.current;
    setPending(true); setError(null);
    try {
      let maskAssetId: string | undefined;
      if (effectiveMode === "brush") {
        if (!brushRef.current?.getDocument().strokes.length) throw new Error("请先在图片上画出需要融合的区域");
        const file = await brushRef.current.exportMaskFile(`${candidate.candidate_id}-fusion-mask.png`);
        if (epochRef.current !== epoch) return;
        if (demoMode) {
          const generatedSrc = rawCandidateUrl(candidate);
          if (!backgroundSrc || !generatedSrc || !backgroundWidth || !backgroundHeight) {
            throw new Error("本地预览缺少原片、候选图或图片尺寸");
          }
          const blob = await renderLocalFusion({
            originalSrc: backgroundSrc,
            generatedSrc,
            mask: file,
            width: backgroundWidth,
            height: backgroundHeight,
          });
          if (epochRef.current !== epoch) return;
          const fusionUrl = URL.createObjectURL(blob);
          if (epochRef.current !== epoch) {
            URL.revokeObjectURL(fusionUrl);
            return;
          }
          revokeDemoResult();
          demoResultUrlRef.current = fusionUrl;
          setResult({
            fusion_key: `fusion-demo:${candidate.candidate_id}`,
            search_id: snapshot.search_id,
            candidate_id: candidate.candidate_id,
            source_manifest_hash: "fusion-demo-local",
            raw_asset: { asset_id: "fusion-demo-raw", asset_url: generatedSrc },
            fusion_asset: { asset_id: "fusion-demo-result", asset_url: fusionUrl },
            mask_asset: { asset_id: "fusion-demo-local-mask" },
            feather_radius_px: 0,
          });
          return;
        }
        maskAssetId = (await uploadFusionMask(snapshot.search_id, file)).asset.asset_id;
      } else if (effectiveMode === "alpha") {
        if (!maskFile) throw new Error("请先选择带透明通道的 PNG 蒙版");
        maskAssetId = (await uploadFusionMask(snapshot.search_id, maskFile)).asset.asset_id;
      }
      if (epochRef.current !== epoch) return;
      const next = await createFusion(snapshot.search_id, { candidate_id: candidate.candidate_id, mask_asset_id: maskAssetId, box: effectiveMode === "box" ? box : undefined, feather_radius_px: effectiveMode === "brush" ? 0 : feather });
      if (epochRef.current === epoch) setResult(next);
    } catch (cause) {
      if (epochRef.current === epoch) setError(cause instanceof Error ? cause.message : "融合预览生成失败");
    } finally {
      if (epochRef.current === epoch) setPending(false);
    }
  }, [backgroundHeight, backgroundSrc, backgroundWidth, box, candidate, demoMode, effectiveMode, feather, maskFile, pending, ready, revokeDemoResult, snapshot.search_id]);
  useImperativeHandle(ref, () => ({ apply }), [apply]);

  const canvas = result
    ? <img className="worker-image" src={assetUrl(result)} alt="局部融合预览" />
    : effectiveMode === "brush" && hasBrush && candidate
      ? <div className="fusion-editor-surface"><MaskBrushEditor key={`${snapshot.search_id}:${candidate.candidate_id}`} ref={bindBrushHandle} originalSrc={backgroundSrc} generatedSrc={rawCandidateUrl(candidate)} generatedCropMapping={candidate.crop_mapping} width={backgroundWidth ?? 1} height={backgroundHeight ?? 1} disabled={pending || interactionDisabled} controlledTool={controlledTool} controlledBrush={controlledBrush} showChrome={showChrome} onHistoryChange={onBrushHistoryChange} onUserEdit={clearCompletedResult} /></div>
      : null;

  if (!showChrome) {
    if (!accepted || !candidate || !hasBrush) return <div className="canvas-image-empty" role="status"><Icon name="lock" /><span>接受一张候选图后可编辑融合范围</span></div>;
    return canvas;
  }
  if (!accepted) return <div className="fusion-disabled"><Icon name="lock" /> 接受一张候选图后再选择融合范围。</div>;
  return <section className="panel fusion-editor">
    <div className="fusion-candidate-line"><span>当前候选</span><strong>{candidate?.candidate_id ?? "未选择"}</strong></div>
    <div className="fusion-mode-tabs" role="tablist" aria-label="融合范围类型">
      {hasBrush && <button type="button" role="tab" aria-selected={effectiveMode === "brush"} onClick={() => setMode("brush")}>画笔实时融合</button>}
      <button type="button" role="tab" aria-selected={effectiveMode === "box"} onClick={() => setMode("box")}>矩形选区</button>
      <button type="button" role="tab" aria-selected={effectiveMode === "alpha"} onClick={() => setMode("alpha")}>PNG 蒙版</button>
    </div>
    {canvas}
    {effectiveMode === "box" && <div className="fusion-box-grid">{(["x", "y", "width", "height"] as const).map((key) => <label key={key}><span>{key.toUpperCase()}</span><input aria-label={key.toUpperCase()} type="number" min="0" max="1" step="0.01" value={box[key]} onChange={(event) => setBox((current) => ({ ...current, [key]: Number(event.target.value) }))} /></label>)}</div>}
    {effectiveMode === "alpha" && <label className="fusion-file-input"><span>上传 PNG 蒙版</span><input type="file" accept="image/png" onChange={(event) => setMaskFile(event.currentTarget.files?.[0] ?? null)} /></label>}
    {effectiveMode !== "brush" && <label className="fusion-feather"><span>边缘羽化 <strong>{feather}px</strong></span><input type="range" min="0" max="64" value={feather} onChange={(event) => setFeather(Number(event.target.value))} /></label>}
    <button className="primary-button" type="button" disabled={!ready || pending} onClick={() => void apply()}>{pending ? "融合中…" : "生成融合预览"}</button>
    {(boxError || error) && <p className="fusion-error" role="alert">{boxError ?? error}</p>}
  </section>;
});
