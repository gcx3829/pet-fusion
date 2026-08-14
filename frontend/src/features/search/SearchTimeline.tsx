import { Icon } from "../../components/Icon";
import type { EventConnectionState } from "../../lib/events";
import type { SearchEvent, SearchStatusValue } from "../../types";

interface SearchTimelineProps {
  events: SearchEvent[];
  status: SearchStatusValue;
  connectionState: EventConnectionState;
  roundIndex: number;
  activeDirectives: { instruction: string }[];
}

const connectionLabels: Record<EventConnectionState, string> = {
  idle: "未连接",
  connecting: "正在连接",
  open: "实时",
  reconnecting: "轮询回退",
  settled: "已结束",
  unavailable: "轮询模式",
};

const eventLabels: Record<string, { title: string; tone: string }> = {
  "search.queued": { title: "搜索任务进入队列", tone: "neutral" },
  "search.started": { title: "搜索图开始执行", tone: "active" },
  "round.queued": { title: "人工反馈已提交，下一轮排队", tone: "active" },
  "round.generation.started": { title: "从不可变原片生成候选", tone: "active" },
  "round.candidate.ready": { title: "一张候选已显影", tone: "candidate" },
  "round.critic.started": { title: "独立摄影审片开始", tone: "active" },
  "round.evaluation.ready": { title: "结构化审片完成", tone: "candidate" },
  "round.winner.updated": { title: "本轮最佳已更新", tone: "winner" },
  "search.global_winner.updated": { title: "历史最佳已更新", tone: "winner" },
  "search.planner.ready": { title: "下一轮修正已收敛", tone: "active" },
  "search.interrupted": { title: "搜索等待人工审片", tone: "warning" },
  "search.waiting_for_human": { title: "候选已交给摄影师", tone: "warning" },
  "search.accepted": { title: "历史最佳已接受", tone: "winner" },
  "search.failed": { title: "搜索停止：执行失败", tone: "danger" },
  "search.cancelled": { title: "搜索已取消", tone: "neutral" },
};

function eventDetail(event: SearchEvent): string {
  const candidate = typeof event.data.candidate === "object" && event.data.candidate !== null
    ? event.data.candidate as Record<string, unknown>
    : undefined;
  if (candidate) {
    const round = Number(candidate.round_index ?? 0);
    const variant = Number(candidate.variant_index ?? 0) + 1;
    return `Round ${round} · Variant ${variant}`;
  }
  const score = event.data.score ?? event.data.global_winner_score;
  if (typeof score === "number") return `摄影总分 ${score.toFixed(1)}`;
  const reason = event.data.reason ?? event.data.stop_reason;
  if (typeof reason === "string") return reason;
  return event.type;
}

function timeLabel(timestamp?: string): string {
  if (!timestamp) return "NOW";
  const date = new Date(timestamp);
  if (Number.isNaN(date.valueOf())) return "NOW";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function SearchTimeline({
  events,
  status,
  connectionState,
  roundIndex,
  activeDirectives,
}: SearchTimelineProps) {
  return (
    <section className="panel timeline-panel" aria-labelledby="timeline-heading">
      <div className="panel-heading timeline-heading-row">
        <div>
          <p className="eyebrow">05 / PROCESS LOG</p>
          <h2 id="timeline-heading">搜索时间线</h2>
        </div>
        <span className={`connection-state connection-${connectionState}`}>
          <Icon name="wave" /> {connectionLabels[connectionState]}
        </span>
      </div>

      {!events.length ? (
        <div className="timeline-empty">
          <span className="timeline-rail" />
          <p>{status === "idle" ? "开始搜索后，这里只记录结构化事件与决策依据。" : "正在等待第一条工作流事件…"}</p>
        </div>
      ) : (
        <ol className="timeline-list" aria-live="polite">
          {events.map((event, index) => {
            const descriptor = eventLabels[event.type] ?? { title: event.type, tone: "neutral" };
            return (
              <li className={`timeline-event tone-${descriptor.tone}`} key={`${event.id}-${index}`}>
                <time>{timeLabel(event.created_at)}</time>
                <span className="event-marker" />
                <div>
                  <strong>{descriptor.title}</strong>
                  <p>{eventDetail(event)}</p>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      <div className="timeline-footer">
        <span>ROUND <b>{roundIndex}</b></span>
        <span>ACTIVE DIRECTIVES <b>{activeDirectives.length}</b></span>
        <span>CHECKPOINT <b>{status === "idle" ? "—" : "DURABLE"}</b></span>
      </div>
      {!!activeDirectives.length && (
        <div className="directive-stack">
          {activeDirectives.map((directive, index) => (
            <p key={`${directive.instruction}-${index}`}><span>0{index + 1}</span>{directive.instruction}</p>
          ))}
        </div>
      )}
    </section>
  );
}
