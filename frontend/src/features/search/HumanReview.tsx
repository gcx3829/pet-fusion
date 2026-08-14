import { useEffect, useState } from "react";
import { Icon } from "../../components/Icon";
import type { ResumeAction, SearchSnapshot } from "../../types";

interface HumanReviewProps {
  snapshot: SearchSnapshot;
  isPending: boolean;
  error?: string | null;
  selectedCandidateId?: string | null;
  onAction: (action: ResumeAction, candidateId?: string, humanFeedback?: string) => void;
}

export function HumanReview({
  snapshot,
  isPending,
  error,
  selectedCandidateId,
  onAction,
}: HumanReviewProps) {
  const [humanFeedback, setHumanFeedback] = useState("");
  useEffect(() => {
    setHumanFeedback("");
  }, [snapshot.round_index]);
  if (snapshot.status !== "waiting_for_human") return null;
  const canAccept = Boolean(snapshot.global_winner_id);
  const allowed = new Set(snapshot.interrupt_payload?.allowed_actions ?? []);
  const canCancel = allowed.has("cancel");
  const canContinue = allowed.has("continue_one_round");
  const canAcceptAction = canAccept && allowed.has("accept_global_winner");
  const effectiveSelectedCandidateId = selectedCandidateId ?? snapshot.global_winner_id ?? null;
  const selectedCandidate = effectiveSelectedCandidateId
    ? snapshot.candidates.find((candidate) => candidate.candidate_id === effectiveSelectedCandidateId)
    : undefined;
  const canAcceptSelected = Boolean(
    selectedCandidate
      && selectedCandidate.candidate_id !== snapshot.global_winner_id
      && allowed.has("accept_candidate"),
  );
  return (
    <aside className="human-review" aria-labelledby="review-heading">
      <div className="review-icon"><Icon name="aperture" /></div>
      <div className="review-copy">
        <p className="eyebrow">HUMAN INTERRUPT</p>
        <h2 id="review-heading">轮到你审片</h2>
        <p>{snapshot.stop_reason === "mock_round_complete"
          ? "本轮候选和自动 Critic 已完成。你可以查看分数、问题和历史最佳，再决定接受或继续。"
          : snapshot.stop_reason ?? "自动搜索在安全边界停下，等待摄影师决定。"}</p>
        <p className="review-authority-note">
          当前审片图：<strong>Raw Candidate</strong>。Critic 与人工评价不读取 protected 或 fused 派生图。
        </p>
        {!!snapshot.interrupt_payload?.blocking_issues?.length && (
          <ul>
            {snapshot.interrupt_payload.blocking_issues.map((issue) => <li key={issue}>{issue}</li>)}
          </ul>
        )}
        {error && <p className="review-error" role="alert">{error}</p>}
      </div>
      {canContinue && (
        <div className="review-feedback">
          <label htmlFor="human-feedback">人工反馈（可选，用于下一轮 prompt）</label>
          <textarea
            id="human-feedback"
            value={humanFeedback}
            maxLength={2000}
            onChange={(event) => setHumanFeedback(event.target.value)}
            placeholder="例如：猫再小一些，身体要更贴合窗台；保留当前眼睛和毛色。"
            disabled={isPending}
          />
          <small>
            选中的 Raw 候选会作为本轮审片锚点；下一轮仍从原始照片和参考图重新生成，不会把候选当底图。
          </small>
        </div>
      )}
      <div className="review-actions">
        {canCancel && <button className="secondary-button" type="button" disabled={isPending} onClick={() => onAction("cancel")}>取消搜索</button>}
        {canContinue && <button
          className="secondary-button"
          type="button"
          disabled={isPending}
          onClick={() => onAction(
            "continue_one_round",
            effectiveSelectedCandidateId ?? undefined,
            humanFeedback.trim() || undefined,
          )}
        >再生成一轮</button>}
        {canAcceptAction && (
          <button className="primary-button" type="button" disabled={isPending} onClick={() => onAction("accept_global_winner")}>
            <Icon name="check" /> 接受历史最佳 Raw
          </button>
        )}
        {canAcceptSelected && selectedCandidate && (
          <button
            className="primary-button"
            type="button"
            disabled={isPending}
            onClick={() => onAction("accept_candidate", selectedCandidate.candidate_id)}
          >
            <Icon name="check" /> 接受所选 Raw
          </button>
        )}
        {!canCancel && !canContinue && !canAcceptAction && !canAcceptSelected && (
          <span className="review-readonly">本轮仅供查看 · Resume 动作尚未开放</span>
        )}
      </div>
    </aside>
  );
}
