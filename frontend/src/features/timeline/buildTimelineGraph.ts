import { rawCandidateUrl } from "../../lib/raw";
import type { SearchCandidate, SearchEvent, SearchSnapshot } from "../../types";

export type TimelineNodeKind = "source" | "candidate" | "final";
export type TimelineNodeStatus = "active" | "complete" | "failed";

export interface TimelineNode {
  id: string;
  kind: TimelineNodeKind;
  imageUrl: string;
  label: string;
  detail?: string;
  roundIndex?: number;
  candidateId?: string;
  score?: number;
  status: TimelineNodeStatus;
  progress?: "indeterminate";
  placeholder?: boolean;
  badges: string[];
}

export interface TimelineEdge {
  id: string;
  from: string;
  to: string;
  relation: "rebase" | "continue" | "accept";
}

export interface TimelineGraph { nodes: TimelineNode[]; edges: TimelineEdge[]; }

export interface TimelineMedia {
  sourceImageUrl?: string | null;
  guidanceActive?: boolean;
  fusionImageUrl?: string | null;
  acceptedCandidateId?: string | null;
  expectedCandidateCount?: number;
  generationActive?: boolean;
  currentRound?: number;
}

function candidateBadges(candidate: SearchCandidate): string[] {
  const badges: string[] = [];
  if (candidate.is_global_winner) badges.push("GLOBAL");
  else if (candidate.is_round_winner) badges.push("WINNER");
  const blocking = candidate.evaluation?.issues.filter((issue) => issue.severity === "blocking").length ?? 0;
  if (blocking) badges.push(`${blocking} BLOCK`);
  else if (candidate.evaluation) badges.push("CRITIC OK");
  return badges;
}

function candidateNode(candidate: SearchCandidate, snapshot?: SearchSnapshot | null): TimelineNode | null {
  const imageUrl = rawCandidateUrl(candidate);
  if (!imageUrl) return null;
  const evaluated = Boolean(candidate.evaluation);
  const active = snapshot?.status === "running" && candidate.round_index === snapshot.round_index && !evaluated;
  return {
    id: `candidate:${candidate.candidate_id}`,
    kind: "candidate",
    imageUrl,
    label: `R${candidate.round_index} · ${candidate.variant_index + 1}`,
    detail: evaluated ? "Critic 已完成" : "Raw candidate",
    roundIndex: candidate.round_index,
    candidateId: candidate.candidate_id,
    score: candidate.score,
    status: active ? "active" : snapshot?.status === "failed" && candidate.round_index === snapshot.round_index ? "failed" : "complete",
    progress: active ? "indeterminate" : undefined,
    badges: candidateBadges(candidate),
  };
}

/** Only durable photo states become nodes. Prompt/Generate/Critic events are
 * metadata on those photo states. Edges after Round 0 describe the reviewed
 * candidate whose feedback planned the next Group; they are decision lineage,
 * not image-input lineage. The backend still rebases every generation request
 * to the immutable source. */
