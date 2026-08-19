import type {
  ProfessionalPromptPlan,
  PromptGenerationMode,
  PromptHistoryEntry,
  PromptRefinementEventState,
} from "../../types";

interface PromptHistoryProps {
  history: PromptHistoryEntry[];
  refinementState?: PromptRefinementEventState;
}

function shortHash(value?: string): string {
  if (!value) return "N/A";
  return `${value.slice(0, 12)}…`;
}

function refinementLabel(entry: PromptHistoryEntry): string {
  if (entry.refinement_mode === "revision" || entry.tuned) {
    if (entry.generation_mode === "candidate_anchored_rebase") return "人工选片 / Critic 修订";
    if (entry.human_feedback) return "人工反馈 / Critic 修订";
    if (entry.active_directives.length) return "Critic / Planner 修订";
    return "Prompt 修订";
  }
  return entry.professional_prompt_plan || entry.prompt_model
    ? "多模态初始理解"
    : "初始 Prompt（兼容记录）";
}

function generationLabel(mode?: PromptGenerationMode): string {
  if (mode === "candidate_anchored_rebase") return "候选视觉锚点 · 原图重基准";
  if (mode === "source_rebase") return "source-only · 原图重基准";
  return "生成模式未提供";
}

const planSections: Array<{ key: keyof ProfessionalPromptPlan; label: string }> = [
  { key: "role_of_inputs", label: "场景与输入理解" },
  { key: "task", label: "任务" },
  { key: "identity_invariants", label: "主体身份约束" },
  { key: "placement", label: "构图、动作与位置" },
  { key: "photographic_integration", label: "光线与光学融合" },
  { key: "scene_preservation", label: "场景保留约束" },
  { key: "preserve_from_anchor", label: "从所选 Raw 保留" },
  { key: "change_from_anchor", label: "相对所选 Raw 修改" },
  { key: "output", label: "输出要求" },
];

function PlanBlock({ plan }: { plan?: ProfessionalPromptPlan }) {
  if (!plan) return null;
  const visibleSections = planSections.flatMap(({ key, label }) => {
    const value = plan[key];
    const items = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
    const safeItems = items.filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
    return safeItems.length ? [{ key, label, items: safeItems }] : [];
  });
  if (!visibleSections.length && !plan.summary) return null;
  return (
    <details className="prompt-plan-block" open>
      <summary><span>多模态专业描述计划</span><code>{plan.summary ? "SUMMARY" : "STRUCTURED"}</code></summary>
      <div className="prompt-plan-grid">
        {plan.summary && <p className="prompt-plan-summary">{plan.summary}</p>}
        {visibleSections.map((section) => (
          <section key={section.key}>
            <strong>{section.label}</strong>
            {section.items.length === 1
              ? <p>{section.items[0]}</p>
              : <ul>{section.items.map((item, index) => <li key={`${section.key}-${index}`}>{item}</li>)}</ul>}
          </section>
        ))}
      </div>
    </details>
  );
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

export function PromptHistory({ history, refinementState }: PromptHistoryProps) {
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

      {refinementState && refinementState.status !== "idle" && (
        <div className={`prompt-refinement-status is-${refinementState.status}`} role="status">
          <span className="prompt-refinement-status-dot" />
          <strong>
            {refinementState.status === "started"
              ? "多模态 Prompt 正在整理"
              : refinementState.status === "ready"
                ? "本轮 Prompt 已就绪"
                : "Prompt 整理失败"}
          </strong>
          <span>{refinementState.message ?? "事件只提供处理状态；完整 Prompt 会在版本落库后显示。"}</span>
        </div>
      )}

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
              <li className="prompt-history-entry" key={`${entry.round_index}-${entry.prompt_version_id ?? entry.generation_prompt_hash ?? "legacy"}`}>
                <div className="prompt-entry-heading">
                  <div>
                    <span className="prompt-round">ROUND {String(entry.round_index).padStart(2, "0")}</span>
                    <strong>{label}</strong>
                  </div>
                  <span className={`prompt-status ${tuned ? "is-tuned" : "is-initial"}`}>
                    {entry.refinement_mode === "revision" || tuned ? "REVISION" : entry.round_index === 0 ? "INITIAL" : "BASELINE"}
                  </span>
                </div>
                <div className="prompt-lineage-meta" aria-label="Prompt 版本信息">
                  <span><b>{refinementLabel(entry)}</b></span>
                  <span>{generationLabel(entry.generation_mode)}</span>
                  {entry.prompt_model && <span>Prompt model · {entry.prompt_model}</span>}
                  {entry.generation_model && <span>Image model · {entry.generation_model}</span>}
                  {entry.prompt_schema_version && <span>Schema · {entry.prompt_schema_version}</span>}
                  {(entry.prompt_template_version ?? entry.canonical_template_version) && (
                    <span>Template · {entry.prompt_template_version ?? entry.canonical_template_version}</span>
                  )}
                </div>
                {(entry.prompt_version_id || entry.based_on_prompt_version_id || entry.human_selected_candidate_id) && (
                  <div className="prompt-version-lineage" aria-label="Prompt 继承关系">
                    {entry.prompt_version_id && <span>VERSION <code>{shortHash(entry.prompt_version_id)}</code></span>}
                    {entry.based_on_prompt_version_id && <span>BASED ON <code>{shortHash(entry.based_on_prompt_version_id)}</code></span>}
                    {entry.human_selected_candidate_id && <span>SELECTED RAW <code>{entry.human_selected_candidate_id}</code></span>}
                  </div>
                )}
                {entry.visual_anchor && (
                  <div className="prompt-visual-anchor" aria-label="本轮视觉锚点">
                    {entry.visual_anchor.raw_asset_url && (
                      <img src={entry.visual_anchor.raw_asset_url} alt="所选 Raw 视觉锚点" />
                    )}
                    <div>
                      <strong>所选 Raw 作为视觉参考</strong>
                      <p>
                        {entry.visual_anchor.candidate_id ?? entry.human_selected_candidate_id ?? "未提供候选 ID"}
                        {typeof entry.visual_anchor.round_index === "number" ? ` · Round ${entry.visual_anchor.round_index}` : ""}
                      </p>
                      <small>原图仍是编辑底片；这一张只用于保持上一轮成功的视觉特征。</small>
                    </div>
                  </div>
                )}
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
                <PlanBlock plan={entry.professional_prompt_plan} />
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
