import { useEffect, useMemo, useState } from "react";
import { Icon } from "../../components/Icon";
import { rawCandidateUrl } from "../../lib/raw";
import type { DimensionScores, SearchCandidate, SearchStatusValue } from "../../types";

interface CandidateGalleryProps {
  candidates: SearchCandidate[];
  status: SearchStatusValue;
  expectedCount: number;
  activeRoundIndex?: number;
  selectedCandidateId?: string | null;
  onSelect?: (candidateId: string | null) => void;
}

const metrics: { key: keyof DimensionScores; short: string; label: string }[] = [
  { key: "cat_identity", short: "ID", label: "身份" },
  { key: "perspective_scale", short: "PER", label: "透视" },
  { key: "optical_consistency", short: "OPT", label: "光学" },
  { key: "physical_integration", short: "PHY", label: "融合" },
];

function ImageFrame({ candidate }: { candidate: SearchCandidate }) {
  const [failed, setFailed] = useState(false);
  const rawImageUrl = rawCandidateUrl(candidate);
  if (!rawImageUrl || failed) {
    return (
      <div className="candidate-image-fallback" role="img" aria-label="候选图片暂不可用">
        <Icon name="image" />
        <span>等待资产</span>
      </div>
    );
  }
  return (
    <img
      src={rawImageUrl}
      alt={`第 ${candidate.round_index + 1} 轮，第 ${candidate.variant_index + 1} 张候选 Raw`}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

function scoreLabel(score?: number): string {
  return typeof score === "number" ? score.toFixed(1) : "N/A";
}

export function CandidateGallery({
  candidates,
  status,
  expectedCount,
  activeRoundIndex,
  selectedCandidateId,
  onSelect,
}: CandidateGalleryProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => {
    if (selectedId && !candidates.some((candidate) => candidate.candidate_id === selectedId)) {
      setSelectedId(null);
      onSelect?.(null);
    }
  }, [candidates, onSelect, selectedId]);
  useEffect(() => {
    // A new automatic round is a new review context.  Do not carry a prior
    // round's manual selection into the next candidate set.
    setSelectedId(null);
    onSelect?.(null);
  }, [activeRoundIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  const selected = useMemo(
    () => candidates.find((candidate) => candidate.candidate_id === (selectedCandidateId ?? selectedId))
      ?? candidates.find((candidate) => candidate.is_global_winner)
      ?? candidates[0],
    [candidates, selectedCandidateId, selectedId],
  );
  const isActive = status === "queued" || status === "running";

  return (
    <section className="panel gallery-panel" aria-labelledby="gallery-heading">
      <div className="panel-heading gallery-heading-row">
        <div>
          <p className="eyebrow">04 / CONTACT SHEET</p>
          <h2 id="gallery-heading">候选接触印样</h2>
        </div>
        <p className="gallery-summary">
          <strong>{String(candidates.length).padStart(2, "0")}</strong>
          <span>EXPOSURES</span>
        </p>
      </div>
      <div className="raw-review-notice" role="note">
        <strong>RAW CANDIDATE</strong>
        <span>Critic 与人工评价均以模型原始输出为准；Fusion Mask 只有在用户主动融合后才会影响预览或导出。</span>
      </div>

      {!candidates.length && !isActive ? (
        <div className="empty-gallery">
          <div className="film-frame-stack" aria-hidden="true"><i /><i /><i /></div>
          <p>搜索结果会像接触印样一样排列在这里。</p>
          <small>每张候选都来自不可变原片，而不是上一张生成图。</small>
        </div>
      ) : (
        <div className="contact-sheet" aria-live="polite">
          {candidates.map((candidate) => {
            const issues = candidate.evaluation?.issues ?? [];
            const blocking = issues.filter((issue) => issue.severity === "blocking").length;
            const warnings = issues.filter((issue) => issue.severity === "warning").length;
            const isSelected = candidate.candidate_id === selected?.candidate_id;
            return (
              <article
                className={`candidate-card ${isSelected ? "is-selected" : ""} ${candidate.is_global_winner ? "is-global" : ""}`}
                key={candidate.candidate_id}
              >
                <button
                  className="candidate-image-button"
                  type="button"
                  aria-pressed={isSelected}
                  onClick={() => {
                    setSelectedId(candidate.candidate_id);
                    onSelect?.(candidate.candidate_id);
                  }}
                >
                  <span className="film-edge film-edge--top" aria-hidden="true" />
                  <ImageFrame candidate={candidate} />
                  <span className="candidate-number">{String(candidate.variant_index + 1).padStart(2, "0")}</span>
                  {candidate.is_global_winner && <span className="winner-ribbon">GLOBAL WINNER</span>}
                  {candidate.is_round_winner && !candidate.is_global_winner && <span className="round-ribbon">ROUND PICK</span>}
                </button>
                <div className="candidate-meta">
                  <div>
                    <span>R{candidate.round_index} · V{candidate.variant_index + 1}</span>
                    <code>{candidate.candidate_id.slice(0, 8)}</code>
                  </div>
                  <p className="candidate-score"><strong>{scoreLabel(candidate.score)}</strong><small>/ 100</small></p>
                </div>
                <div className="candidate-flags">
                  <span className="raw-flag">RAW REVIEW</span>
                  <span className={blocking ? "has-blocking" : ""}>{blocking} BLOCK</span>
                  <span>{warnings} WARN</span>
                  {!candidate.evaluation && <span>CRITIC PENDING</span>}
                </div>
              </article>
            );
          })}
          {isActive && Array.from({ length: Math.max(0, expectedCount - candidates.length) }, (_, index) => (
            <div className="candidate-card candidate-skeleton" key={`skeleton-${index}`} aria-label="候选正在生成">
              <div className="skeleton-image"><Icon name="aperture" /></div>
              <span>DEVELOPING…</span>
            </div>
          ))}
        </div>
      )}

      {selected && (
        <div className="inspection-drawer">
          <div className="inspection-head">
            <div>
              <span className="eyebrow">SELECTED RAW CANDIDATE</span>
              <strong>R{selected.round_index} / V{selected.variant_index + 1}</strong>
            </div>
            <p>{selected.evaluation?.summary ?? "正在查看 Raw candidate，等待结构化 Critic 评价。"}</p>
          </div>
          <div className="metric-grid">
            {metrics.map((metric) => {
              const value = selected.evaluation?.scores?.[metric.key];
              return (
                <div className="metric-cell" key={metric.key}>
                  <span>{metric.short}</span>
                  <strong>{scoreLabel(value)}</strong>
                  <small>{metric.label}</small>
                  <i style={{ width: `${typeof value === "number" ? value : 0}%` }} />
                </div>
              );
            })}
          </div>
          {!!selected.evaluation?.issues.length && (
            <ul className="issue-list">
              {selected.evaluation.issues.slice(0, 3).map((issue, index) => (
                <li key={issue.issue_id ?? `${issue.category}-${index}`} className={`issue-${issue.severity}`}>
                  <span>{issue.severity}</span>
                  <p>{issue.evidence}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
