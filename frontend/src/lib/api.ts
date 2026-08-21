import type {
  ActiveDirective,
  AssetRef,
  CandidateEvaluation,
  FusionBox,
  FusionMaskRegistration,
  FusionResult,
  GuidanceMaskRegistration,
  CriticIssue,
  ProjectRecord,
  ResumeAction,
  SearchCandidate,
  SearchOptions,
  SearchRecord,
  SearchSnapshot,
  SearchStatusValue,
  SourceDraft,
  SourceManifest,
  PlacementIntent,
  PromptHistoryEntry,
  ProfessionalPromptPlan,
  PromptVisualAnchor,
  PromptRefinementMode,
  PromptGenerationMode,
  CropMapping,
} from "../types";
import { MAX_UPLOAD_BYTES, prepareImageForUpload } from "./files";

type JsonObject = Record<string, unknown>;

const configuredBase = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export const API_ROOT = configuredBase.endsWith("/api/v1")
  ? configuredBase
  : `${configuredBase}/api/v1`;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function unwrap(payload: unknown): unknown {
  return isObject(payload) && "data" in payload ? payload.data : payload;
}

function asObject(payload: unknown): JsonObject {
  const value = unwrap(payload);
  if (!isObject(value)) {
    throw new Error("服务器返回了无法识别的数据格式");
  }
  return value;
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function boundedString(value: unknown, maxLength: number, fallback = ""): string {
  const string = stringValue(value, fallback);
  return string ? string.slice(0, maxLength) : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function normalizeCropMapping(value: unknown): CropMapping | undefined {
  if (!isObject(value) || !isObject(value.crop_box) || !isObject(value.padding)) {
    return undefined;
  }
  const cropBox = value.crop_box;
  const padding = value.padding;
  const mapping: CropMapping = {
    schema_version: value.schema_version === "crop-mapping/v1"
      ? "crop-mapping/v1"
      : undefined,
    full_width: numberValue(value.full_width),
    full_height: numberValue(value.full_height),
    crop_box: {
      x: numberValue(cropBox.x),
      y: numberValue(cropBox.y),
      width: numberValue(cropBox.width),
      height: numberValue(cropBox.height),
    },
    canvas_width: numberValue(value.canvas_width),
    canvas_height: numberValue(value.canvas_height),
    padding: {
      left: numberValue(padding.left),
      top: numberValue(padding.top),
      right: numberValue(padding.right),
      bottom: numberValue(padding.bottom),
    },
  };
  const values = [
    mapping.full_width,
    mapping.full_height,
    mapping.crop_box.x,
    mapping.crop_box.y,
    mapping.crop_box.width,
    mapping.crop_box.height,
    mapping.canvas_width,
    mapping.canvas_height,
    mapping.padding.left,
    mapping.padding.top,
    mapping.padding.right,
    mapping.padding.bottom,
  ];
  if (
    values.some((item) => !Number.isInteger(item) || item < 0)
    || mapping.full_width <= 0
    || mapping.full_height <= 0
    || mapping.crop_box.width <= 0
    || mapping.crop_box.height <= 0
    || mapping.canvas_width <= 0
    || mapping.canvas_height <= 0
    || mapping.crop_box.x + mapping.crop_box.width > mapping.full_width
    || mapping.crop_box.y + mapping.crop_box.height > mapping.full_height
    || mapping.padding.left + mapping.padding.right >= mapping.canvas_width
    || mapping.padding.top + mapping.padding.bottom >= mapping.canvas_height
  ) {
    return undefined;
  }
  return mapping;
}

function resolveApiUrl(pathOrUrl: string): string {
  if (/^(https?:|blob:|data:)/.test(pathOrUrl)) return pathOrUrl;
  if (!configuredBase || !configuredBase.startsWith("http")) return pathOrUrl;
  try {
    return new URL(pathOrUrl, configuredBase).toString();
  } catch {
    return pathOrUrl;
  }
}

export function assetContentUrl(assetId: string): string {
  return `${API_ROOT}/assets/${encodeURIComponent(assetId)}`;
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(`${API_ROOT}${path}`, init);
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const nestedError = isObject(payload) && isObject(payload.error) ? payload.error : undefined;
    const nestedDetails = Array.isArray(nestedError?.details) ? nestedError.details : [];
    const firstDetail = nestedDetails.find(isObject);
    const detailSuffix = firstDetail
      ? [stringValue(firstDetail.field), stringValue(firstDetail.message)]
          .filter(Boolean)
          .join(": ")
      : "";
    const detail = isObject(payload)
      ? stringValue(
          payload.detail,
          stringValue(payload.message, stringValue(nestedError?.message)),
        )
      : String(payload);
    const message = detailSuffix && detail
      ? `${detail}（${detailSuffix}）`
      : detailSuffix || detail;
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return payload;
}

function normalizeAsset(value: unknown): AssetRef | undefined {
  if (!isObject(value)) return undefined;
  const assetId = stringValue(value.asset_id, stringValue(value.id));
  if (!assetId) return undefined;
  return {
    asset_id: assetId,
    sha256: stringValue(value.sha256) || undefined,
    mime_type: stringValue(value.mime_type) || undefined,
    width: numberValue(value.width) || undefined,
    height: numberValue(value.height) || undefined,
    content_url: stringValue(value.content_url)
      ? resolveApiUrl(stringValue(value.content_url))
      : undefined,
    url: stringValue(value.url) ? resolveApiUrl(stringValue(value.url)) : undefined,
    asset_url: stringValue(value.asset_url)
      ? resolveApiUrl(stringValue(value.asset_url))
      : undefined,
  };
}

function normalizeManifest(value: unknown): SourceManifest | undefined {
  if (!isObject(value)) return undefined;
  const refs = Array.isArray(value.cat_references)
    ? value.cat_references.map(normalizeAsset).filter(Boolean) as AssetRef[]
    : [];
  return {
    manifest_hash: stringValue(value.manifest_hash) || undefined,
    background: normalizeAsset(value.background),
    cat_references: refs,
  };
}

function normalizeStatus(value: unknown): SearchStatusValue {
  const status = stringValue(value, "queued") as SearchStatusValue;
  const allowed: SearchStatusValue[] = [
    "idle",
    "queued",
    "running",
    "waiting_for_human",
    "accepted",
    "completed",
    "failed",
    "cancelled",
  ];
  return allowed.includes(status) ? status : "running";
}

function normalizeIssues(value: unknown): CriticIssue[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): CriticIssue[] => {
    if (!isObject(item)) return [];
    const severity = stringValue(item.severity, "info");
    return [{
      issue_id: stringValue(item.issue_id) || undefined,
      category: stringValue(item.category, "photographic_naturalness"),
      severity: severity === "blocking" || severity === "warning" ? severity : "info",
      evidence: stringValue(item.evidence, stringValue(item.description, "未提供说明")),
      suggested_fix: stringValue(item.suggested_fix) || undefined,
    }];
  });
}

