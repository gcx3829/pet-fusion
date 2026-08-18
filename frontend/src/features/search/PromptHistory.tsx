import type { PromptHistoryEntry } from "../../types";

interface PromptHistoryProps {
  history: PromptHistoryEntry[];
}

function shortHash(value?: string): string {
  if (!value) return "N/A";
  return `${value.slice(0, 12)}…`;
}

function PromptBlock({
  label,
  prompt,
  hash,
  open = false,
}: {
  label: string;
  prompt: string;
  hash?: string;
  open?: boolean;
}) {
  return (
    <details className="prompt-block" open={open}>
      <summary>
        <span>{label}</span>
        <code title={hash}>{shortHash(hash)}</code>
      </summary>
      <pre>{prompt}</pre>
    </details>
  );
}

export function PromptHistory({ history }: PromptHistoryProps) {
  const latestRoundIndex = history.length ? history[history.length - 1].round_index : null;
  return (
    <section className="panel prompt-history-panel" aria-labelledby="prompt-history-heading">
      <div className="panel-heading prompt-history-heading">
        <div>
          <p className="eyebrow">04 / PROMPT TRACE</p>
          <h2 id="prompt-history-heading">提示词轨迹</h2>
        </div>
        <span className="prompt-history-count">
          {history.length ? `${history.length} ROUND${history.length > 1 ? "S" : ""}` : "等待编译"}
        </span>
      </div>

      {!history.length ? (
        <p className="prompt-history-empty">搜索开始后，这里会显示首轮 prompt，以及每轮 Critic 之后的调优版本。</p>
      ) : (
        <ol className="prompt-history-list">
          {history.map((entry) => {
            const tuned = entry.tuned;
            const label = tuned
              ? "Critic / Planner 调优"
              : entry.round_index === 0
                ? "初始生成"
                : "沿用稳定基准";
            return (
              <li className="prompt-history-entry" key={`${entry.round_index}-${entry.generation_prompt_hash ?? entry.generation_prompt}`}>
                <div className="prompt-entry-heading">
                  <div>
                    <span className="prompt-round">ROUND {String(entry.round_index).padStart(2, "0")}</span>
                    <strong>{label}</strong>
                  </div>
                  <span className={`prompt-status ${tuned ? "is-tuned" : "is-initial"}`}>
                    {tuned ? "TUNED" : entry.round_index === 0 ? "INITIAL" : "BASELINE"}
                  </span>
                </div>
                {!!entry.active_directives.length && (
                  <div className="prompt-directive-list" aria-label="本轮调优指令">
                    {entry.active_directives.map((directive, index) => (
                      <span key={`${directive.directive_id ?? directive.instruction}-${index}`}>
                        {directive.category ? `${directive.category} · ` : ""}{directive.instruction}
                      </span>
                    ))}
                  </div>
                )}
                {!!entry.human_feedback && (
                  <div className="prompt-human-feedback" aria-label="本轮人工反馈">
                    <span>PHOTOGRAPHER FEEDBACK</span>
                    <p>{entry.human_feedback}</p>
                  </div>
                )}
                <PromptBlock
                  label="发送给图像模型的 generation prompt"
                  prompt={entry.generation_prompt}
                  hash={entry.generation_prompt_hash}
                  open={entry.round_index === latestRoundIndex}
                />
                <PromptBlock
                  label="稳定基准 canonical prompt"
                  prompt={entry.canonical_prompt}
                  hash={entry.canonical_prompt_hash}
                />
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
