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
  const selected = selectedCandidateId ?? snapshot.global_winner_id ?? null;
  const canAcceptSelected = Boolean(selected)
    && selected !== snapshot.global_winner_id
    && allowed.has("accept_candidate");

  return (
    <div className={`review-actions-block ${compact ? "is-compact" : ""}`}>
      {canContinue && !compact && (
        <label className="review-feedback review-feedback--inspector" htmlFor="review-feedback-inspector">
          <span>人工反馈（可选，用于下一轮 prompt）</span>
          <textarea id="review-feedback-inspector" value={humanFeedback} maxLength={2000} disabled={isPending} onChange={(event) => setHumanFeedback(event.target.value)} placeholder="例如：主体再小一些，保留当前眼睛和毛色。" />
        </label>
      )}
      {error && <p className="review-error" role="alert">{error}</p>}
      <div className="review-action-row">
        {canCancel && <button className="secondary-button" type="button" disabled={isPending} onClick={() => onAction("cancel")}>取消搜索</button>}
        {canContinue && <button className="secondary-button" type="button" disabled={isPending} onClick={() => onAction("continue_one_round", selected ?? undefined, humanFeedback.trim() || undefined)}>再生成一轮</button>}
        {canAcceptGlobal && <button className="primary-button" type="button" disabled={isPending} onClick={() => onAction("accept_global_winner")}><Icon name="check" /> 接受历史最佳 Raw</button>}
        {canAcceptSelected && <button className="primary-button" type="button" disabled={isPending} onClick={() => onAction("accept_candidate", selected ?? undefined)}><Icon name="check" /> 接受所选 Raw</button>}
        {!canCancel && !canContinue && !canAcceptGlobal && !canAcceptSelected && <span className="review-readonly">本轮仅供查看 · Resume 动作尚未开放</span>}
      </div>
    </div>
  );
}