function normalizeEvaluation(value: unknown): CandidateEvaluation | undefined {
  if (!isObject(value)) return undefined;
  const scoreObject = isObject(value.scores) ? value.scores : undefined;
  return {
    total_score: numberValue(value.total_score, numberValue(value.score)) || undefined,
    scores: scoreObject ? {
      cat_identity: numberValue(scoreObject.cat_identity) || undefined,
      pose_geometry: numberValue(scoreObject.pose_geometry) || undefined,
      perspective_scale: numberValue(scoreObject.perspective_scale) || undefined,
      lighting_color: numberValue(scoreObject.lighting_color) || undefined,
      optical_consistency: numberValue(scoreObject.optical_consistency) || undefined,
      physical_integration: numberValue(scoreObject.physical_integration) || undefined,
      scene_preservation: numberValue(scoreObject.scene_preservation) || undefined,
      overall_photographic_naturalness:
        numberValue(scoreObject.overall_photographic_naturalness) || undefined,
    } : undefined,
    issues: normalizeIssues(value.issues),
    summary: stringValue(value.summary) || undefined,
    no_meaningful_defect: typeof value.no_meaningful_defect === "boolean"
      ? value.no_meaningful_defect
      : undefined,
  };
}

function imageUrlFromCandidate(candidate: JsonObject): string {
  // Raw is the Search/Critic/human-review authority.  Prefer explicit raw
  // fields even when a legacy response also contains protected fields.
  const rawDirect = stringValue(
    candidate.raw_asset_url,
    stringValue(candidate.raw_image_url),
  );
  if (rawDirect) return resolveApiUrl(rawDirect);
  const rawAsset = normalizeAsset(candidate.raw_asset);
  if (rawAsset) {
    const suppliedUrl = rawAsset.asset_url ?? rawAsset.content_url ?? rawAsset.url;
    return suppliedUrl ? resolveApiUrl(suppliedUrl) : assetContentUrl(rawAsset.asset_id);
  }
  const rawAssetId = stringValue(candidate.raw_asset_id);
  if (rawAssetId) return assetContentUrl(rawAssetId);

  // v2's generic fields are raw aliases.  They are checked before any
  // protected-only compatibility fields from v1.
  const direct = stringValue(candidate.image_url, stringValue(candidate.asset_url));
  if (direct) return resolveApiUrl(direct);
  const protectedDirect = stringValue(
    candidate.protected_asset_url,
    stringValue(candidate.protected_image_url),
  );
  if (protectedDirect) return resolveApiUrl(protectedDirect);
  const protectedAsset = normalizeAsset(candidate.protected_asset);
  if (protectedAsset) {
    const suppliedUrl = protectedAsset.asset_url ?? protectedAsset.content_url ?? protectedAsset.url;
    return suppliedUrl ? resolveApiUrl(suppliedUrl) : assetContentUrl(protectedAsset.asset_id);
  }
  const asset = normalizeAsset(candidate.asset);
  if (asset) {
    const suppliedUrl = asset.asset_url ?? asset.content_url ?? asset.url;
    return suppliedUrl ? resolveApiUrl(suppliedUrl) : assetContentUrl(asset.asset_id);
  }
  const assetId = stringValue(candidate.protected_asset_id, stringValue(candidate.asset_id));
  return assetId ? assetContentUrl(assetId) : "";
}

