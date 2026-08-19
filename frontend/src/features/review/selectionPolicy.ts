import type { ResumeAction } from "../../types";

/**
 * A continued round still belongs to the candidate the photographer reviewed.
 * Keep that selection visible until the snapshot advances to the next round;
 * otherwise the worker briefly falls back to Global Winner and misrepresents
 * the visual anchor actually sent to the backend.
 */
export function shouldClearCandidateAfterResume(action: ResumeAction): boolean {
  return action !== "continue_one_round";
}
