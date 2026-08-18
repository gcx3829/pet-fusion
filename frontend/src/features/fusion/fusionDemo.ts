import type { SearchSnapshot } from "../../types";

export const FUSION_DEMO_SOURCE_URL = "/mock/fusion-source.svg";
export const FUSION_DEMO_GENERATED_URL = "/mock/fusion-generated.svg";
export const FUSION_DEMO_WIDTH = 1280;
export const FUSION_DEMO_HEIGHT = 800;
export const FUSION_DEMO_CANDIDATE_ID = "candidate-fusion-demo";

export const FUSION_DEMO_SNAPSHOT: SearchSnapshot = {
  search_id: "search-fusion-demo",
  status: "accepted",
  round_index: 0,
  candidates: [{
    candidate_id: FUSION_DEMO_CANDIDATE_ID,
    round_index: 0,
    variant_index: 0,
    image_url: FUSION_DEMO_GENERATED_URL,
    raw_image_url: FUSION_DEMO_GENERATED_URL,
    raw_asset_url: FUSION_DEMO_GENERATED_URL,
    raw_width: FUSION_DEMO_WIDTH,
    raw_height: FUSION_DEMO_HEIGHT,
    score: 92,
    is_round_winner: true,
    is_global_winner: true,
  }],
  global_winner_id: FUSION_DEMO_CANDIDATE_ID,
  global_winner_score: 92,
  prompt_history: [],
  active_directives: [],
  stop_reason: "fusion_demo",
};

export function isFusionDemoEnabled(locationSearch: string): boolean {
  return import.meta.env.DEV && new URLSearchParams(locationSearch).get("demo") === "fusion";
}

