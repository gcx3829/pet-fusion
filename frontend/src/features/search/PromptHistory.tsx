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
    if (entry.generation_mode === "candidate_anchored_rebase") return "根据所选候选调整";
    if (entry.human_feedback) return "根据修改意见调整";
    if (entry.active_directives.length) return "根据自动检查调整";
    return "已调整画面描述";
  }
  return entry.professional_prompt_plan || entry.prompt_model
    ? "初始图像分析"
    : "初始画面描述";
}

function generationLabel(mode?: PromptGenerationMode): string {
  if (mode === "candidate_anchored_rebase") return "从原片生成，并参考所选候选";
  if (mode === "source_rebase") return "只使用原片和宠物参考图";
  return "未记录生成方式";
}

const planSections: Array<{ key: keyof ProfessionalPromptPlan; label: string }> = [
  { key: "role_of_inputs", label: "场景与输入理解" },
  { key: "task", label: "任务" },
  { key: "identity_invariants", label: "主体身份约束" },
  { key: "pet_identity_observations", label: "宠物身份特征" },
  { key: "background_observations", label: "背景观察" },
  { key: "placement", label: "构图、动作与位置" },
  { key: "capture_geometry", label: "机位与透视" },
  { key: "lighting_analysis", label: "光线" },
  { key: "color_analysis", label: "色彩" },
  { key: "optics_and_depth_analysis", label: "镜头与景深" },
  { key: "texture_and_noise_analysis", label: "锐化与噪点" },
  { key: "physical_integration", label: "接触、阴影与遮挡" },
  { key: "photographic_integration", label: "光线与光学融合" },
  { key: "scene_preservation", label: "场景保留约束" },
  { key: "uncertainties", label: "无法确认的信息" },
  { key: "preserve_from_anchor", label: "从所选候选保留" },
  { key: "change_from_anchor", label: "相对所选候选修改" },
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
    <details className="prompt-plan-block">
      <summary><span>图像分析</span></summary>
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
          <h2 id="prompt-history-heading">生成记录</h2>
        </div>
        <span className="prompt-history-count">
          {history.length ? `${history.length} 轮` : "暂无记录"}
        </span>
      </div>

      {refinementState && refinementState.status !== "idle" && (
        <div className={`prompt-refinement-status is-${refinementState.status}`} role="status">
          <span className="prompt-refinement-status-dot" />
          <strong>
            {refinementState.status === "started"
              ? "正在分析图片"
              : refinementState.status === "ready"
                ? "画面描述已准备好"
                : "图片分析失败"}
          </strong>
          <span>{refinementState.message ?? "完成后会在下方显示实际发送内容。"}</span>
        </div>
      )}

      {!history.length ? (
        <p className="prompt-history-empty">开始生成后，这里会显示图像分析和实际发送给模型的内容。</p>
      ) : (
        <ol className="prompt-history-list">
          {history.map((entry) => {
            const tuned = entry.tuned;
            const label = tuned
              ? "已根据检查结果调整"
              : entry.round_index === 0
                ? "初始生成"
                : "沿用稳定基准";
            return (
              <li className="prompt-history-entry" key={`${entry.round_index}-${entry.prompt_version_id ?? entry.generation_prompt_hash ?? "legacy"}`}>
                <div className="prompt-entry-heading">
                  <div>
                    <span className="prompt-round">第 {entry.round_index + 1} 轮</span>
                    <strong>{label}</strong>
                  </div>
                  <span className={`prompt-status ${tuned ? "is-tuned" : "is-initial"}`}>
                    {entry.refinement_mode === "revision" || tuned ? "已调整" : entry.round_index === 0 ? "初始" : "沿用"}
                  </span>
                </div>
                <div className="prompt-lineage-meta" aria-label="生成记录概览">
                  <span><b>{refinementLabel(entry)}</b></span>
                  <span>{generationLabel(entry.generation_mode)}</span>
                </div>
                {(entry.prompt_version_id || entry.based_on_prompt_version_id || entry.human_selected_candidate_id) && (
                  <details className="prompt-version-lineage" aria-label="技术信息">
                    <summary>技术信息</summary>
                    {entry.prompt_version_id && <span>版本 <code>{shortHash(entry.prompt_version_id)}</code></span>}
                    {entry.based_on_prompt_version_id && <span>基于 <code>{shortHash(entry.based_on_prompt_version_id)}</code></span>}
                    {entry.human_selected_candidate_id && <span>所选候选 <code>{entry.human_selected_candidate_id}</code></span>}
                    {entry.prompt_model && <span>分析模型 · {entry.prompt_model}</span>}
                    {entry.generation_model && <span>图像模型 · {entry.generation_model}</span>}
                    {entry.prompt_schema_version && <span>数据格式 · {entry.prompt_schema_version}</span>}
                    {(entry.prompt_template_version ?? entry.canonical_template_version) && <span>模板 · {entry.prompt_template_version ?? entry.canonical_template_version}</span>}
                  </details>
                )}
                {entry.visual_anchor && (
                  <div className="prompt-visual-anchor" aria-label="本轮参考候选">
                    {entry.visual_anchor.raw_asset_url && (
                      <img src={entry.visual_anchor.raw_asset_url} alt="所选参考候选" />
                    )}
                    <div>
                      <strong>参考所选候选</strong>
                      <p>
                        {entry.visual_anchor.candidate_id ?? entry.human_selected_candidate_id ?? "未提供候选 ID"}
                        {typeof entry.visual_anchor.round_index === "number" ? ` · 第 ${entry.visual_anchor.round_index + 1} 轮` : ""}
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
                    <span>修改意见</span>
                    <p>{entry.human_feedback}</p>
                  </div>
                )}
                <PlanBlock plan={entry.professional_prompt_plan} />
                <PromptBlock
                  label="实际发送给图像模型"
                  prompt={entry.generation_prompt}
                  hash={entry.generation_prompt_hash}
                  open={entry.round_index === latestRoundIndex}
                />
                <PromptBlock
                  label="基础画面描述"
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