function normalizeCandidate(
  value: unknown,
  globalWinnerId?: string | null,
  roundWinnerId?: string | null,
  evaluations?: JsonObject,
): SearchCandidate | undefined {
  if (!isObject(value)) return undefined;
  const id = stringValue(value.candidate_id, stringValue(value.id));
  if (!id) return undefined;
  const linkedEvaluation = normalizeEvaluation(value.evaluation ?? evaluations?.[id]);
  const directScore = numberValue(value.total_score, numberValue(value.score));
  const rawImageUrl = imageUrlFromCandidate(value);
  const rawAsset = normalizeAsset(value.raw_asset);
  const protectedAsset = normalizeAsset(value.protected_asset);
  return {
    candidate_id: id,
    round_index: numberValue(value.round_index),
    variant_index: numberValue(value.variant_index),
    image_url: rawImageUrl,
    raw_image_url: rawImageUrl || undefined,
    raw_asset_id: stringValue(value.raw_asset_id) || rawAsset?.asset_id,
    raw_asset_url: stringValue(
      value.raw_asset_url,
      stringValue(value.raw_image_url),
    ) || rawAsset?.asset_url || rawAsset?.content_url || rawAsset?.url,
    raw_width: numberValue(value.raw_width, numberValue(rawAsset?.width, numberValue(value.width))) || undefined,
    raw_height: numberValue(value.raw_height, numberValue(rawAsset?.height, numberValue(value.height))) || undefined,
    crop_mapping: normalizeCropMapping(value.crop_mapping),
    protected_asset_id: stringValue(value.protected_asset_id) || protectedAsset?.asset_id,
    protected_asset_url: stringValue(
      value.protected_asset_url,
      stringValue(value.protected_image_url),
    ) || protectedAsset?.asset_url || protectedAsset?.content_url || protectedAsset?.url,
    review_asset_kind: "raw",
    score: directScore || linkedEvaluation?.total_score,
    evaluation: linkedEvaluation,
    is_round_winner: value.is_round_winner === true || id === roundWinnerId,
    is_global_winner: value.is_global_winner === true || id === globalWinnerId,
    generation_depth: numberValue(value.generation_depth) || undefined,
    model: stringValue(value.model) || undefined,
    quality: stringValue(value.quality) || undefined,
  };
}

