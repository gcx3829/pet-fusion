import { Icon } from "../../components/Icon";
import type { SearchOptions, SearchStatusValue } from "../../types";

interface SearchControlsProps {
  userIntent: string;
  onUserIntentChange: (value: string) => void;
  options: SearchOptions;
  onOptionsChange: (value: SearchOptions) => void;
  canStart: boolean;
  isSubmitting: boolean;
  status: SearchStatusValue;
  error?: string | null;
  onStart: () => void;
}

const statusLabels: Record<SearchStatusValue, string> = {
  idle: "等待素材",
  queued: "已进入队列",
  running: "正在冲印",
  waiting_for_human: "等待审片",
  accepted: "已接受",
  completed: "已完成",
  failed: "搜索失败",
  cancelled: "已取消",
};

export function SearchControls({
  userIntent,
  onUserIntentChange,
  options,
  onOptionsChange,
  canStart,
  isSubmitting,
  status,
  error,
  onStart,
}: SearchControlsProps) {
  const searchExists = status !== "idle";
  return (
    <section className="panel search-controls" aria-labelledby="search-heading">
      <div className="panel-heading">
        <div>
          <h2 id="search-heading">生成</h2>
        </div>
        <span className={`status-stamp status-${status}`}>
          <i /> {statusLabels[status]}
        </span>
      </div>

      <label className="text-field intent-field">
        <span>要求</span>
        <textarea
          rows={3}
          maxLength={700}
          value={userIntent}
          disabled={searchExists || isSubmitting}
          placeholder="让宠物自然地坐在这里，像旅行时一起拍到的照片。"
          onChange={(event) => onUserIntentChange(event.target.value)}
        />
        <small>{userIntent.length} / 700</small>
      </label>

      <fieldset className="search-settings search-settings--primary" disabled={searchExists || isSubmitting}>
        <legend className="sr-only">自动搜索参数</legend>
        <div className="setting-row">
          <div>
            <strong>每轮候选</strong>
          </div>
          <div className="segmented-control" aria-label="每轮候选数量">
            {[1, 2, 3].map((count) => (
              <button
                key={count}
                type="button"
                className={options.candidate_count === count ? "is-active" : ""}
                aria-pressed={options.candidate_count === count}
                onClick={() => onOptionsChange({ ...options, candidate_count: count })}
              >
                {count}
              </button>
            ))}
          </div>
        </div>

      </fieldset>

      <details className="search-advanced">
        <summary><span>高级设置</span><small>{options.max_rounds} 轮 · ${options.budget_usd}</small></summary>
        <fieldset className="search-settings" disabled={searchExists || isSubmitting}>
          <legend className="sr-only">高级搜索参数</legend>
          <label className="setting-row setting-row--range">
            <strong>最多轮次</strong>
            <input
              type="range"
              min="1"
              max="3"
              step="1"
              value={options.max_rounds}
              aria-valuetext={`${options.max_rounds} 轮`}
              onChange={(event) => onOptionsChange({ ...options, max_rounds: Number(event.target.value) })}
            />
            <output>{options.max_rounds} 轮</output>
          </label>

          <label className="setting-row budget-row">
            <strong>成本上限</strong>
            <span className="budget-input">
              <b>$</b>
              <input
                type="number"
                min="0.1"
                max="50"
                step="0.1"
                value={options.budget_usd}
                onChange={(event) => onOptionsChange({
                  ...options,
                  budget_usd: Math.max(0.1, Number(event.target.value) || 0.1),
                })}
              />
            </span>
          </label>

          <label className="check-setting">
            <input
              type="checkbox"
              checked={options.review_each_round}
              onChange={(event) => onOptionsChange({ ...options, review_each_round: event.target.checked })}
            />
            <span className="custom-check"><Icon name="check" /></span>
            <strong>每轮审片</strong>
          </label>
        </fieldset>
      </details>

      {error && (
        <div className="inline-error" role="alert">
          <Icon name="warning" />
          <span>{error}</span>
        </div>
      )}

      <button
        className="primary-button search-run-button"
        type="button"
        disabled={!canStart || isSubmitting || searchExists}
        onClick={onStart}
      >
        <Icon name="spark" />
        <strong>{isSubmitting ? "正在开始…" : searchExists ? statusLabels[status] : `生成 ${options.candidate_count} 张`}</strong>
      </button>
      {!canStart && !searchExists && (
        <p className="control-hint">需要 1 张底片和至少 1 张参考图。</p>
      )}
    </section>
  );
}
