import type { SearchCandidate } from "../types";

/**
 * Resolve the asset that is authoritative during Search and review.
 *
 * Search is deliberately raw-first.  The legacy protected asset fields stay
 * readable for old rows and Local Fix, but they must never silently become a
 * Search, Critic, or timeline preview input.
 */
export function rawCandidateUrl(candidate: Pick<SearchCandidate, "raw_image_url" | "raw_asset_url" | "image_url"> | null | undefined): string | null {
  if (!candidate) return null;
  return candidate.raw_image_url ?? candidate.raw_asset_url ?? candidate.image_url ?? null;
}

export function isRawCandidateUrl(
  candidate: Pick<SearchCandidate, "raw_image_url" | "raw_asset_url" | "image_url">,
  url: string | null | undefined,
): boolean {
  return Boolean(url) && rawCandidateUrl(candidate) === url;
}
