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
        <h2 id="review-heading">轮到你审片</h2>
        <p>{snapshot.stop_reason === "mock_round_complete"
          ? "本轮候选已生成并检查完毕。你可以接受一张，或提出修改后再生成。"
          : snapshot.stop_reason ?? "生成已暂停，等待你的决定。"}</p>
        <p className="review-authority-note">
          当前审片图是模型的原始输出，局部融合不会影响评分。
        </p>
        <p className="review-rebase-note">
          {selectedCurrentRound
            ? "下一轮仍从原片生成，并参考你选中的这张图。"
            : selectedCandidate
              ? "当前查看的是历史候选，可以比较或接受，但不能作为下一轮参考。"
              : "没有选中候选，下一轮只使用原片和宠物参考图。"}
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
