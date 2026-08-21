import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { Icon } from "../../components/Icon";
import { rawCandidateUrl } from "../../lib/raw";
import type { SearchSnapshot } from "../../types";

interface RawCandidateViewerProps {
  snapshot: SearchSnapshot;
  selectedCandidateId?: string | null;
  expectedCount?: number;
  sourceWidth?: number;
  sourceHeight?: number;
}

/** The worker is deliberately image-only. Navigation, scores and actions live
 * in the timeline/review sidebar so the canvas never becomes another panel. */
export function RawCandidateViewer({ snapshot, selectedCandidateId, expectedCount = 1, sourceWidth, sourceHeight }: RawCandidateViewerProps) {
  const [failed, setFailed] = useState(false);
  const selected = useMemo(
    () => snapshot.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId)
      ?? snapshot.candidates.find((candidate) => candidate.is_global_winner)
      ?? snapshot.candidates[0],
    [selectedCandidateId, snapshot.candidates],
  );
  const src = rawCandidateUrl(selected);
  useEffect(() => setFailed(false), [selected?.candidate_id, src]);

  if (failed) {
    return <div className="canvas-image-empty" role="status"><Icon name="aperture" /><span>图片暂不可用</span></div>;
  }
  if (!selected || !src) {
    const count = Math.max(1, Math.min(4, expectedCount));
    const ratio = sourceWidth && sourceHeight ? sourceWidth / sourceHeight : 4 / 3;
    return <div className="worker-generation-placeholder" role="status" style={{ "--source-ratio": ratio } as CSSProperties}>
      <div className="generation-placeholder-stage">
        <span className="generation-placeholder-mark"><Icon name="aperture" /></span>
        <strong>正在生成第 {snapshot.round_index + 1} 轮</strong>
        <small>生成完成后会自动出现在时间线</small>
        <i aria-hidden="true" />
      </div>
      <div className="generation-placeholder-slots" aria-label={`${count} 个候选生成槽位`}>
        {Array.from({ length: count }, (_, index) => <span key={index} aria-label={`候选 ${index + 1} 正在生成`}><b>{String(index + 1).padStart(2, "0")}</b><i /></span>)}
      </div>
    </div>;
  }
  return <img className="worker-image" src={src} alt={`候选图 ${selected.candidate_id}`} onError={() => setFailed(true)} />;
}
