import { buildFlowEdges } from "./build-flow-edges";
import { resolveLayoutConfig } from "./config";
import { layoutBranchLocally } from "./layout-branch-locally";
import { packBranchesVertically } from "./pack-branches-vertically";
import type { ChoiceFlowGraph, FlowBounds, FlowNodeData, LayoutConfig, LayoutResult, PositionedFlowNode } from "./types";
import { validateChoiceFlow } from "./validate-choice-flow";

export function layoutChoiceFlow<TData extends FlowNodeData>(
  graph: ChoiceFlowGraph<TData>,
  configOverrides: Partial<LayoutConfig> = {},
): LayoutResult<TData> {
  const config = resolveLayoutConfig(configOverrides);
  validateChoiceFlow(graph, config);

  const localBranches = graph.branches.map((branch) => layoutBranchLocally(branch, config));
  const packed = packBranchesVertically(localBranches, config);
  const actualDepth = Math.max(0, ...graph.branches.map((branch) => branch.groups.length));
  const outputDepth = config.reserveMaxDepth ? config.maxDepth : actualDepth;
  const positions = {
    [graph.initialNode.id]: { x: 0, y: 0 },
    ...packed.positions,
    [graph.outputNode.id]: {
      x: (outputDepth + 1) * (config.nodeWidth + config.columnGapX),
      y: 0,
    },
  };

  const orderedBranches = [...graph.branches].sort((left, right) => (
    left.order - right.order || left.id.localeCompare(right.id)
  ));
  const nodes: PositionedFlowNode<TData>[] = [
    { ...graph.initialNode, role: "initial", position: positions[graph.initialNode.id] },
    ...orderedBranches.flatMap((branch) => branch.groups.flatMap((group) => (
      group.nodes.map((node) => ({ ...node, role: "choice" as const, position: positions[node.id] }))
    ))),
    { ...graph.outputNode, role: "output", position: positions[graph.outputNode.id] },
  ];
  const minX = Math.min(...nodes.map((node) => node.position.x - config.nodeWidth / 2));
  const maxX = Math.max(...nodes.map((node) => node.position.x + config.nodeWidth / 2));
  const minY = Math.min(...nodes.map((node) => node.position.y - config.nodeHeight / 2));
  const maxY = Math.max(...nodes.map((node) => node.position.y + config.nodeHeight / 2));
  const bounds: FlowBounds = { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };

  return {
    nodes,
    edges: buildFlowEdges(graph, positions),
    positions,
    branchBounds: packed.branchBounds,
    bounds,
    config,
  };
}

