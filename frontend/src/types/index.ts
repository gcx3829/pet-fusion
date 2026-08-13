export type SearchStatusValue =
  | "idle"
  | "queued"
  | "running"
  | "waiting_for_human"
  | "accepted"
  | "completed"
  | "failed"
  | "cancelled";

export type Pose = "sitting" | "standing" | "lying" | "walking";
export type Facing =
  | "camera"
  | "slightly_left"
  | "slightly_right"
  | "left"
  | "right"
  | "away";

export interface PlacementIntent {
  x: number;
  y: number;
  width: number;
  height: number;
  coordinate_space: "normalized";
  pose: Pose;
  facing: Facing;
  contact_surface: string | null;
}

export interface SourceDraft {
  background: File | null;
  references: File[];
  catName: string;
  catTraits: string;
}

export interface AssetRef {
  asset_id: string;
  sha256?: string;
  mime_type?: string;
  width?: number;
  height?: number;
  content_url?: string;
  url?: string;
  asset_url?: string;
}

export interface SourceManifest {
  manifest_hash?: string;
  background?: AssetRef;
  cat_references?: AssetRef[];
}

export interface ProjectRecord {
  project_id: string;
  source_manifest?: SourceManifest;
}

export interface SearchOptions {
  candidate_count: number;
  max_rounds: number;
  budget_usd: number;
  review_each_round: boolean;
}

export interface SearchRecord {
  search_id: string;
  thread_id?: string;
  status: SearchStatusValue;
  events_url?: string;
}

export type IssueSeverity = "blocking" | "warning" | "info";

export interface CriticIssue {
  issue_id?: string;
  category: string;
  severity: IssueSeverity;
  evidence: string;
  suggested_fix?: string | null;
}

export interface DimensionScores {
  cat_identity?: number;
  pose_geometry?: number;
  perspective_scale?: number;
  lighting_color?: number;
  optical_consistency?: number;
  physical_integration?: number;
  scene_preservation?: number;
  overall_photographic_naturalness?: number;
}

export interface CandidateEvaluation {
  total_score?: number;
  scores?: DimensionScores;
  issues: CriticIssue[];
  summary?: string;
  no_meaningful_defect?: boolean;
}

export interface SearchCandidate {
  candidate_id: string;
  round_index: number;
  variant_index: number;
  image_url: string;
  raw_image_url?: string;
  score?: number;
  evaluation?: CandidateEvaluation;
  is_round_winner: boolean;
  is_global_winner: boolean;
  generation_depth?: number;
  model?: string;
  quality?: string;
}

export interface ActiveDirective {
  directive_id?: string;
  category?: string;
  instruction: string;
}

export interface InterruptPayload {
  type?: string;
  global_winner_id?: string;
  global_winner_score?: number;
  blocking_issues?: string[];
  allowed_actions?: string[];
}

export interface SearchSnapshot extends SearchRecord {
  round_index: number;
  candidates: SearchCandidate[];
  global_winner_id?: string | null;
  global_winner_score?: number | null;
  active_directives: ActiveDirective[];
  stop_reason?: string | null;
  estimated_cost_usd?: number | null;
  interrupt_payload?: InterruptPayload | null;
}

export interface SearchEvent {
  id: string;
  type: string;
  created_at?: string;
  data: Record<string, unknown>;
}

export type ResumeAction =
  | "accept_global_winner"
  | "continue_one_round"
  | "cancel";
