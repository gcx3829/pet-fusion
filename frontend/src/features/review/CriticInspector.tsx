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
    <div className="critic-inspector" id="sidebar-panel-review" role="tabpanel" aria-label="Review Critic 审片">
      <div className="inspector-title"><p className="workbench-kicker">CRITIC / TIMELINE CONTROLLED</p><h2>Review</h2><p>候选切换仅由下方 Timeline 控制；这里始终展示当前 Timeline 节点的评价。</p></div>
      {!selected ? (
        <div className="critic-detail-empty" role="status">
          <strong>{active ? `正在等待 ${expectedCount} 张候选` : "Timeline 尚无可审片节点"}</strong>
          <span>{active ? "候选完成后会自动出现在 Timeline。" : "请在 Timeline 选择一个照片节点。"}</span>
        </div>
      ) : (
        <section className="critic-detail" aria-label={`当前 Timeline 候选 R${selected.round_index} V${selected.variant_index + 1}`}>
          <header>
            <span><small>CURRENT TIMELINE NODE</small><strong>R{selected.round_index} / V{selected.variant_index + 1}</strong></span>
            <output>{typeof selected.score === "number" ? selected.score.toFixed(1) : "N/A"}<small>/100</small></output>
          </header>
          <code>{selected.candidate_id}</code>
          <p>{selected.evaluation?.summary ?? "Critic 正在处理当前 Raw candidate。"}</p>
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
