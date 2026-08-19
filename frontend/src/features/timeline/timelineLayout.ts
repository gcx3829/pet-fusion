import type { TimelineGraph } from "./buildTimelineGraph";
import { layoutChoiceFlow } from "../choice-flow/layout/layout-choice-flow";
import type { ChoiceFlowGraph, FlowEdgeSpec, FlowNodeData } from "../choice-flow/layout/types";

export interface TimelinePoint { x: number; y: number; width: number; height: number; }
export interface TimelineLayout { width: number; height: number; positions: Record<string, TimelinePoint>; edges: FlowEdgeSpec[]; }

interface TimelineLayoutData extends FlowNodeData { timelineNodeId: string }

const TIMELINE_NODE_WIDTH = 132;
const TIMELINE_NODE_HEIGHT = 94;

function legacyLayout(graph: TimelineGraph, minWidth: number): TimelineLayout {
  // Keep the same horizontal rhythm as the constrained ChoiceFlow layout so
  // a fourth candidate or an incomplete graph does not visibly collapse.
  const gap = 82;
  const rounds = new Map<number, number>();
  const positions: Record<string, TimelinePoint> = {};
  let column = 0;
  for (const node of graph.nodes) {
    if (node.kind === "source") {
      positions[node.id] = { x: 0, y: 0, width: TIMELINE_NODE_WIDTH, height: TIMELINE_NODE_HEIGHT };
      column = 1;
      continue;
    }
    if (node.kind === "final") continue;
    const round = node.roundIndex ?? 0;
    const ordinal = rounds.get(round) ?? 0;
    rounds.set(round, ordinal + 1);
    positions[node.id] = {
      x: (round + column) * (TIMELINE_NODE_WIDTH + gap),
      y: (ordinal - ((graph.nodes.filter((item) => item.kind === "candidate" && (item.roundIndex ?? 0) === round).length - 1) / 2)) * (TIMELINE_NODE_HEIGHT + 22),
      width: TIMELINE_NODE_WIDTH,
      height: TIMELINE_NODE_HEIGHT,
    };
  }
  const final = graph.nodes.find((node) => node.kind === "final");
  if (final) {
    const maxX = Math.max(TIMELINE_NODE_WIDTH / 2, ...Object.values(positions).map((point) => point.x));
    positions[final.id] = { x: maxX + TIMELINE_NODE_WIDTH + gap, y: 0, width: TIMELINE_NODE_WIDTH, height: TIMELINE_NODE_HEIGHT };
  }
  const flowEdges: FlowEdgeSpec[] = graph.edges.map((edge) => ({
    id: `${edge.from}__${edge.to}`,
    source: edge.from,
    target: edge.to,
    sourceHandle: "out",
    targetHandle: "in",
    type: positions[edge.from]?.y === positions[edge.to]?.y ? "straight" : "default",
  }));
  const contentWidth = Math.max(minWidth, TIMELINE_NODE_WIDTH + Math.max(0, ...Object.values(positions).map((point) => point.x + TIMELINE_NODE_WIDTH / 2)));
  const contentHeight = TIMELINE_NODE_HEIGHT + Math.max(TIMELINE_NODE_HEIGHT, ...Object.values(positions).map((point) => point.y + TIMELINE_NODE_HEIGHT / 2));
  return { width: contentWidth, height: contentHeight, positions, edges: flowEdges };
}

