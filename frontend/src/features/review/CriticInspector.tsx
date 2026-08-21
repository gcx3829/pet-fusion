import { ReviewActions } from "./ReviewActions";
import type { DimensionScores, ResumeAction, SearchSnapshot, SearchStatusValue } from "../../types";

interface CriticInspectorProps {
  snapshot?: SearchSnapshot;
  status: SearchStatusValue;
  expectedCount: number;
  selectedCandidateId?: string | null;
  isPending?: boolean;
  error?: string | null;
  onAction?: (action: ResumeAction, candidateId?: string, humanFeedback?: string) => void;
}

export function CriticInspector({
  snapshot,
  status,
  expectedCount,
  selectedCandidateId,
  isPending = false,
  error,
  onAction,
}: CriticInspectorProps) {
  // Timeline is the only review selector. A Global Winner is still available
  // as an explicit accept action, but must not silently become the candidate
  // used for human refinement when no Timeline node is selected.
  const selected = selectedCandidateId
    ? snapshot?.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId)
    : undefined;
  const metrics: { key: keyof DimensionScores; short: string; label: string }[] = [
    { key: "cat_identity", short: "ID", label: "身份" },
    { key: "perspective_scale", short: "PER", label: "透视" },
    { key: "optical_consistency", short: "OPT", label: "光学" },
    { key: "physical_integration", short: "PHY", label: "融合" },
  ];
  const active = status === "queued" || status === "running";
  return (
    <div className="critic-inspector" id="sidebar-panel-review" role="tabpanel" aria-label="审片">
      <div className="inspector-title"><h2>审片</h2><p>在下方时间线选择图片，查看评分和问题。</p></div>
      {!selected ? (
        <div className="critic-detail-empty" role="status">
          <strong>{active ? `正在生成 ${expectedCount} 张候选` : "还没有可审的图片"}</strong>
          <span>{active ? "生成后会自动出现在时间线。" : "请在下方时间线选择一张图片。"}</span>
        </div>
      ) : (
        <section className="critic-detail" aria-label={`当前候选：第 ${selected.round_index + 1} 轮，第 ${selected.variant_index + 1} 张`}>
          <header>
            <span><small>当前候选</small><strong>第 {selected.round_index + 1} 轮 / 第 {selected.variant_index + 1} 张</strong></span>
            <output>{typeof selected.score === "number" ? selected.score.toFixed(1) : "N/A"}<small>/100</small></output>
          </header>
          <code>{selected.candidate_id}</code>
          <p>{selected.evaluation?.summary ?? "正在检查这张候选图。"}</p>
          <div className="metric-grid critic-metric-grid">
            {metrics.map((metric) => {
              const value = selected.evaluation?.scores?.[metric.key];
              return <div className="metric-cell" key={metric.key}>
                <span>{metric.short}</span><strong>{typeof value === "number" ? value.toFixed(1) : "N/A"}</strong><small>{metric.label}</small>
                <i style={{ width: `${typeof value === "number" ? value : 0}%` }} />
              </div>;
            })}
          </div>
          {!!selected.evaluation?.issues.length && <ul className="issue-list critic-issue-list">
            {selected.evaluation.issues.map((issue, index) => <li key={issue.issue_id ?? `${issue.category}-${index}`} className={`issue-${issue.severity}`}>
              <span>{issue.severity}</span><p>{issue.evidence}</p>
            </li>)}
          </ul>}
        </section>
      )}
      {snapshot && onAction && <ReviewActions snapshot={snapshot} isPending={isPending} error={error} selectedCandidateId={selectedCandidateId} onAction={onAction} />}
    </div>
  );
}
