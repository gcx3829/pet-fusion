import { PromptHistory } from "./PromptHistory";
import { SearchControls } from "./SearchControls";
import type {
  PromptHistoryEntry,
  PromptRefinementEventState,
  SearchOptions,
  SearchStatusValue,
} from "../../types";

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
  refinementState?: PromptRefinementEventState;
}

/** Prompt tab composition. SearchControls remains the single action owner. */
export function PromptInspector({ history, refinementState, ...searchProps }: PromptInspectorProps) {
  return (
    <div className="prompt-inspector" id="sidebar-panel-prompt" role="tabpanel" aria-label="生成设置">
      <SearchControls {...searchProps} />
      <PromptHistory history={history} refinementState={refinementState} />
    </div>
  );
}