function normalizeDirectives(value: unknown): ActiveDirective[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((directive): ActiveDirective[] => {
    if (typeof directive === "string") {
      const instruction = directive.trim().slice(0, 600);
      return instruction ? [{ instruction }] : [];
    }
    if (!isObject(directive)) return [];
    const instruction = boundedString(directive.instruction, 600).trim();
    return instruction ? [{
      directive_id: boundedString(directive.directive_id, 160) || undefined,
      category: boundedString(directive.category, 120) || undefined,
      instruction,
    }] : [];
  }).slice(0, 3);
}

function normalizeSafeStringList(value: unknown, maxItems = 32, maxLength = 600): string[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, maxItems).flatMap((item): string[] => {
    if (typeof item !== "string") return [];
    const text = item.trim().slice(0, maxLength);
    return text ? [text] : [];
  });
}

function normalizeStringList(value: unknown, maxItems = 24, maxItemLength = 600): string[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, maxItems).flatMap((item): string[] => {
    if (typeof item === "string") {
      const text = item.trim().slice(0, maxItemLength);
      return text ? [text] : [];
    }
    // Some compatible adapters wrap a visible clause as { text } or
    // { instruction }. Never stringify an arbitrary object: that would leak
    // ``[object Object]`` into the Prompt Inspector.
    if (isObject(item)) {
      const text = boundedString(item.text, maxItemLength)
        || boundedString(item.instruction, maxItemLength)
        || boundedString(item.value, maxItemLength);
      return text.trim() ? [text.trim()] : [];
    }
    return [];
  });
}

const promptPlanSections = [
  "role_of_inputs",
  "identity_invariants",
  "pet_identity_observations",
  "background_observations",
  "placement",
  "capture_geometry",
  "lighting_analysis",
  "color_analysis",
  "optics_and_depth_analysis",
  "texture_and_noise_analysis",
  "physical_integration",
  "photographic_integration",
  "scene_preservation",
  "uncertainties",
  "preserve_from_anchor",
  "change_from_anchor",
] as const;

function normalizePromptPlan(value: unknown): ProfessionalPromptPlan | undefined {
  if (!isObject(value)) return undefined;
  const plan: ProfessionalPromptPlan = {};
  for (const section of promptPlanSections) {
    const clauses = normalizeStringList(value[section]);
    if (clauses.length) plan[section] = clauses;
  }
  const task = boundedString(value.task, 2_000).trim();
  const output = boundedString(value.output, 1_000).trim();
  const summary = boundedString(value.summary, 600).trim();
  if (task) plan.task = task;
  if (output) plan.output = output;
  if (summary) plan.summary = summary;
  return Object.keys(plan).length ? plan : undefined;
}

