import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "../../components/Icon";
import {
  MaskBrushEditor,
  type MaskBrushEditorHandle,
} from "../mask/MaskBrushEditor";
import { createFusion, uploadFusionMask } from "../../lib/api";
import type {
  FusionBox,
  FusionResult,
  PlacementIntent,
  SearchCandidate,
  SearchSnapshot,
} from "../../types";

interface FusionEditorProps {
  snapshot: SearchSnapshot;
  selectedCandidateId?: string | null;
  placement: PlacementIntent;
  /** The immutable background asset URL used by the local two-layer preview. */
  backgroundSrc?: string | null;
  backgroundWidth?: number;
  backgroundHeight?: number;
}

type FusionMode = "brush" | "box" | "alpha";

function bounded(value: number, fallback: number): number {
  return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : fallback;
}

function defaultBox(placement: PlacementIntent): FusionBox {
  return {
    x: bounded(placement.x, 0.5),
    y: bounded(placement.y, 0.4),
    width: Math.min(1 - bounded(placement.x, 0.5), Math.max(0.01, placement.width)),
    height: Math.min(1 - bounded(placement.y, 0.4), Math.max(0.01, placement.height)),
  };
}

function validateBox(box: FusionBox): string | null {
  if (![box.x, box.y, box.width, box.height].every(Number.isFinite)) {
    return "矩形选区必须填写有效数字";
  }
  if (box.x < 0 || box.y < 0 || box.width <= 0 || box.height <= 0) {
    return "X/Y 不能小于 0，宽高必须大于 0";
  }
  if (box.x + box.width > 1 || box.y + box.height > 1) {
    return "矩形选区超出原片边界，请减小宽高或调整 X/Y";
  }
  return null;
}

function isPositiveInteger(value: number | undefined): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function candidateRawUrl(candidate: SearchCandidate | undefined): string | null {
  if (!candidate) return null;
  return candidate.raw_image_url ?? candidate.raw_asset_url ?? candidate.image_url ?? null;
}

function assetUrl(result: FusionResult): string {
  return result.fusion_asset.asset_url
    ?? result.fusion_asset.content_url
    ?? result.fusion_asset.url
    ?? "";
}

