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
      {!compact && <h3 className="sr-only" id="review-next-step-heading">下一步</h3>}

      {!compact && canContinue && (
        <div className={`review-refinement-context ${historicalSelection ? "is-warning" : ""}`} role="status" title={selectedForContinue ? `AI 检查 ${selectedIssueCount} 项` : undefined}>
          <Icon name={historicalSelection ? "warning" : "arrow"} />
          <strong>{selectedForContinue ? "下一轮参考当前候选" : historicalSelection ? "历史候选不能继续" : "下一轮从原片生成"}</strong>
        </div>
      )}

      {canContinue && !compact && (
        <label className="review-feedback review-feedback--inspector" htmlFor="review-feedback-inspector">
          <span><strong>修改意见</strong><small>{humanFeedback.length} / 2000</small></span>
          <textarea
            id="review-feedback-inspector"
            aria-label="修改意见（可选）"
            value={humanFeedback}
            maxLength={2000}
            disabled={isPending}
            onChange={(event) => setHumanFeedback(event.target.value)}
            placeholder="例如：猫小一些，保留眼睛和毛色。"
          />
        </label>
      )}

      {error && <p className="review-error" role="alert">{error}</p>}

      <div className="review-action-row">
        {canContinue && (
          <button
            className="review-explore-button"
            type="button"
            aria-label="继续探索一轮"
            disabled={isPending || historicalSelection}
            onClick={() => onAction("continue_one_round", selectedForContinue ?? undefined, humanFeedback.trim() || undefined)}
          >
            <Icon name="spark" />
            <strong>{isPending ? "正在提交…" : "继续一轮"}</strong>
          </button>
        )}
        {canAcceptSelected && (
          <button className="review-accept-button" type="button" aria-label="接受当前图片" disabled={isPending} onClick={acceptSelected}>
            <Icon name="check" /> 接受
          </button>
        )}
        {showSeparateGlobalAction && (
          <button className="review-tertiary-button" type="button" aria-label="接受历史最佳" disabled={isPending} onClick={() => onAction("accept_global_winner")}>
            接受最佳
          </button>
        )}
        {canCancel && (
          <button className="review-tertiary-button is-danger" type="button" aria-label="结束搜索" disabled={isPending} onClick={() => onAction("cancel")}>
            结束
          </button>
        )}
        {!canCancel && !canContinue && !canAcceptGlobal && !canAcceptSelected && <span className="review-readonly">本轮只能查看</span>}
      </div>
    </section>
  );
}