function normalizePromptAnchor(value: unknown, parent: JsonObject): PromptVisualAnchor | undefined {
  const nested = isObject(value) ? value : {};
  const rawAssetValue = nested.raw_asset ?? parent.visual_anchor_asset;
  const rawAsset = normalizeAsset(rawAssetValue);
  const rawAssetId = boundedString(
    nested.raw_asset_id,
    160,
    rawAsset?.asset_id ?? boundedString(parent.visual_anchor_raw_asset_id, 160),
  );
  const candidateId = boundedString(
    nested.candidate_id,
    160,
    boundedString(parent.visual_anchor_candidate_id, 160),
  );
  const suppliedRawAssetUrl = boundedString(
    nested.raw_asset_url,
    2_048,
    rawAsset?.asset_url ?? rawAsset?.content_url ?? rawAsset?.url
      ?? boundedString(parent.visual_anchor_raw_asset_url, 2_048),
  );
  const rawAssetUrl = /^(https?:|blob:|\/)/i.test(suppliedRawAssetUrl)
    ? suppliedRawAssetUrl
    : rawAssetId
      ? assetContentUrl(rawAssetId)
      : "";
  const roundValue = nested.round_index ?? parent.visual_anchor_round_index;
  const roundIndex = typeof roundValue === "number" && Number.isInteger(roundValue) && roundValue >= 0
    ? roundValue
    : undefined;
  if (!candidateId && !rawAssetId && !rawAssetUrl) return undefined;
  return {
    schema_version: boundedString(nested.schema_version, 120) || undefined,
    kind: boundedString(nested.kind, 120) || undefined,
    search_id: boundedString(nested.search_id, 160) || undefined,
    candidate_id: candidateId || undefined,
    round_index: roundIndex,
    source_manifest_hash: boundedString(nested.source_manifest_hash, 128) || undefined,
    raw_asset: rawAsset,
    raw_asset_id: rawAssetId || undefined,
    raw_asset_sha256: boundedString(nested.raw_asset_sha256, 128) || undefined,
    raw_asset_url: rawAssetUrl || undefined,
  };
}

export function normalizePromptHistory(value: unknown): PromptHistoryEntry[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 64).flatMap((entry): PromptHistoryEntry[] => {
    if (!isObject(entry)) return [];
    const canonicalPrompt = boundedString(
      entry.canonical_prompt,
      12_000,
      boundedString(entry.prompt, 12_000),
    ).trim();
    const generationPrompt = boundedString(
      entry.generation_prompt,
      16_000,
      canonicalPrompt,
    ).trim();
    if (!canonicalPrompt || !generationPrompt) return [];
    const directives = normalizeDirectives(entry.active_directives);
    const roundIndex = numberValue(entry.round_index);
    const tuned = entry.tuned === true || directives.length > 0 || Boolean(entry.human_feedback);
    const refinementRaw = boundedString(entry.refinement_mode, 32).toLowerCase();
    const refinementMode: PromptRefinementMode = refinementRaw === "initial"
      ? "initial"
      : refinementRaw === "revision"
        ? "revision"
        : roundIndex > 0 || tuned ? "revision" : "initial";
    const anchor = normalizePromptAnchor(entry.visual_anchor, entry);
    const generationRaw = boundedString(entry.generation_mode, 64).toLowerCase();
    const generationMode: PromptGenerationMode = generationRaw === "candidate_anchored_rebase"
      ? "candidate_anchored_rebase"
      : generationRaw === "source_rebase"
        ? "source_rebase"
        : anchor ? "candidate_anchored_rebase" : "source_rebase";
    const templateVersion = boundedString(
      entry.prompt_template_version,
      120,
      boundedString(entry.canonical_template_version, 120),
    ) || undefined;
    const promptVersionHash = boundedString(
      entry.prompt_version_hash,
      128,
      boundedString(entry.version_hash, 128),
    ) || undefined;
    const basedOn = boundedString(
      entry.based_on_prompt_version_id,
      160,
      boundedString(
        entry.parent_prompt_version_id,
        160,
        isObject(entry.lineage) ? boundedString(entry.lineage.based_on_prompt_version_id, 160) : "",
      ),
    ) || undefined;
    const plan = normalizePromptPlan(
      entry.professional_prompt_plan ?? entry.structured_plan ?? entry.plan,
    );
    const promptRequestKey = boundedString(
      entry.prompt_request_key,
      256,
      boundedString(entry.prompt_refiner_request_key, 256, boundedString(entry.request_key, 256)),
    ) || undefined;
    return [{
      round_index: roundIndex,
      canonical_prompt: canonicalPrompt,
      canonical_prompt_hash: boundedString(entry.canonical_prompt_hash, 128) || undefined,
      generation_prompt: generationPrompt,
      generation_prompt_hash: boundedString(entry.generation_prompt_hash, 128) || undefined,
      prompt_version_id: boundedString(
        entry.prompt_version_id,
        160,
        boundedString(entry.version_id, 160),
      ) || undefined,
      prompt_version_hash: promptVersionHash,
      version_hash: promptVersionHash,
      based_on_prompt_version_id: basedOn,
      parent_prompt_version_id: basedOn,
      refinement_mode: refinementMode,
      generation_mode: generationMode,
      prompt_model: boundedString(entry.prompt_model, 160) || undefined,
      generation_model: boundedString(entry.generation_model, 160) || undefined,
      // `schema_version` is the PromptVersion envelope schema, not the
      // professional plan schema. Do not relabel it in the inspector.
      prompt_schema_version: boundedString(entry.prompt_schema_version, 160) || undefined,
      prompt_template_version: templateVersion,
      canonical_template_version: templateVersion,
      active_directives: directives,
      active_directives_hash: boundedString(entry.active_directives_hash, 128) || undefined,
      professional_prompt_plan: plan,
      prompt_summary: boundedString(
        entry.prompt_summary,
        600,
        boundedString(entry.summary, 600, plan?.summary ?? ""),
      ) || undefined,
      visual_anchor: anchor,
      prompt_request_key: promptRequestKey
        ?? (isObject(entry.lineage)
          ? boundedString(entry.lineage.prompt_request_key, 256) || undefined
          : undefined),
      source_manifest_hash: boundedString(entry.source_manifest_hash, 128) || undefined,
      human_feedback: boundedString(entry.human_feedback, 2_000) || undefined,
      human_selected_candidate_id: boundedString(entry.human_selected_candidate_id, 160) || undefined,
      tuned,
    }];
  }).sort((left, right) => left.round_index - right.round_index);
}

