import { useEffect, useMemo, useState } from "react";
import { Icon } from "../../components/Icon";
import { createFusion, uploadFusionMask } from "../../lib/api";
import type { FusionBox, FusionResult, PlacementIntent, SearchSnapshot } from "../../types";

interface FusionEditorProps {
  snapshot: SearchSnapshot;
  selectedCandidateId?: string | null;
  placement: PlacementIntent;
}

type FusionMode = "box" | "alpha";

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

export function FusionEditor({ snapshot, selectedCandidateId, placement }: FusionEditorProps) {
  const [mode, setMode] = useState<FusionMode>("box");
  const [box, setBox] = useState<FusionBox>(() => defaultBox(placement));
  const [feather, setFeather] = useState(8);
  const [maskFile, setMaskFile] = useState<File | null>(null);
  const [result, setResult] = useState<FusionResult | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const candidate = useMemo(
    () => snapshot.candidates.find((item) => item.candidate_id === selectedCandidateId)
      ?? snapshot.candidates.find((item) => item.candidate_id === snapshot.global_winner_id),
    [selectedCandidateId, snapshot.candidates, snapshot.global_winner_id],
  );
  const accepted = snapshot.status === "accepted";
  const boxError = mode === "box" ? validateBox(box) : null;

  useEffect(() => {
    setResult(null);
    setError(null);
    setMaskFile(null);
    setBox(defaultBox(placement));
  }, [snapshot.search_id, candidate?.candidate_id, placement.x, placement.y, placement.width, placement.height]);

  const updateBox = (key: keyof FusionBox, value: string) => {
    const next = Number(value);
    if (!Number.isFinite(next)) return;
    setResult(null);
    setError(null);
    setBox((current) => ({ ...current, [key]: next }));
  };

  const submit = async () => {
    if (!accepted || !candidate || boxError) return;
    setPending(true);
    setError(null);
    try {
      let maskAssetId: string | undefined;
      if (mode === "alpha") {
        if (!maskFile) throw new Error("请先选择 PNG alpha Fusion Mask");
        const registered = await uploadFusionMask(snapshot.search_id, maskFile);
        maskAssetId = registered.asset.asset_id;
      }
      const next = await createFusion(snapshot.search_id, {
        candidate_id: candidate.candidate_id,
        mask_asset_id: maskAssetId,
        box: mode === "box" ? box : undefined,
        feather_radius_px: feather,
      });
      setResult(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Fusion 生成失败");
    } finally {
      setPending(false);
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
        它不会改写 Raw，也不会改变 Critic 分数或下一轮搜索。
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
            <button type="button" role="tab" aria-selected={mode === "box"} onClick={() => { setMode("box"); setResult(null); setError(null); }}>矩形选区</button>
            <button type="button" role="tab" aria-selected={mode === "alpha"} onClick={() => { setMode("alpha"); setResult(null); setError(null); }}>PNG Alpha Mask</button>
          </div>
          {mode === "box" ? (
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
                    onChange={(event) => updateBox(key, event.target.value)}
                  />
                </label>
              ))}
            </div>
          ) : (
            <label className="fusion-file-input">
              <span>上传与原片同尺寸的 RGBA PNG；白色/alpha 区域会被融合。</span>
              <input type="file" accept="image/png" onChange={(event) => { setMaskFile(event.target.files?.[0] ?? null); setResult(null); setError(null); }} />
              {maskFile && <small>{maskFile.name}</small>}
            </label>
          )}
          <label className="fusion-feather">
            <span>边缘羽化 <strong>{feather}px</strong></span>
            <input type="range" min={0} max={64} step={1} value={feather} onChange={(event) => { setFeather(Number(event.target.value)); setResult(null); setError(null); }} />
          </label>
          <div className="fusion-actions">
            <button className="primary-button" type="button" disabled={pending || !candidate || Boolean(boxError) || (mode === "alpha" && !maskFile)} onClick={submit}>
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
              <img src={result.fusion_asset.asset_url ?? result.fusion_asset.content_url ?? result.fusion_asset.url} alt="用户 Fusion Mask 预览" />
              <small>Raw 仍保留为权威候选；Fusion Key {result.fusion_key.slice(0, 12)}…</small>
            </div>
          )}
        </>
      )}
    </section>
  );
}
