import { PromptHistory } from "./PromptHistory";
import { SearchControls } from "./SearchControls";
import type { PromptHistoryEntry, SearchOptions, SearchStatusValue } from "../../types";

interface PromptInspectorProps {
  userIntent: string;
  onUserIntentChange: (value: string) => void;
  options: SearchOptions;
  onOptionsChange: (value: SearchOptions) => void;
  canStart: boolean;
  isSubmitting: boolean;
  status: SearchStatusValue;
  error?: string | null;
  onStart: () => void;
  history: PromptHistoryEntry[];
}

/** Prompt tab composition. SearchControls remains the single action owner. */
export function PromptInspector({ history, ...searchProps }: PromptInspectorProps) {
  return (
    <div className="prompt-inspector" id="sidebar-panel-prompt" role="tabpanel" aria-label="Prompt 摄影师意图">
      <div className="inspector-title"><p className="workbench-kicker">INTENT / SEARCH</p><h2>Prompt</h2><p>摄影师意图、生成预算与每轮 rebase 参数。</p></div>
      <SearchControls {...searchProps} />
      <PromptHistory history={history} />
    </div>
  );
}