export async function createProject(draft: SourceDraft): Promise<ProjectRecord> {
  if (!draft.background) throw new Error("请先选择旅行原片");
  const form = new FormData();
  const background = draft.background.size > 18 * 1024 * 1024
    ? (await prepareImageForUpload(draft.background, "background")).file
    : draft.background;
  form.append("background", background);
  for (const reference of draft.references) {
    const prepared = reference.size > 8 * 1024 * 1024
      ? (await prepareImageForUpload(reference, "reference")).file
      : reference;
    form.append("cat_references", prepared);
  }
  const object = asObject(await request("/projects", { method: "POST", body: form }));
  const projectId = stringValue(object.project_id, stringValue(object.id));
  if (!projectId) throw new Error("项目创建成功，但响应缺少 project_id");
  return {
    project_id: projectId,
    source_manifest: normalizeManifest(object.source_manifest ?? object.manifest),
  };
}

export function createSearchIdempotencyKey(): string {
  return typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `search-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function startSearch(
  projectId: string,
  placement: PlacementIntent,
  userIntent: string,
  options: SearchOptions,
  idempotencyKey = createSearchIdempotencyKey(),
  guidanceMaskAssetId?: string | null,
): Promise<SearchRecord> {
  const payload = await request(`/projects/${encodeURIComponent(projectId)}/searches`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({
      placement,
      user_intent: userIntent.trim(),
      candidate_count: options.candidate_count,
      max_rounds: options.max_rounds,
      budget_usd: options.budget_usd,
      review_each_round: options.review_each_round,
      ...(guidanceMaskAssetId ? { guidance_mask_asset_id: guidanceMaskAssetId } : {}),
    }),
  });
  const object = asObject(payload);
  const searchId = stringValue(object.search_id, stringValue(object.id));
  if (!searchId) throw new Error("搜索已提交，但响应缺少 search_id");
  return {
    search_id: searchId,
    thread_id: stringValue(object.thread_id) || undefined,
    status: normalizeStatus(object.status),
    events_url: stringValue(object.events_url) || undefined,
  };
}

export async function uploadGuidanceMask(
  projectId: string,
  file: File,
): Promise<GuidanceMaskRegistration> {
  if (file.type !== "image/png") {
    throw new Error("引导区域必须导出为带透明通道的 PNG 图片");
  }
  if (!file.size) throw new Error("引导区域不能为空");
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error(`Guidance Mask 超过 ${MAX_UPLOAD_BYTES} 字节上传限制`);
  }
  const form = new FormData();
  form.append("mask", file);
  const object = asObject(await request(
    `/projects/${encodeURIComponent(projectId)}/guidance-masks`,
    { method: "POST", body: form },
  ));
  const asset = normalizeAsset(object.asset);
  if (!asset) throw new Error("引导区域上传成功，但响应缺少图片信息");
  return {
    project_id: stringValue(object.project_id, projectId),
    source_manifest_hash: stringValue(object.source_manifest_hash),
    asset,
  };
}

function normalizeSearchSnapshot(object: JsonObject, searchId: string): SearchSnapshot {
  const globalWinnerValue = isObject(object.global_winner) ? object.global_winner : undefined;
  const globalWinnerId = stringValue(
    object.global_winner_id,
    stringValue(globalWinnerValue?.candidate_id),
  ) || null;
  const roundWinnerId = stringValue(object.round_winner_id) || null;
  const evaluations = isObject(object.evaluations_by_candidate)
    ? object.evaluations_by_candidate
    : undefined;
  const rawCandidates = Array.isArray(object.candidates)
    ? object.candidates
    : Array.isArray(object.current_candidates)
      ? object.current_candidates
      : [];
  const candidates = rawCandidates
    .map((candidate) => normalizeCandidate(
      candidate,
      globalWinnerId,
      roundWinnerId,
      evaluations,
    ))
    .filter(Boolean) as SearchCandidate[];

  if (globalWinnerValue && globalWinnerId && !candidates.some((item) => item.candidate_id === globalWinnerId)) {
    const globalCandidate = normalizeCandidate(
      globalWinnerValue,
      globalWinnerId,
      roundWinnerId,
      evaluations,
    );
    if (globalCandidate) candidates.unshift(globalCandidate);
  }

  return {
    search_id: stringValue(object.search_id, searchId),
    thread_id: stringValue(object.thread_id) || undefined,
    status: normalizeStatus(object.status),
    events_url: stringValue(object.events_url) || undefined,
    round_index: numberValue(object.round_index, numberValue(object.current_round)),
    candidates,
    global_winner_id: globalWinnerId,
    global_winner_score:
      numberValue(object.global_winner_score, numberValue(globalWinnerValue?.score)) || null,
    prompt_history: normalizePromptHistory(object.prompt_history ?? object.prompt_versions),
    active_directives: normalizeDirectives(object.active_directives),
    stop_reason: stringValue(object.stop_reason) || null,
    estimated_cost_usd:
      numberValue(object.estimated_cost_usd, numberValue(object.cost_usd)) || null,
    interrupt_payload: isObject(object.interrupt_payload)
      ? {
          type: stringValue(object.interrupt_payload.type) || undefined,
          global_winner_id:
            stringValue(object.interrupt_payload.global_winner_id) || undefined,
          global_winner_score:
            numberValue(object.interrupt_payload.global_winner_score) || undefined,
          blocking_issues: normalizeSafeStringList(object.interrupt_payload.blocking_issues),
          allowed_actions: normalizeSafeStringList(object.interrupt_payload.allowed_actions, 16, 80),
        }
      : null,
  };
}

export async function getSearch(searchId: string): Promise<SearchSnapshot> {
  const object = asObject(await request(`/searches/${encodeURIComponent(searchId)}`));
  return normalizeSearchSnapshot(object, searchId);
}

export async function resumeSearch(
  searchId: string,
  action: ResumeAction,
  selectedCandidateId?: string | null,
  humanFeedback?: string | null,
  reviewedRoundIndex?: number | null,
): Promise<SearchSnapshot> {
  const body: Record<string, string | number | null> = {
    action,
    updated_user_intent: null,
  };
  if ((action === "accept_candidate" || action === "continue_one_round") && selectedCandidateId) {
    body.selected_candidate_id = selectedCandidateId;
  }
  if (action === "continue_one_round" && humanFeedback?.trim()) {
    body.human_feedback = humanFeedback.trim();
  }
  if (action === "continue_one_round" && typeof reviewedRoundIndex === "number") {
    body.reviewed_round_index = reviewedRoundIndex;
  }
  const object = asObject(await request(`/searches/${encodeURIComponent(searchId)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
  return normalizeSearchSnapshot(object, searchId);
}

export function searchEventsUrl(search: SearchRecord): string {
  if (search.events_url) return resolveApiUrl(search.events_url);
  return `${API_ROOT}/searches/${encodeURIComponent(search.search_id)}/events`;
}

function normalizeFusion(value: unknown): FusionResult {
  const object = asObject(value);
  const rawAsset = normalizeAsset(object.raw_asset);
  const fusionAsset = normalizeAsset(object.fusion_asset);
  const maskAsset = normalizeAsset(object.mask_asset);
  if (!rawAsset || !fusionAsset || !maskAsset) {
    throw new Error("融合结果缺少完整图片信息");
  }
  const boxValue = isObject(object.box) ? object.box : undefined;
  const box: FusionBox | undefined = boxValue
    ? {
        x: numberValue(boxValue.x),
        y: numberValue(boxValue.y),
        width: numberValue(boxValue.width),
        height: numberValue(boxValue.height),
      }
    : undefined;
  const inputMask = normalizeAsset(object.input_mask_asset);
  return {
    fusion_key: stringValue(object.fusion_key),
    search_id: stringValue(object.search_id),
    candidate_id: stringValue(object.candidate_id),
    source_manifest_hash: stringValue(object.source_manifest_hash),
    raw_asset: rawAsset,
    fusion_asset: fusionAsset,
    mask_asset: maskAsset,
    input_mask_asset: inputMask,
    feather_radius_px: numberValue(object.feather_radius_px),
    box,
    crop_mapping: isObject(object.crop_mapping) ? object.crop_mapping : undefined,
  };
}

export async function uploadFusionMask(
  searchId: string,
  file: File,
): Promise<FusionMaskRegistration> {
  if (file.type !== "image/png") {
    throw new Error("融合蒙版必须是带透明通道的 PNG 图片");
  }
  if (!file.size) throw new Error("融合蒙版不能为空");
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error(`Fusion Mask 超过 ${MAX_UPLOAD_BYTES} 字节上传限制`);
  }
  const form = new FormData();
  form.append("mask", file);
  const object = asObject(await request(
    `/searches/${encodeURIComponent(searchId)}/fusion-masks`,
    { method: "POST", body: form },
  ));
  const asset = normalizeAsset(object.asset);
  if (!asset) throw new Error("融合蒙版上传成功，但响应缺少图片信息");
  return {
    search_id: stringValue(object.search_id, searchId),
    source_manifest_hash: stringValue(object.source_manifest_hash),
    asset,
  };
}

