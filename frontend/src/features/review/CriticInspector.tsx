import { Icon } from "../../components/Icon";
import type {
  CriticIssue,
  DimensionScores,
  ResumeAction,
  SearchSnapshot,
  SearchStatusValue,
} from "../../types";
import { ReviewActions } from "./ReviewActions";

interface CriticInspectorProps {
  snapshot?: SearchSnapshot;
  status: SearchStatusValue;
  expectedCount: number;
  selectedCandidateId?: string | null;
  isPending?: boolean;
  error?: string | null;
  onAction?: (action: ResumeAction, candidateId?: string, humanFeedback?: string) => void;
}

const METRICS: { key: keyof DimensionScores; label: string }[] = [
  { key: "cat_identity", label: "宠物身份" },
  { key: "pose_geometry", label: "姿态结构" },
  { key: "perspective_scale", label: "透视尺度" },
  { key: "lighting_color", label: "光线色彩" },
  { key: "optical_consistency", label: "光学一致" },
  { key: "physical_integration", label: "物理融合" },
  { key: "scene_preservation", label: "场景保留" },
  { key: "overall_photographic_naturalness", label: "摄影自然度" },
];

const CATEGORY_LABELS: Record<string, string> = {
  cat_identity: "宠物身份",
  identity: "宠物身份",
  pose_geometry: "姿态结构",
  perspective_scale: "透视尺度",
  lighting_color: "光线色彩",
  optical_consistency: "光学一致",
  physical_integration: "物理融合",
  scene_preservation: "场景保留",
  asset_integrity: "素材完整性",
  prompt_adherence: "需求遵循",
  photographic_naturalness: "摄影自然度",
  unclassified: "其他",
};

const SEVERITY_LABELS: Record<CriticIssue["severity"], string> = {
  blocking: "阻断",
  warning: "复核",
  info: "提示",
};

function metricTone(value?: number) {
  if (typeof value !== "number") return "is-empty";
  if (value < 60) return "is-low";
  if (value < 75) return "is-watch";
  return "is-stable";
}