export function FusionEditor({
  snapshot,
  selectedCandidateId,
  placement,
  backgroundSrc,
  backgroundWidth,
  backgroundHeight,
}: FusionEditorProps) {
  const [mode, setMode] = useState<FusionMode>("brush");
  const [box, setBox] = useState<FusionBox>(() => defaultBox(placement));
  const [feather, setFeather] = useState(8);
  const [maskFile, setMaskFile] = useState<File | null>(null);
  const [result, setResult] = useState<FusionResult | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const brushRef = useRef<MaskBrushEditorHandle | null>(null);
  const submissionEpochRef = useRef(0);
  const submissionPendingRef = useRef(false);

  const candidate = useMemo(
    () => snapshot.candidates.find((item) => item.candidate_id === selectedCandidateId)
      ?? snapshot.candidates.find((item) => item.candidate_id === snapshot.global_winner_id),
    [selectedCandidateId, snapshot.candidates, snapshot.global_winner_id],
  );
  const accepted = snapshot.status === "accepted";
  const hasBrushSource = Boolean(
    backgroundSrc
    && isPositiveInteger(backgroundWidth)
    && isPositiveInteger(backgroundHeight),
  );
  // Keep the brush as the production default. If an older embedding does not
  // pass a background asset, the legacy box mode remains usable instead of
  // producing a stretched or fabricated local preview.
  const effectiveMode: FusionMode = hasBrushSource
    ? mode
    : mode === "brush" ? "box" : mode;
  const boxError = effectiveMode === "box" ? validateBox(box) : null;

  useEffect(() => {
    // A provider-independent upload/create operation cannot be cancelled once
    // dispatched. Invalidate its UI epoch so a late response from the previous
    // candidate can never appear under the newly selected candidate.
    submissionEpochRef.current += 1;
    submissionPendingRef.current = false;
    setPending(false);
    setResult(null);
    setError(null);
    setMaskFile(null);
    setBox(defaultBox(placement));
    return () => {
      // Also fence a late promise after the whole editor unmounts.
      submissionEpochRef.current += 1;
      submissionPendingRef.current = false;
    };
  }, [
    snapshot.search_id,
    candidate?.candidate_id,
    placement.x,
    placement.y,
    placement.width,
    placement.height,
  ]);

  const handleBrushDocumentChange = useCallback(() => {
    // A stroke is intentionally a local-only operation. This callback only
    // invalidates the previously saved preview; it never calls the API.
    setResult(null);
    setError(null);
  }, []);

  const updateBox = (key: keyof FusionBox, value: string) => {
    const next = Number(value);
    if (!Number.isFinite(next)) return;
    setResult(null);
    setError(null);
    setBox((current) => ({ ...current, [key]: next }));
  };

  const submit = async () => {
    if (!accepted || !candidate || boxError || submissionPendingRef.current) return;
    const submissionEpoch = submissionEpochRef.current + 1;
    submissionEpochRef.current = submissionEpoch;
    submissionPendingRef.current = true;
    setPending(true);
    setError(null);
    try {
      let maskAssetId: string | undefined;
      if (effectiveMode === "brush") {
        const editor = brushRef.current;
        if (!editor) throw new Error("画笔编辑器尚未准备好");
        if (!editor.getDocument().strokes.length) {
          throw new Error("请先在画布上绘制 Fusion 区域");
        }
        // This is the first network boundary: all pointer strokes and
        // previews before this point stay in browser memory.
        const file = await editor.exportMaskFile(`${candidate.candidate_id}-fusion-mask.png`);
        if (submissionEpochRef.current !== submissionEpoch) return;
        const registered = await uploadFusionMask(snapshot.search_id, file);
        maskAssetId = registered.asset.asset_id;
      } else if (effectiveMode === "alpha") {
        if (!maskFile) throw new Error("请先选择 PNG alpha Fusion Mask");
        const registered = await uploadFusionMask(snapshot.search_id, maskFile);
        maskAssetId = registered.asset.asset_id;
      }
      if (submissionEpochRef.current !== submissionEpoch) return;
      const next = await createFusion(snapshot.search_id, {
        candidate_id: candidate.candidate_id,
        mask_asset_id: maskAssetId,
        box: effectiveMode === "box" ? box : undefined,
        // Brush feather is already baked into the local alpha raster. Sending
        // zero prevents the server from applying a second blur pass.
        feather_radius_px: effectiveMode === "brush" ? 0 : feather,
      });
      if (submissionEpochRef.current === submissionEpoch) {
        setResult(next);
      }
    } catch (cause) {
      if (submissionEpochRef.current === submissionEpoch) {
        setError(cause instanceof Error ? cause.message : "Fusion 生成失败");
      }
    } finally {
      if (submissionEpochRef.current === submissionEpoch) {
        submissionPendingRef.current = false;
        setPending(false);
      }
    }
  };

  return (
    <section className="panel fusion-editor" aria-labelledby="fusion-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">05 / OPTIONAL FUSION</p>
          <h2 id="fusion-heading">用户融合遮罩</h2>
        </div>
        <span className={`fusion-status ${accepted ? "is-ready" : ""}`}>
          {accepted ? "RAW 已接受" : "接受候选后可用"}
        </span>
      </div>
      <p className="fusion-explainer">
        Guidance Mask 只给图像模型定义编辑窗口；Fusion Mask 是最终预览/导出时由你决定的融合区域。
        画笔预览完全在本地运行，只有点击生成 Fusion 预览时才上传 alpha PNG；它不会改写 Raw、Critic 分数或下一轮搜索。
      </p>
      {!accepted ? (
        <div className="fusion-disabled"><Icon name="lock" /> 接受一个 Raw candidate 后再选择融合范围。</div>
      ) : (
        <>
          <div className="fusion-candidate-line">
            <span>当前 Raw</span>
            <strong>{candidate?.candidate_id ?? "未选择"}</strong>
            {candidate?.score !== undefined && <small>Critic {candidate.score.toFixed(1)}（仅 Raw）</small>}
          </div>
          <div className="fusion-mode-tabs" role="tablist" aria-label="Fusion Mask 类型">
            {hasBrushSource && (
              <button
                type="button"
                role="tab"
                aria-selected={effectiveMode === "brush"}
                disabled={pending}
                onClick={() => { setMode("brush"); setResult(null); setError(null); }}
              >
                画笔实时融合
              </button>
            )}
            <button type="button" role="tab" aria-selected={effectiveMode === "box"} disabled={pending} onClick={() => { setMode("box"); setResult(null); setError(null); }}>矩形选区</button>
            <button type="button" role="tab" aria-selected={effectiveMode === "alpha"} disabled={pending} onClick={() => { setMode("alpha"); setResult(null); setError(null); }}>PNG Alpha Mask</button>
          </div>
          {effectiveMode === "brush" && hasBrushSource ? (
            <div className="fusion-brush-wrap">
              <p className="fusion-local-note">
                生成 Raw 在底层，原图在上层。绘制 alpha 后显示底层生成图：0 = 原图，255 = 生成图。
                画笔尺寸、流量、羽化、撤销与重做都不会发起网络请求。
              </p>
              <MaskBrushEditor
                key={`${snapshot.search_id}:${candidate?.candidate_id ?? "none"}`}
                ref={brushRef}
                originalSrc={backgroundSrc}
                generatedSrc={candidateRawUrl(candidate)}
                generatedCropMapping={candidate?.crop_mapping}
                width={backgroundWidth ?? 1}
                height={backgroundHeight ?? 1}
                disabled={pending}
                onDocumentChange={handleBrushDocumentChange}
              />
            </div>
          ) : effectiveMode === "box" ? (
            <div className="fusion-box-grid">
              {(["x", "y", "width", "height"] as const).map((key) => (
                <label key={key}>
                  <span>{key.toUpperCase()}</span>
                  <input
                    type="number"
                    min={key === "width" || key === "height" ? 0.01 : 0}
                    max={1}
                    step={0.01}
                    value={box[key]}
                    aria-invalid={Boolean(boxError)}
                    disabled={pending}
                    onChange={(event) => updateBox(key, event.target.value)}
                  />
                </label>
              ))}
            </div>
          ) : (
            <label className="fusion-file-input">
              <span>上传与原片同尺寸的 RGBA PNG；白色/alpha 区域会被融合。</span>
              <input type="file" accept="image/png" disabled={pending} onChange={(event) => { setMaskFile(event.target.files?.[0] ?? null); setResult(null); setError(null); }} />
              {maskFile && <small>{maskFile.name}</small>}
            </label>
          )}
          {effectiveMode !== "brush" && (
            <label className="fusion-feather">
              <span>边缘羽化 <strong>{feather}px</strong></span>
              <input type="range" min={0} max={64} step={1} value={feather} disabled={pending} onChange={(event) => { setFeather(Number(event.target.value)); setResult(null); setError(null); }} />
            </label>
          )}
          <div className="fusion-actions">
            <button className="primary-button" type="button" disabled={pending || !candidate || Boolean(boxError) || (effectiveMode === "alpha" && !maskFile)} onClick={submit}>
              <Icon name="spark" /> {pending ? "融合中…" : "生成 Fusion 预览"}
            </button>
            {(boxError || error) && <p className="fusion-error" role="alert">{boxError ?? error}</p>}
          </div>
          {result && (
            <div className="fusion-result" aria-live="polite">
              <div className="fusion-result-heading">
                <strong>Fusion 预览</strong>
                <span>不继承 Raw 的 Critic 分数</span>
              </div>
              <img src={assetUrl(result)} alt="用户 Fusion Mask 预览" />
              <small>Raw 仍保留为权威候选；Fusion Key {result.fusion_key.slice(0, 12)}…</small>
            </div>
          )}
        </>
      )}
    </section>
  );
}
