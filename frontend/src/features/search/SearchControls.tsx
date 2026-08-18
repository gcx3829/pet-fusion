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
          <p className="eyebrow">04 / SEARCH EXPOSURE</p>
          <h2 id="search-heading">自动搜索</h2>
        </div>
        <span className={`status-stamp status-${status}`}>
          <i /> {statusLabels[status]}
        </span>
      </div>

      <label className="text-field intent-field">
        <span>画面意图</span>
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

      <fieldset className="search-settings" disabled={searchExists || isSubmitting}>
        <legend className="sr-only">自动搜索参数</legend>
        <div className="setting-row">
          <div>
            <strong>每轮候选</strong>
            <span>相同源素材独立采样</span>
          </div>
          <div className="segmented-control" aria-label="每轮候选数量">
            {[2, 3, 4].map((count) => (
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

        <label className="setting-row setting-row--range">
          <span>
            <strong>最多轮次</strong>
            <small>每轮都从原片 Rebase</small>
          </span>
          <input
            type="range"
            min="1"
            max="3"
            step="1"
            value={options.max_rounds}
            aria-valuetext={`${options.max_rounds} 轮`}
            onChange={(event) => onOptionsChange({ ...options, max_rounds: Number(event.target.value) })}
          />
          <output>{options.max_rounds}R</output>
        </label>

        <label className="setting-row budget-row">
          <span>
            <strong>成本上限</strong>
            <small>付费节点前再次校验</small>
          </span>
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
          <span>
            <strong>每轮由我审片</strong>
            <small>生成后中断，不自动进入下一轮</small>
          </span>
        </label>
      </fieldset>

      {error && (
        <div className="inline-error" role="alert">
          <Icon name="warning" />
          <span>{error}</span>
        </div>
      )}

      <button
        className="expose-button"
        type="button"
        disabled={!canStart || isSubmitting || searchExists}
        onClick={onStart}
      >
        <span className="expose-icon"><Icon name="aperture" /></span>
        <span>
          <strong>{isSubmitting ? "正在建立耐久任务…" : searchExists ? statusLabels[status] : "开始 Auto Search"}</strong>
          <small>IMMUTABLE SOURCE · BEST OF {options.candidate_count}</small>
        </span>
        <span className="expose-mark">↗</span>
      </button>
      {!canStart && !searchExists && (
        <p className="control-hint">装入一张旅行原片和至少一张宠物参考后即可开始。</p>
      )}
    </section>
  );
}