function confidenceLabel(confidence?: number) {
  if (typeof confidence !== "number") return null;
  const normalized = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.round(normalized)}% 置信度`;
}

function hasClientSemanticConflict(
  evaluation: SearchSnapshot["candidates"][number]["evaluation"],
  score?: number,
) {
  if (!evaluation) return false;
  const values = evaluation.scores
    ? Object.values(evaluation.scores).filter(
      (value): value is number => typeof value === "number" && Number.isFinite(value),
    )
    : [];
  const ambiguousScale = values.length === METRICS.length && Math.max(...values) <= 10;
  const positiveVerdict = evaluation.no_meaningful_defect === true
    || evaluation.recommended_action === "accept";
  const contradictedByScore = positiveVerdict && (
    (typeof score === "number" && score < 50)
    || values.some((value) => value < 60)
  );
  return evaluation.semantic_conflict === true || ambiguousScale || contradictedByScore;
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
  // Timeline is the sole review selector. Nothing in this inspector changes
  // the selected candidate or silently substitutes the Global Winner.
  const selected = selectedCandidateId
    ? snapshot?.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId)
    : undefined;
  const active = status === "queued" || status === "running";
  const evaluation = selected?.evaluation;
  const issues = evaluation?.issues ?? [];
  const blockingCount = issues.filter((issue) => issue.severity === "blocking").length;
  const warningCount = issues.filter((issue) => issue.severity === "warning").length;
  const score = selected?.score ?? evaluation?.total_score;
  const semanticConflict = hasClientSemanticConflict(evaluation, score);
  const isCurrentRound = selected?.round_index === snapshot?.round_index;
  const verdict = !evaluation
    ? { tone: "is-pending", label: "正在分析", detail: "Critic 结果尚未返回" }
    : semanticConflict
      ? { tone: "is-warning", label: "Critic 输出矛盾", detail: "量表、评分或文字结论不一致，禁止自动采信" }
      : blockingCount > 0
        ? { tone: "is-blocking", label: `${blockingCount} 个阻断问题`, detail: "建议先处理结构性缺陷" }
        : selected?.ranker_eligible === false
          ? { tone: "is-warning", label: "未通过自动门槛", detail: "存在硬约束失败，请人工复核原图" }
        : warningCount > 0
          ? { tone: "is-warning", label: `${warningCount} 项需要复核`, detail: "没有自动结论，请结合原图判断" }
          : { tone: "is-clear", label: "未发现明确缺陷", detail: "仍需人工检查整体可信度" };

  return (
    <div className="critic-inspector" id="sidebar-panel-review" role="tabpanel" aria-label="审片">
      <header className="review-inspector-heading">
        <div>
          <span className="workbench-kicker">RAW REVIEW</span>
          <h2>审片</h2>
        </div>
        <span className="review-authority-badge">AI 仅供参考</span>
      </header>

      {!selected ? (
        <div className="critic-detail-empty" role="status">
          <span className="critic-empty-icon"><Icon name={active ? "spark" : "image"} /></span>
          <strong>{active ? `正在生成 ${expectedCount} 张候选` : "从时间线选择一张图片"}</strong>
          <span>{active ? "图片和检查状态会沿时间线出现。" : "时间线是唯一的图片切换入口。"}</span>
        </div>
      ) : (
        <section className="critic-detail" aria-label={`当前候选：第 ${selected.round_index + 1} 轮，第 ${selected.variant_index + 1} 张`}>
          <div className="critic-selection-context">
            <span className="critic-selection-index">R{selected.round_index + 1} · {String(selected.variant_index + 1).padStart(2, "0")}</span>
            <span className={isCurrentRound ? "is-current" : "is-history"}>{isCurrentRound ? "本轮选择" : "历史候选"}</span>
            {selected.is_global_winner && <span className="is-best">历史最佳</span>}
            <code title={selected.candidate_id}>{selected.candidate_id}</code>
          </div>

          <div className={`critic-verdict ${verdict.tone}`}>
            <span className="critic-verdict-mark"><Icon name={semanticConflict || blockingCount > 0 || selected?.ranker_eligible === false ? "warning" : evaluation ? "check" : "spark"} /></span>
            <span><small>系统检查</small><strong>{verdict.label}</strong><em>{verdict.detail}</em></span>
            <output aria-label={`AI 参考分 ${typeof score === "number" ? score.toFixed(1) : "不可用"}`}>
              <small>参考分</small>
              <strong>{typeof score === "number" ? score.toFixed(1) : "—"}</strong>
            </output>
          </div>

          <div className="critic-summary">
            <span>摘要</span>
            <p>{evaluation?.summary ?? "正在读取原始候选并生成结构化检查结果。"}</p>
          </div>

          <section className="critic-section" aria-labelledby="critic-dimensions-heading">
            <header><h3 id="critic-dimensions-heading">检查维度</h3><span>0–100 · 横向比较</span></header>
            <div className="critic-metric-list">
              {METRICS.map((metric) => {
                const value = evaluation?.scores?.[metric.key];
                return (
                  <div className={`critic-metric ${metricTone(value)}`} key={metric.key}>
                    <span>{metric.label}</span>
                    <i><b style={{ width: `${typeof value === "number" ? Math.min(100, Math.max(0, value)) : 0}%` }} /></i>
                    <output>{typeof value === "number" ? value.toFixed(0) : "—"}</output>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="critic-section critic-findings" aria-labelledby="critic-findings-heading">
            <header>
              <h3 id="critic-findings-heading">检查发现</h3>
              <span>{issues.length ? `${issues.length} 项` : "无明确问题"}</span>
            </header>
            {issues.length ? (
              <div className="critic-issue-list">
                {issues.map((issue, index) => {
                  const confidence = confidenceLabel(issue.confidence);
                  return (
                    <details key={issue.issue_id ?? `${issue.category}-${index}`} className={`critic-issue issue-${issue.severity}`} open={issue.severity === "blocking"}>
                      <summary>
                        <span className="critic-issue-severity">{SEVERITY_LABELS[issue.severity]}</span>
                        <strong>{CATEGORY_LABELS[issue.category] ?? issue.category}</strong>
                        <Icon name="chevron" />
                      </summary>
                      <div className="critic-issue-body">
                        <p>{issue.evidence}</p>
                        {(issue.region || confidence) && <div className="critic-issue-meta">
                          {issue.region && <span>区域 · {issue.region}</span>}
                          {confidence && <span>{confidence}</span>}
                        </div>}
                        {issue.suggested_fix && <div className="critic-suggestion"><span>建议修正</span><p>{issue.suggested_fix}</p></div>}
                      </div>
                    </details>
                  );
                })}
              </div>
            ) : (
              <p className="critic-findings-empty">系统没有报告明确异常；这不等同于图片已通过，请继续检查肢体、接触关系和整体光影。</p>
            )}
          </section>
        </section>
      )}

      {snapshot && onAction && (
        <ReviewActions
          snapshot={snapshot}
          isPending={isPending}
          error={error}
          selectedCandidateId={selectedCandidateId}
          onAction={onAction}
        />
      )}
    </div>
  );
}
