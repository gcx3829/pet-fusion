import type { ChoiceFlowGraph, FlowEdgeSpec, FlowNodeData, FlowPosition } from "./types";

export function buildFlowEdges<TData extends FlowNodeData>(
  graph: ChoiceFlowGraph<TData>,
  positions: Record<string, FlowPosition>,
): FlowEdgeSpec[] {
  const edges: FlowEdgeSpec[] = [];
  const append = (source: string, target: string) => {
    edges.push({
      id: `${source}__${target}`,
      source,
      target,
      sourceHandle: "out",
      targetHandle: "in",
      type: positions[source].y === positions[target].y ? "straight" : "default",
    });
  };

  const orderedBranches = [...graph.branches].sort((left, right) => (
    left.order - right.order || left.id.localeCompare(right.id)
  ));
  for (const branch of orderedBranches) {
    branch.groups[0].nodes.forEach((node) => append(graph.initialNode.id, node.id));
    for (let groupIndex = 0; groupIndex < branch.groups.length - 1; groupIndex += 1) {
      const group = branch.groups[groupIndex];
      branch.groups[groupIndex + 1].nodes.forEach((node) => append(group.continueFromNodeId!, node.id));
    }
    append(branch.terminalNodeId, graph.outputNode.id);
  }
  return edges;
}