export async function createFusion(
  searchId: string,
  payload: {
    candidate_id?: string;
    mask_asset_id?: string;
    box?: FusionBox;
    feather_radius_px: number;
  },
): Promise<FusionResult> {
  const { box, feather_radius_px: feather } = payload;
  if (Boolean(payload.mask_asset_id) === Boolean(box)) {
    throw new Error("融合范围只能选择矩形或 PNG 蒙版中的一种");
  }
  if (!Number.isInteger(feather) || feather < 0 || feather > 256) {
    throw new Error("融合羽化必须是 0 到 256 的整数");
  }
  if (box) {
    const values = [box.x, box.y, box.width, box.height];
    if (
      values.some((value) => !Number.isFinite(value))
      || box.x < 0
      || box.y < 0
      || box.width <= 0
      || box.height <= 0
      || box.x + box.width > 1
      || box.y + box.height > 1
    ) {
      throw new Error("融合矩形必须完整位于原片内");
    }
  }
  const object = asObject(await request(
    `/searches/${encodeURIComponent(searchId)}/fusions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  ));
  return normalizeFusion(object);
}

export async function getFusion(searchId: string, fusionKey: string): Promise<FusionResult> {
  const object = asObject(await request(
    `/searches/${encodeURIComponent(searchId)}/fusions/${encodeURIComponent(fusionKey)}`,
  ));
  return normalizeFusion(object);
}