export function layoutTimelineGraph(graph: TimelineGraph, minWidth = 880): TimelineLayout {
  const source = graph.nodes.find((node) => node.kind === "source");
  const output = graph.nodes.find((node) => node.kind === "final");
  const choiceNodes = graph.nodes.filter((node) => node.kind === "candidate");
  if (!source || !choiceNodes.length) return legacyLayout(graph, minWidth);

  const grouped = new Map<number, typeof choiceNodes>();
  for (const node of choiceNodes) {
    const roundIndex = node.roundIndex ?? 0;
    grouped.set(roundIndex, [...(grouped.get(roundIndex) ?? []), node]);
  }
  const orderedGroups = [...grouped.entries()].sort(([left], [right]) => left - right);
  if (orderedGroups.length > 3 || orderedGroups.some(([, nodes]) => nodes.length > 3)) {
    return legacyLayout(graph, minWidth);
  }

  const asSpec = (node: (typeof graph.nodes)[number]) => ({
    id: node.id,
    data: { label: node.label, detail: node.detail, timelineNodeId: node.id },
  });
  const groups = orderedGroups.map(([roundIndex, nodes], groupIndex) => {
    const nextIds = new Set(orderedGroups[groupIndex + 1]?.[1].map((node) => node.id) ?? []);
    const continuation = groupIndex < orderedGroups.length - 1
      ? graph.edges.find((edge) => nodes.some((node) => node.id === edge.from) && nextIds.has(edge.to))?.from
      : undefined;
    return {
      id: `round-${roundIndex}`,
      nodes: nodes.map(asSpec),
      continueFromNodeId: continuation,
    };
  });
  const lastIds = new Set(orderedGroups.at(-1)![1].map((node) => node.id));
  const terminalNodeId = output
    ? graph.edges.find((edge) => lastIds.has(edge.from) && edge.to === output.id)?.from
      ?? groups.at(-1)!.nodes[0]!.id
    : groups.at(-1)!.nodes[0]!.id;
  if (groups.some((group, index) => index < groups.length - 1 && !group.continueFromNodeId)) {
    return legacyLayout(graph, minWidth);
  }
  // The generic layout requires an output node. Use an internal sentinel while
  // generation is still in progress, then discard it. This keeps every real
  // column at identical coordinates before and after acceptance.
  const layoutOutput = output ?? {
    id: "__timeline_layout_output__",
    kind: "final" as const,
    imageUrl: "",
    label: "layout sentinel",
    status: "complete" as const,
    badges: [],
  };
  const choiceGraph: ChoiceFlowGraph<TimelineLayoutData> = {
    initialNode: asSpec(source),
    outputNode: asSpec(layoutOutput),
    branches: [{ id: "search", order: 0, groups, terminalNodeId }],
  };
  const result = layoutChoiceFlow(choiceGraph, {
    nodeWidth: TIMELINE_NODE_WIDTH,
    nodeHeight: TIMELINE_NODE_HEIGHT,
    nodeGapY: 22,
    columnGapX: 82,
    branchGapY: 72,
    // The generic choice-flow view reserves maxDepth for stable authoring.
    // Timeline output is event-driven: once acceptance creates the final node,
    // place it directly after the deepest real Group instead of keeping empty
    // columns for rounds that may never happen.
    reserveMaxDepth: false,
  });
  const positions = Object.fromEntries(Object.entries(result.positions).map(([id, position]) => [
    id,
    { ...position, width: TIMELINE_NODE_WIDTH, height: TIMELINE_NODE_HEIGHT },
  ]));
  if (!output) delete positions[layoutOutput.id];

  // A historical accepted winner can originate before the final choice group.
  // Route its built-in Bezier below the intervening nodes instead of drawing a
  // straight line underneath them, where it appears to vanish.
  if (output) {
    const acceptedEdge = graph.edges.find((edge) => edge.to === output.id);
    if (acceptedEdge && !lastIds.has(acceptedEdge.from)) {
      const maxChoiceY = Math.max(...choiceNodes.map((node) => positions[node.id]!.y));
      positions[output.id]!.y = maxChoiceY + TIMELINE_NODE_HEIGHT + 44;
    }
  }
  const edges: FlowEdgeSpec[] = graph.edges.map((edge) => ({
    id: `${edge.from}__${edge.to}`,
    source: edge.from,
    target: edge.to,
    sourceHandle: "out",
    targetHandle: "in",
    type: positions[edge.from]?.y === positions[edge.to]?.y ? "straight" : "default",
  }));
  const positioned = Object.values(positions);
  const minX = Math.min(...positioned.map((point) => point.x - point.width / 2));
  const maxX = Math.max(...positioned.map((point) => point.x + point.width / 2));
  const minY = Math.min(...positioned.map((point) => point.y - point.height / 2));
  const maxY = Math.max(...positioned.map((point) => point.y + point.height / 2));
  return {
    width: Math.max(minWidth, maxX - minX),
    height: maxY - minY,
    positions,
    edges,
  };
}
