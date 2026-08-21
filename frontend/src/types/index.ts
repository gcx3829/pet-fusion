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
  /** Browser-local project library; only assigned roles are uploaded. */
  assets?: File[];
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

export interface CropPixelBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface CropPadding {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/**
 * Mapping emitted by the backend for a model-sized candidate.  The model
 * canvas can contain padding around the actual crop; Fusion must remove that
 * padding and place the crop back into this full-resolution box.
 */
export interface CropMapping {
  schema_version?: "crop-mapping/v1";
  full_width: number;
  full_height: number;
  crop_box: CropPixelBox;
  canvas_width: number;
  canvas_height: number;
  padding: CropPadding;
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

export interface GuidanceMaskRegistration {
  project_id: string;
  source_manifest_hash: string;
  asset: AssetRef;
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
  /** Raw provider output; authoritative for Critic and human review. */
  raw_image_url?: string;
  raw_asset_id?: string;
  raw_asset_url?: string;
  raw_width?: number;
  raw_height?: number;
  crop_mapping?: CropMapping;
  /** Legacy diagnostic only; never used as ``image_url``. */
  protected_asset_id?: string;
  protected_asset_url?: string;
  review_asset_kind?: "raw";
  score?: number;
  evaluation?: CandidateEvaluation;
  is_round_winner: boolean;
  is_global_winner: boolean;
  generation_depth?: number;
  model?: string;
  quality?: string;
}

export interface FusionBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface FusionMaskRegistration {
  search_id: string;
  source_manifest_hash: string;
  asset: AssetRef;
}

export interface FusionResult {
  fusion_key: string;
  search_id: string;
  candidate_id: string;
  source_manifest_hash: string;
  raw_asset: AssetRef;
  fusion_asset: AssetRef;
  mask_asset: AssetRef;
  input_mask_asset?: AssetRef;
  feather_radius_px: number;
  box?: FusionBox;
  crop_mapping?: Record<string, unknown>;
}

export interface ActiveDirective {
  directive_id?: string;
  category?: string;
  instruction: string;
}

/** User-visible sections returned by the multimodal prompt refiner.
 *
 * This is intentionally a narrow projection of the backend contract. It does
 * not include provider reasoning, raw request payloads, or audit metadata.
 */
export interface ProfessionalPromptPlan {
  role_of_inputs?: string[];
  task?: string;
  identity_invariants?: string[];
  pet_identity_observations?: string[];
  background_observations?: string[];
  placement?: string[];
  capture_geometry?: string[];
  lighting_analysis?: string[];
  color_analysis?: string[];
  optics_and_depth_analysis?: string[];
  texture_and_noise_analysis?: string[];
  physical_integration?: string[];
  photographic_integration?: string[];
  scene_preservation?: string[];
  uncertainties?: string[];
  output?: string;
  preserve_from_anchor?: string[];
  change_from_anchor?: string[];
  summary?: string;
}

export interface PromptVisualAnchor {
  schema_version?: string;
  kind?: string;
  search_id?: string;
  candidate_id?: string;
  round_index?: number;
  source_manifest_hash?: string;
  raw_asset?: AssetRef;
  raw_asset_id?: string;
  raw_asset_sha256?: string;
  raw_asset_url?: string;
}

export type PromptRefinementMode = "initial" | "revision" | "unknown";
export type PromptGenerationMode =
  | "source_rebase"
  | "candidate_anchored_rebase"
  | "unknown";

export interface PromptHistoryEntry {
  round_index: number;
  canonical_prompt: string;
  canonical_prompt_hash?: string;
  generation_prompt: string;
  generation_prompt_hash?: string;
  prompt_version_id?: string;
  prompt_version_hash?: string;
  /** Compatibility alias accepted from older API adapters. */
  version_hash?: string;
  based_on_prompt_version_id?: string;
  /** Compatibility alias accepted from a parent/lineage-shaped response. */
  parent_prompt_version_id?: string;
  refinement_mode?: PromptRefinementMode;
  generation_mode?: PromptGenerationMode;
  prompt_model?: string;
  generation_model?: string;
  prompt_schema_version?: string;
  prompt_template_version?: string;
  canonical_template_version?: string;
  active_directives: ActiveDirective[];
  active_directives_hash?: string;
  professional_prompt_plan?: ProfessionalPromptPlan;
  prompt_summary?: string;
  visual_anchor?: PromptVisualAnchor;
  prompt_request_key?: string;
  source_manifest_hash?: string;
  human_feedback?: string;
  human_selected_candidate_id?: string;
  tuned: boolean;
}

export type PromptRefinementStatus = "idle" | "started" | "ready" | "failed";

export interface PromptRefinementEventState {
  status: PromptRefinementStatus;
  roundIndex?: number;
  mode?: PromptRefinementMode;
  generationMode?: PromptGenerationMode;
  requestKey?: string;
  message?: string;
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
  prompt_history: PromptHistoryEntry[];
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
  | "accept_candidate"
  | "continue_one_round"
  | "cancel";
