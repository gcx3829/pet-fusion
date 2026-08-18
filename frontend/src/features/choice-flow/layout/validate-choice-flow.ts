import type { ChoiceFlowGraph, FlowNodeData, LayoutConfig } from "./types";

function assertPositive(value: number, name: keyof LayoutConfig): void {
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`LayoutConfig.${name} must be greater than zero; received ${String(value)}`);
  }
}

export function validateChoiceFlow<TData extends FlowNodeData>(
  graph: ChoiceFlowGraph<TData>,
  config: LayoutConfig,
): void {
  assertPositive(config.nodeWidth, "nodeWidth");
  assertPositive(config.nodeHeight, "nodeHeight");
  assertPositive(config.nodeGapY, "nodeGapY");
  assertPositive(config.columnGapX, "columnGapX");
  assertPositive(config.branchGapY, "branchGapY");
  assertPositive(config.maxDepth, "maxDepth");
  if (!Number.isInteger(config.maxDepth)) {
    throw new Error(`LayoutConfig.maxDepth must be an integer; received ${String(config.maxDepth)}`);
  }

  const nodeIds = new Set<string>();
  const registerNode = (nodeId: string, context: string) => {
    if (!nodeId.trim()) throw new Error(`${context}: Node id must not be empty`);
    if (nodeIds.has(nodeId)) throw new Error(`${context}: duplicate Node id "${nodeId}"`);
    nodeIds.add(nodeId);
  };
  registerNode(graph.initialNode.id, "initialNode");
  registerNode(graph.outputNode.id, "outputNode");

  if (!graph.branches.length) {
    throw new Error("ChoiceFlowGraph must contain at least one Branch");
  }

  const branchIds = new Set<string>();
  const groupIds = new Set<string>();
  for (const branch of graph.branches) {
    const branchContext = `Branch "${branch.id}"`;
    if (!branch.id.trim()) throw new Error("Branch id must not be empty");
    if (branchIds.has(branch.id)) throw new Error(`${branchContext}: duplicate Branch id`);
    branchIds.add(branch.id);
    if (branch.groups.length < 1 || branch.groups.length > 3) {
      throw new Error(`${branchContext}: expected 1..3 Groups, received ${branch.groups.length}`);
    }
    if (config.maxDepth < branch.groups.length) {
      throw new Error(`${branchContext}: LayoutConfig.maxDepth ${config.maxDepth} is smaller than actual Group depth ${branch.groups.length}`);
    }

    branch.groups.forEach((group, groupIndex) => {
      const groupContext = `${branchContext}, Group "${group.id}" at depth ${groupIndex}`;
      if (!group.id.trim()) throw new Error(`${branchContext}: Group id must not be empty`);
      if (groupIds.has(group.id)) throw new Error(`${groupContext}: duplicate Group id`);
      groupIds.add(group.id);
      if (group.nodes.length < 1 || group.nodes.length > 3) {
        throw new Error(`${groupContext}: expected 1..3 Nodes, received ${group.nodes.length}`);
      }
      for (const node of group.nodes) registerNode(node.id, `${groupContext}, Node "${node.id}"`);

      const isLast = groupIndex === branch.groups.length - 1;
      if (!isLast) {
        if (!group.continueFromNodeId) {
          throw new Error(`${groupContext}: continueFromNodeId is required for a non-terminal Group`);
        }
        if (!group.nodes.some((node) => node.id === group.continueFromNodeId)) {
          throw new Error(`${groupContext}: continueFromNodeId "${group.continueFromNodeId}" does not belong to this Group`);
        }
      } else if (group.continueFromNodeId) {
        throw new Error(`${groupContext}: the final Group must not define continueFromNodeId`);
      }
    });

    const lastGroup = branch.groups[branch.groups.length - 1];
    if (!lastGroup.nodes.some((node) => node.id === branch.terminalNodeId)) {
      throw new Error(`${branchContext}, Group "${lastGroup.id}": terminalNodeId "${branch.terminalNodeId}" must belong to the final Group`);
    }
  }
}
