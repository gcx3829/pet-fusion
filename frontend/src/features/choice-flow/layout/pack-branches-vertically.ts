import type { BranchLayoutBounds, FlowNodeData, LayoutConfig, LocalBranchLayout } from "./types";

export interface PackedBranches {
  positions: Record<string, { x: number; y: number }>;
  branchBounds: BranchLayoutBounds[];
}

export function packBranchesVertically<TData extends FlowNodeData>(
  branches: LocalBranchLayout<TData>[],
  config: LayoutConfig,
): PackedBranches {
  const ordered = [...branches].sort((left, right) => (
    left.branch.order - right.branch.order || left.branch.id.localeCompare(right.branch.id)
  ));
  if (!ordered.length) return { positions: {}, branchBounds: [] };

  const provisional: Array<{ layout: LocalBranchLayout<TData>; shiftY: number }> = [];
  let cursorY = 0;
  for (const layout of ordered) {
    const shiftY = cursorY - layout.bounds.minY;
    provisional.push({ layout, shiftY });
    cursorY = layout.bounds.maxY + shiftY + config.branchGapY;
  }
  const globalMinY = provisional[0].layout.bounds.minY + provisional[0].shiftY;
  const last = provisional[provisional.length - 1];
  const globalMaxY = last.layout.bounds.maxY + last.shiftY;
  const globalShiftY = -(globalMinY + globalMaxY) / 2;

  const positions: Record<string, { x: number; y: number }> = {};
  const branchBounds: BranchLayoutBounds[] = [];
  for (const { layout, shiftY } of provisional) {
    const finalShiftY = shiftY + globalShiftY;
    for (const [nodeId, position] of Object.entries(layout.positions)) {
      positions[nodeId] = { x: position.x, y: position.y + finalShiftY };
    }
    branchBounds.push({
      branchId: layout.branch.id,
      minX: layout.bounds.minX,
      maxX: layout.bounds.maxX,
      minY: layout.bounds.minY + finalShiftY,
      maxY: layout.bounds.maxY + finalShiftY,
      width: layout.bounds.width,
      height: layout.bounds.height,
    });
  }
  return { positions, branchBounds };
}

