import { getCenteredOffsets } from "./get-centered-offsets";
import type { FlowBounds, FlowBranch, FlowNodeData, LayoutConfig, LocalBranchLayout } from "./types";

function verticalBounds(positions: Record<string, { x: number; y: number }>, config: LayoutConfig): FlowBounds {
  const values = Object.values(positions);
  const minX = Math.min(...values.map(({ x }) => x - config.nodeWidth / 2));
  const maxX = Math.max(...values.map(({ x }) => x + config.nodeWidth / 2));
  const minY = Math.min(...values.map(({ y }) => y - config.nodeHeight / 2));
  const maxY = Math.max(...values.map(({ y }) => y + config.nodeHeight / 2));
  return { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };
}

export function layoutBranchLocally<TData extends FlowNodeData>(
  branch: FlowBranch<TData>,
  config: LayoutConfig,
): LocalBranchLayout<TData> {
  const positions: Record<string, { x: number; y: number }> = {};
  let groupAnchorY = 0;

  branch.groups.forEach((group, depth) => {
    const groupX = (depth + 1) * (config.nodeWidth + config.columnGapX);
    const offsets = getCenteredOffsets(group.nodes.length, config.nodeHeight, config.nodeGapY);
    group.nodes.forEach((node, index) => {
      positions[node.id] = { x: groupX, y: groupAnchorY + offsets[index] };
    });
    if (depth < branch.groups.length - 1 && config.alignNextGroupToContinuation) {
      groupAnchorY = positions[group.continueFromNodeId!].y;
    }
  });

  const uncenteredBounds = verticalBounds(positions, config);
  const centerY = (uncenteredBounds.minY + uncenteredBounds.maxY) / 2;
  for (const position of Object.values(positions)) position.y -= centerY;
  return { branch, positions, bounds: verticalBounds(positions, config) };
}