export function buildTimelineGraph(
  _events: SearchEvent[],
  snapshot?: SearchSnapshot | null,
  media: TimelineMedia = {},
): TimelineGraph {
  const nodes: TimelineNode[] = [];
  const edges: TimelineEdge[] = [];
  if (media.sourceImageUrl) {
    nodes.push({
      id: "source",
      kind: "source",
      imageUrl: media.sourceImageUrl,
      label: "底图",
      detail: media.guidanceActive ? "Guidance Mask" : "原始照片",
      status: "complete",
      badges: media.guidanceActive ? ["MASK"] : [],
    });
  }
  const candidates = [...(snapshot?.candidates ?? [])].sort((left, right) => (
    left.round_index - right.round_index || left.variant_index - right.variant_index
  ));
  for (const candidate of candidates) {
    const node = candidateNode(candidate, snapshot);
    if (!node) continue;
    nodes.push(node);
  }

  if (media.sourceImageUrl && media.generationActive) {
    const roundIndex = media.currentRound ?? snapshot?.round_index ?? 0;
    const expectedCount = Math.max(1, Math.min(4, media.expectedCandidateCount ?? 1));
    const readyVariants = new Set(
      candidates
        .filter((candidate) => candidate.round_index === roundIndex)
        .map((candidate) => candidate.variant_index),
    );
    for (let variantIndex = 0; variantIndex < expectedCount; variantIndex += 1) {
      if (readyVariants.has(variantIndex)) continue;
      const id = `pending:${roundIndex}:${variantIndex}`;
      nodes.push({
        id,
        kind: "candidate",
        imageUrl: media.sourceImageUrl,
        label: `R${roundIndex} · ${variantIndex + 1}`,
        detail: "正在生成",
        roundIndex,
        status: "active",
        progress: "indeterminate",
        placeholder: true,
        badges: ["GENERATING"],
      });
    }
  }

  let hasAcceptedOutput = false;
  let acceptedSourceId: string | null = null;
  if (snapshot?.status === "accepted") {
    const accepted = candidates.find((candidate) => candidate.candidate_id === media.acceptedCandidateId)
      ?? candidates.find((candidate) => candidate.candidate_id === snapshot.global_winner_id)
      ?? candidates.find((candidate) => candidate.is_global_winner);
    const acceptedUrl = media.fusionImageUrl ?? rawCandidateUrl(accepted);
    if (accepted && acceptedUrl) {
      const sourceId = `candidate:${accepted.candidate_id}`;
      nodes.push({
        id: "final",
        kind: "final",
        imageUrl: acceptedUrl,
        label: media.fusionImageUrl ? "Fusion" : "已接受",
        detail: media.fusionImageUrl ? "最终融合照片" : "等待 Fusion Mask",
        candidateId: accepted.candidate_id,
        score: accepted.score,
        status: "complete",
        badges: [media.fusionImageUrl ? "FINAL" : "ACCEPTED"],
      });
      hasAcceptedOutput = nodes.some((node) => node.id === sourceId);
      acceptedSourceId = hasAcceptedOutput ? sourceId : null;
    }
  }

  // Fusion is not a future Search stage and must not reserve Timeline space.
  // Materialize the final node only after a candidate has actually been
  // accepted; React Flow will then receive a new graph and fit the compact
  // layout around the real terminal node.
  const choiceNodes = nodes.filter((node) => node.kind === "candidate");

  const groups = new Map<number, TimelineNode[]>();
  for (const node of choiceNodes) {
    const roundIndex = node.roundIndex ?? 0;
    groups.set(roundIndex, [...(groups.get(roundIndex) ?? []), node]);
  }
  const orderedGroups = [...groups.entries()].sort(([left], [right]) => left - right);
  const chooseContinuation = (groupIndex: number): TimelineNode => {
    const [roundIndex, groupNodes] = orderedGroups[groupIndex];
    const explicitCandidateId = snapshot?.prompt_history.find((entry) => (
      entry.round_index === roundIndex + 1
    ))?.human_selected_candidate_id;
    return groupNodes.find((node) => node.candidateId === explicitCandidateId)
      ?? groupNodes.find((node) => node.candidateId && candidates.find((candidate) => (
        candidate.candidate_id === node.candidateId && candidate.is_round_winner
      )))
      ?? groupNodes.find((node) => node.candidateId && candidates.find((candidate) => (
        candidate.candidate_id === node.candidateId && candidate.is_global_winner
      )))
      ?? groupNodes[0];
  };

  if (media.sourceImageUrl && orderedGroups.length) {
    orderedGroups[0][1].forEach((node) => edges.push({
      id: `source__${node.id}`,
      from: "source",
      to: node.id,
      relation: "rebase",
    }));
    for (let groupIndex = 0; groupIndex < orderedGroups.length - 1; groupIndex += 1) {
      const continuation = chooseContinuation(groupIndex);
      orderedGroups[groupIndex + 1][1].forEach((node) => edges.push({
        id: `${continuation.id}__${node.id}`,
        from: continuation.id,
        to: node.id,
        relation: "continue",
      }));
    }
    if (nodes.some((node) => node.id === "final")) {
      // Acceptance lineage must point at the image the user actually chose,
      // even when the historical Global Winner belongs to an earlier round.
      // timelineLayout intentionally falls back to its unrestricted layout in
      // that case because ChoiceFlow terminals must belong to the final Group.
      const terminal = choiceNodes.find((node) => node.id === acceptedSourceId)
        ?? chooseContinuation(orderedGroups.length - 1);
      edges.push({
        id: `${terminal.id}__final`,
        from: terminal.id,
        to: "final",
        relation: hasAcceptedOutput ? "accept" : "continue",
      });
    }
  }
  return { nodes, edges };
}
