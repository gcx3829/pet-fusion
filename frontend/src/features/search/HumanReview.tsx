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
  const selectedCandidate = selectedCandidateId
    ? snapshot.candidates.find((candidate) => candidate.candidate_id === selectedCandidateId)
    : undefined;
  const selectedCurrentRound = selectedCandidate?.round_index === snapshot.round_index;
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
        <p className="review-rebase-note">
          {selectedCurrentRound
            ? "再生成时：原图仍为编辑底片，所选 Raw 作为视觉参考；不会把它替换成新的底片。"
            : selectedCandidate
              ? "当前查看的是历史轮 Raw；它可供比较或接受，但不能作为下一轮视觉锚点。"
              : "再生成时：仅使用不可变原图与宠物参考图（source-only）；请先在 Timeline 选择候选，或明确按 source-only 继续。"}
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
