import { useEffect, useState } from "react";
import { Icon } from "../../components/Icon";
import type { ResumeAction, SearchSnapshot } from "../../types";

interface ReviewActionsProps {
  snapshot: SearchSnapshot;
  isPending: boolean;
  error?: string | null;
  selectedCandidateId?: string | null;
  compact?: boolean;
  onAction: (action: ResumeAction, candidateId?: string, humanFeedback?: string) => void;
}

/** The one place where Resume actions are rendered for the Review surface. */
export function ReviewActions({
  snapshot,
  isPending,
  error,
  selectedCandidateId,
  compact = false,
  onAction,
}: ReviewActionsProps) {
  const [humanFeedback, setHumanFeedback] = useState("");
  useEffect(() => setHumanFeedback(""), [snapshot.round_index]);
  if (snapshot.status !== "waiting_for_human") return null;

  const allowed = new Set(snapshot.interrupt_payload?.allowed_actions ?? []);
  const canContinue = allowed.has("continue_one_round");
  const canCancel = allowed.has("cancel");
  const canAcceptGlobal = Boolean(snapshot.global_winner_id) && allowed.has("accept_global_winner");
  // Only a Timeline selection can become the visual anchor. The main viewer
  // and Global Winner never silently change this value.
  const selectedCandidate = selectedCandidateId
    ? snapshot.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId)
    : undefined;
  const selectedForContinue = selectedCandidate?.round_index === snapshot.round_index
    ? selectedCandidate.candidate_id
    : null;
  const historicalSelection = Boolean(selectedCandidate && !selectedForContinue);
  const selectedIsGlobal = selectedCandidate?.candidate_id === snapshot.global_winner_id;
  const canAcceptSelected = Boolean(selectedCandidate) && (
    (selectedIsGlobal && canAcceptGlobal)
    || (!selectedIsGlobal && allowed.has("accept_candidate"))
  );
  const showSeparateGlobalAction = canAcceptGlobal && !selectedIsGlobal;
  const selectedIssueCount = selectedCandidate?.evaluation?.issues.length ?? 0;

  const acceptSelected = () => {
    if (!selectedCandidate) return;
    if (selectedIsGlobal) onAction("accept_global_winner");
    else onAction("accept_candidate", selectedCandidate.candidate_id);
  };

  return (
    <section
      className={`review-actions-block ${compact ? "is-compact" : ""}`}
      aria-label={compact ? "审片操作" : undefined}
      aria-labelledby={compact ? undefined : "review-next-step-heading"}
    >
      {!compact && <header className="review-actions-heading">
        <div><span className="workbench-kicker">HUMAN DECISION</span><h3 id="review-next-step-heading">下一步</h3></div>
        <span>等待人工决定</span>
      </header>}

      {!compact && canContinue && (
        <div className={`review-refinement-context ${historicalSelection ? "is-warning" : ""}`} role="status">
          <Icon name={historicalSelection ? "warning" : "arrow"} />
          <span>
            <strong>{selectedForContinue ? "以当前选择为视觉参考" : historicalSelection ? "历史候选不能继续探索" : "从原片重新采样"}</strong>
            <small>{selectedForContinue
              ? `仍以原片和 Guidance Mask 为底；Critic 的 ${selectedIssueCount} 项检查会随候选上下文进入 Prompt Refiner。`
              : historicalSelection
                ? "可接受这张历史候选，或在时间线改选本轮图片。"
                : "未选择本轮候选，下一轮不会使用视觉锚点。"}</small>
          </span>
        </div>
      )}

      {canContinue && !compact && (
        <label className="review-feedback review-feedback--inspector" htmlFor="review-feedback-inspector">
          <span><strong>修改意见（可选）</strong><small>{humanFeedback.length} / 2000</small></span>
          <textarea
            id="review-feedback-inspector"
            aria-label="修改意见（可选）"
            value={humanFeedback}
            maxLength={2000}
            disabled={isPending}
            onChange={(event) => setHumanFeedback(event.target.value)}
            placeholder="描述主观目标，例如：猫再小一些；保留当前眼睛和毛色；接触阴影更自然。"
          />
          <small>这里写你对画面的判断。系统检查只是另一份输入，不会替你做接受决定。</small>
        </label>
      )}

      {error && <p className="review-error" role="alert">{error}</p>}

      <div className="review-action-row">
        {canContinue && (
          <button
            className="review-explore-button"
            type="button"
            disabled={isPending || historicalSelection}
            onClick={() => onAction("continue_one_round", selectedForContinue ?? undefined, humanFeedback.trim() || undefined)}
          >
            <Icon name="spark" />
            <span><strong>{isPending ? "正在提交…" : "继续探索一轮"}</strong><small>{selectedForContinue ? "当前候选作为参考" : "从原片重新生成"}</small></span>
            <Icon name="arrow" />
          </button>
        )}
        {canAcceptSelected && (
          <button className="review-accept-button" type="button" disabled={isPending} onClick={acceptSelected}>
            <Icon name="check" /> 接受当前图片
          </button>
        )}
        {showSeparateGlobalAction && (
          <button className="review-tertiary-button" type="button" disabled={isPending} onClick={() => onAction("accept_global_winner")}>
            接受历史最佳
          </button>
        )}
        {canCancel && (
          <button className="review-tertiary-button is-danger" type="button" disabled={isPending} onClick={() => onAction("cancel")}>
            结束搜索
          </button>
        )}
        {!canCancel && !canContinue && !canAcceptGlobal && !canAcceptSelected && <span className="review-readonly">本轮只能查看</span>}
      </div>
    </section>
  );
}
