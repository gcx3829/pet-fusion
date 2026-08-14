import type {
  ActiveDirective,
  AssetRef,
  CandidateEvaluation,
  FusionBox,
  FusionMaskRegistration,
  FusionResult,
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

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
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
    if (typeof directive === "string") return [{ instruction: directive }];
    if (!isObject(directive)) return [];
    const instruction = stringValue(directive.instruction);
    return instruction ? [{
      directive_id: stringValue(directive.directive_id) || undefined,
      category: stringValue(directive.category) || undefined,
      instruction,
    }] : [];
  });
}

function normalizePromptHistory(value: unknown): PromptHistoryEntry[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): PromptHistoryEntry[] => {
    if (!isObject(entry)) return [];
    const canonicalPrompt = stringValue(entry.canonical_prompt);
    const generationPrompt = stringValue(entry.generation_prompt);
    if (!canonicalPrompt || !generationPrompt) return [];
    const directives = normalizeDirectives(entry.active_directives);
    return [{
      round_index: numberValue(entry.round_index),
      canonical_prompt: canonicalPrompt,
      canonical_prompt_hash: stringValue(entry.canonical_prompt_hash) || undefined,
      generation_prompt: generationPrompt,
      generation_prompt_hash: stringValue(entry.generation_prompt_hash) || undefined,
      canonical_template_version: stringValue(entry.canonical_template_version) || undefined,
      active_directives: directives,
      active_directives_hash: stringValue(entry.active_directives_hash) || undefined,
      tuned: entry.tuned === true || directives.length > 0,
    }];
  }).sort((left, right) => left.round_index - right.round_index);
}

export async function createProject(draft: SourceDraft): Promise<ProjectRecord> {
  if (!draft.background) throw new Error("请先选择旅行原片");
  const form = new FormData();
  const background = draft.background.size > MAX_UPLOAD_BYTES
    ? (await prepareImageForUpload(draft.background, "background")).file
    : draft.background;
  form.append("background", background);
  for (const reference of draft.references) {
    const prepared = reference.size > MAX_UPLOAD_BYTES
      ? (await prepareImageForUpload(reference, "reference")).file
      : reference;
    form.append("cat_references", prepared);
  }
  if (draft.catName.trim()) form.append("cat_name", draft.catName.trim());
  if (draft.catTraits.trim()) form.append("cat_traits", draft.catTraits.trim());
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

export async function getSearch(searchId: string): Promise<SearchSnapshot> {
  const object = asObject(await request(`/searches/${encodeURIComponent(searchId)}`));
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
    prompt_history: normalizePromptHistory(object.prompt_history),
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
          blocking_issues: Array.isArray(object.interrupt_payload.blocking_issues)
            ? object.interrupt_payload.blocking_issues.map(String)
            : undefined,
          allowed_actions: Array.isArray(object.interrupt_payload.allowed_actions)
            ? object.interrupt_payload.allowed_actions.map(String)
            : undefined,
        }
      : null,
  };
}

export async function resumeSearch(
  searchId: string,
  action: ResumeAction,
  selectedCandidateId?: string | null,
): Promise<void> {
  const body: Record<string, string | null> = {
    action,
    updated_user_intent: null,
  };
  if (action === "accept_candidate" && selectedCandidateId) {
    body.selected_candidate_id = selectedCandidateId;
  }
  await request(`/searches/${encodeURIComponent(searchId)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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
    throw new Error("Fusion 响应缺少完整资产引用");
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
    throw new Error("Fusion Mask 必须是 PNG alpha 图片");
  }
  if (!file.size) throw new Error("Fusion Mask 不能为空");
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
  if (!asset) throw new Error("Fusion Mask 上传成功，但响应缺少资产引用");
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
    throw new Error("Fusion 必须且只能提供矩形或 PNG alpha mask 之一");
  }
  if (!Number.isInteger(feather) || feather < 0 || feather > 256) {
    throw new Error("Fusion 羽化半径必须是 0 到 256 的整数");
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
      throw new Error("Fusion 矩形必须完整位于原片归一化边界内");
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
