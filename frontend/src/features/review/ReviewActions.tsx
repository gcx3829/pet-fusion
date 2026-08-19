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
  // A continue request is allowed to be source-only. Never silently promote
  // Global Winner (or whatever the main worker is showing) to the visual
  // anchor: only the currently selected Timeline photo may be submitted.
  const selectedCandidate = selectedCandidateId
    ? snapshot.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId)
    : undefined;
  const selectedForContinue = selectedCandidate?.round_index === snapshot.round_index
    ? selectedCandidate.candidate_id
    : null;
  const historicalSelection = Boolean(selectedCandidate && !selectedForContinue);
  const canAcceptSelected = Boolean(selectedCandidate)
    && selectedCandidate?.candidate_id !== snapshot.global_winner_id
    && allowed.has("accept_candidate");

  return (
    <div className={`review-actions-block ${compact ? "is-compact" : ""}`}>
      {!compact && canContinue && (
        <p className="review-rebase-note" role="status">
          {selectedForContinue
            ? "原图仍为编辑底片，所选 Raw 作为视觉参考。"
            : historicalSelection
              ? "当前选中的是历史轮 Raw：可以查看或接受，但不能作为下一轮视觉锚点。请选当前轮候选，或点击底图后 source-only 继续。"
              : "未选择 Timeline 候选：本轮将 source-only 重生成，不会偷偷使用 Global Winner。"}
        </p>
      )}
      {canContinue && !compact && (
        <label className="review-feedback review-feedback--inspector" htmlFor="review-feedback-inspector">
          <span>人工反馈（可选，用于下一轮 prompt）</span>
          <textarea id="review-feedback-inspector" value={humanFeedback} maxLength={2000} disabled={isPending} onChange={(event) => setHumanFeedback(event.target.value)} placeholder="例如：主体再小一些，保留当前眼睛和毛色。" />
        </label>
      )}
      {error && <p className="review-error" role="alert">{error}</p>}
      <div className="review-action-row">
        {canCancel && <button className="secondary-button" type="button" disabled={isPending} onClick={() => onAction("cancel")}>取消搜索</button>}
        {canContinue && <button className="secondary-button" type="button" disabled={isPending || historicalSelection} onClick={() => onAction("continue_one_round", selectedForContinue ?? undefined, humanFeedback.trim() || undefined)}>再生成一轮</button>}
        {canAcceptGlobal && <button className="primary-button" type="button" disabled={isPending} onClick={() => onAction("accept_global_winner")}><Icon name="check" /> 接受历史最佳 Raw</button>}
        {canAcceptSelected && <button className="primary-button" type="button" disabled={isPending} onClick={() => onAction("accept_candidate", selectedCandidate?.candidate_id)}><Icon name="check" /> 接受所选 Raw</button>}
        {!canCancel && !canContinue && !canAcceptGlobal && !canAcceptSelected && <span className="review-readonly">本轮仅供查看 · Resume 动作尚未开放</span>}
      </div>
    </div>
  );
}
