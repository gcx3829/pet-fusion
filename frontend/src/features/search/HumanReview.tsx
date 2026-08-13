import { Icon } from "../../components/Icon";
import type { ResumeAction, SearchSnapshot } from "../../types";

interface HumanReviewProps {
  snapshot: SearchSnapshot;
  isPending: boolean;
  error?: string | null;
  onAction: (action: ResumeAction) => void;
}

export function HumanReview({ snapshot, isPending, error, onAction }: HumanReviewProps) {
  if (snapshot.status !== "waiting_for_human") return null;
  const canAccept = Boolean(snapshot.global_winner_id);
  const allowed = new Set(snapshot.interrupt_payload?.allowed_actions ?? []);
  const canCancel = allowed.has("cancel");
  const canContinue = allowed.has("continue_one_round");
  const canAcceptAction = canAccept && allowed.has("accept_global_winner");
  return (
    <aside className="human-review" aria-labelledby="review-heading">
      <div className="review-icon"><Icon name="aperture" /></div>
      <div className="review-copy">
        <p className="eyebrow">HUMAN INTERRUPT</p>
        <h2 id="review-heading">轮到你审片</h2>
        <p>{snapshot.stop_reason === "mock_round_complete"
          ? "Mock 生成轮已完成。候选已持久化，真实 Critic / Ranker 接入后将在这里给出历史最佳。"
          : snapshot.stop_reason ?? "自动搜索在安全边界停下，等待摄影师决定。"}</p>
        {!!snapshot.interrupt_payload?.blocking_issues?.length && (
          <ul>
            {snapshot.interrupt_payload.blocking_issues.map((issue) => <li key={issue}>{issue}</li>)}
          </ul>
        )}
        {error && <p className="review-error" role="alert">{error}</p>}
      </div>
      <div className="review-actions">
        {canCancel && <button className="secondary-button" type="button" disabled={isPending} onClick={() => onAction("cancel")}>取消搜索</button>}
        {canContinue && <button className="secondary-button" type="button" disabled={isPending} onClick={() => onAction("continue_one_round")}>再搜索一轮</button>}
        {canAcceptAction && (
          <button className="primary-button" type="button" disabled={isPending} onClick={() => onAction("accept_global_winner")}>
            <Icon name="check" /> 接受历史最佳
          </button>
        )}
        {!canCancel && !canContinue && !canAcceptAction && (
          <span className="review-readonly">本轮仅供查看 · Resume 动作尚未开放</span>
        )}
      </div>
    </aside>
  );
}
