import { Icon } from "../../components/Icon";
import { ReviewActions } from "../review/ReviewActions";
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
  if (snapshot.status !== "waiting_for_human") return null;
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
      </div>
      <ReviewActions snapshot={snapshot} isPending={isPending} error={error} selectedCandidateId={selectedCandidateId} onAction={onAction} />
    </aside>
  );
}
