import type { TimelineGraph } from "./buildTimelineGraph";

export interface TimelinePoint { x: number; y: number; width: number; height: number; }
export interface TimelineLayout { width: number; height: number; positions: Record<string, TimelinePoint>; }

export function layoutTimelineGraph(graph: TimelineGraph, minWidth = 880): TimelineLayout {
  const width = 132;
  const height = 94;
  const gap = 34;
  const rounds = new Map<number, number>();
  const positions: Record<string, TimelinePoint> = {};
  let column = 0;
  for (const node of graph.nodes) {
    if (node.kind === "source") {
      positions[node.id] = { x: 18, y: 22, width, height };
      column = 1;
      continue;
    }
    if (node.kind === "final") {
      const maxX = Math.max(18, ...Object.values(positions).map((point) => point.x));
      positions[node.id] = { x: maxX + width + gap, y: 22, width, height };
      continue;
    }
    const round = node.roundIndex ?? 0;
    const ordinal = rounds.get(round) ?? 0;
    rounds.set(round, ordinal + 1);
    positions[node.id] = {
      x: 18 + (round + column) * (width + gap),
      y: 14 + ordinal * (height + 10),
      width,
      height,
    };
  }
  const contentWidth = Math.max(minWidth, 36 + Math.max(0, ...Object.values(positions).map((point) => point.x + point.width)));
  const contentHeight = 28 + Math.max(height, ...Object.values(positions).map((point) => point.y + point.height));
  return { width: contentWidth, height: contentHeight, positions };
}
