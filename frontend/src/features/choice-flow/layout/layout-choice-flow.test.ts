import { describe, expect, it } from "vitest";
import { DEFAULT_LAYOUT_CONFIG } from "./config";
import { layoutChoiceFlow } from "./layout-choice-flow";
import type { ChoiceFlowGraph, FlowNodeData, FlowNodeSpec, LayoutResult } from "./types";

interface TestData extends FlowNodeData { label: string }

const node = (id: string): FlowNodeSpec<TestData> => ({ id, data: { label: id } });

function singleBranch(groupSizes: number[], continuations: number[] = []): ChoiceFlowGraph<TestData> {
  const groups = groupSizes.map((count, groupIndex) => {
    const nodes = Array.from({ length: count }, (_, nodeIndex) => node(`g${groupIndex}-n${nodeIndex}`));
    return {
      id: `g${groupIndex}`,
      nodes,
      continueFromNodeId: groupIndex < groupSizes.length - 1
        ? nodes[continuations[groupIndex] ?? Math.floor(count / 2)].id
        : undefined,
    };
  });
  return {
    initialNode: node("initial"),
    outputNode: node("output"),
    branches: [{ id: "branch-0", order: 0, groups, terminalNodeId: groups.at(-1)!.nodes[0].id }],
  };
}

function rectanglesOverlap(
  left: { position: { x: number; y: number } },
  right: { position: { x: number; y: number } },
  nodeWidth: number,
  nodeHeight: number,
): boolean {
  return Math.abs(left.position.x - right.position.x) < nodeWidth
    && Math.abs(left.position.y - right.position.y) < nodeHeight;
}

function assertLayoutInvariants(graph: ChoiceFlowGraph<TestData>, result: LayoutResult<TestData>): void {
  const ids = new Set(result.nodes.map(({ id }) => id));
  expect(ids.size).toBe(result.nodes.length);
  for (const edge of result.edges) {
    expect(ids.has(edge.source)).toBe(true);
    expect(ids.has(edge.target)).toBe(true);
    expect(edge.id).toBe(`${edge.source}__${edge.target}`);
    expect(edge.sourceHandle).toBe("out");
    expect(edge.targetHandle).toBe("in");
  }
  for (let leftIndex = 0; leftIndex < result.nodes.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < result.nodes.length; rightIndex += 1) {
      expect(rectanglesOverlap(
        result.nodes[leftIndex],
        result.nodes[rightIndex],
        result.config.nodeWidth,
        result.config.nodeHeight,
      )).toBe(false);
    }
  }
  const outputX = result.positions[graph.outputNode.id].x;
  expect(outputX).toBe(Math.max(...Object.values(result.positions).map(({ x }) => x)));
  expect(result.positions[graph.initialNode.id].y).toBe(0);
  expect(result.positions[graph.outputNode.id].y).toBe(0);
  for (let index = 1; index < result.branchBounds.length; index += 1) {
    const gap = result.branchBounds[index].minY - result.branchBounds[index - 1].maxY;
    expect(gap).toBeGreaterThanOrEqual(result.config.branchGapY);
  }
}

describe("layoutChoiceFlow", () => {
  it("lays out one branch with one Group and one Node", () => {
    const graph = singleBranch([1]);
    const result = layoutChoiceFlow(graph);
    expect(result.positions).toMatchObject({
      initial: { x: 0, y: 0 },
      "g0-n0": { x: 360, y: 0 },
      output: { x: 1440, y: 0 },
    });
    expect(result.edges.map(({ id, type }) => [id, type])).toEqual([
      ["initial__g0-n0", "straight"],
      ["g0-n0__output", "straight"],
    ]);
  });

  it.each([1, 2, 3])("centers a Group containing %i Nodes", (count) => {
    const result = layoutChoiceFlow(singleBranch([count]));
    const ys = Array.from({ length: count }, (_, index) => result.positions[`g0-n${index}`].y);
    expect(ys.reduce((sum, y) => sum + y, 0) / count).toBe(0);
    if (count > 1) expect(ys[1] - ys[0]).toBe(DEFAULT_LAYOUT_CONFIG.nodeHeight + DEFAULT_LAYOUT_CONFIG.nodeGapY);
  });

  it.each([
    ["top", 0],
    ["middle", 1],
    ["bottom", 2],
  ] as const)("aligns the next Group to the %s continuation", (_label, continuationIndex) => {
    const result = layoutChoiceFlow(singleBranch([3, 2], [continuationIndex]));
    const nextAnchor = (result.positions["g1-n0"].y + result.positions["g1-n1"].y) / 2;
    expect(nextAnchor).toBe(result.positions[`g0-n${continuationIndex}`].y);
  });

  it("preserves continuation alignment through three extreme Groups", () => {
    const result = layoutChoiceFlow(singleBranch([3, 3, 3], [0, 2]));
    expect(result.positions["g1-n1"].y).toBe(result.positions["g0-n0"].y);
    expect(result.positions["g2-n1"].y).toBe(result.positions["g1-n2"].y);
    assertLayoutInvariants(singleBranch([3, 3, 3], [0, 2]), result);
  });

  it("packs differently sized Branches by their actual bounds", () => {
    const first = singleBranch([3, 3], [0]).branches[0];
    const second = { ...singleBranch([1]).branches[0], id: "branch-1", order: 1 };
    const third = { ...singleBranch([2, 3], [1]).branches[0], id: "branch-2", order: 2 };
    const rename = (branch: typeof first, prefix: string) => ({
      ...branch,
      id: prefix,
      groups: branch.groups.map((group) => ({
        ...group,
        id: `${prefix}-${group.id}`,
        nodes: group.nodes.map((item) => node(`${prefix}-${item.id}`)),
        continueFromNodeId: group.continueFromNodeId ? `${prefix}-${group.continueFromNodeId}` : undefined,
      })),
      terminalNodeId: `${prefix}-${branch.terminalNodeId}`,
    });
    const graph: ChoiceFlowGraph<TestData> = {
      initialNode: node("initial"), outputNode: node("output"),
      branches: [rename(first, "a"), rename(second, "b"), rename(third, "c")],
    };
    const result = layoutChoiceFlow(graph);
    assertLayoutInvariants(graph, result);
    expect((result.branchBounds[0].minY + result.branchBounds.at(-1)!.maxY) / 2).toBe(0);
  });

  it("places output at actual depth when reserveMaxDepth is disabled", () => {
    const graph = singleBranch([1]);
    const result = layoutChoiceFlow(graph, { reserveMaxDepth: false });
    expect(result.positions.output.x).toBe(2 * (result.config.nodeWidth + result.config.columnGapX));
  });

  it("is deterministic for identical input", () => {
    const graph = singleBranch([3, 2, 3], [2, 0]);
    expect(layoutChoiceFlow(graph)).toEqual(layoutChoiceFlow(structuredClone(graph)));
  });

  it("rejects invalid context with Branch, Group and Node identifiers", () => {
    const graph = singleBranch([2, 1], [0]);
    graph.branches[0].groups[0].continueFromNodeId = "missing-node";
    expect(() => layoutChoiceFlow(graph)).toThrow(/Branch "branch-0".*Group "g0".*missing-node/);
    expect(() => layoutChoiceFlow(singleBranch([1, 1, 1]), { maxDepth: 2 })).toThrow(/Branch "branch-0".*maxDepth 2/);
  });

  it("validates 500 seeded constrained graphs without collisions or invalid edges", () => {
    let seed = 0x5eed1234;
    const random = () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
      return seed / 0x1_0000_0000;
    };
    const integer = (min: number, max: number) => min + Math.floor(random() * (max - min + 1));

    for (let iteration = 0; iteration < 500; iteration += 1) {
      const branches = Array.from({ length: integer(1, 5) }, (_, branchIndex) => {
        const groupCount = integer(1, 3);
        const groups = Array.from({ length: groupCount }, (_, groupIndex) => {
          const count = integer(1, 3);
          const nodes = Array.from({ length: count }, (_, nodeIndex) => node(`i${iteration}-b${branchIndex}-g${groupIndex}-n${nodeIndex}`));
          return {
            id: `i${iteration}-b${branchIndex}-g${groupIndex}`,
            nodes,
            continueFromNodeId: groupIndex < groupCount - 1 ? nodes[integer(0, count - 1)].id : undefined,
          };
        });
        const finalNodes = groups.at(-1)!.nodes;
        return {
          id: `i${iteration}-branch-${branchIndex}`,
          order: branchIndex,
          groups,
          terminalNodeId: finalNodes[integer(0, finalNodes.length - 1)].id,
        };
      });
      const graph: ChoiceFlowGraph<TestData> = {
        initialNode: node(`i${iteration}-initial`),
        outputNode: node(`i${iteration}-output`),
        branches,
      };
      assertLayoutInvariants(graph, layoutChoiceFlow(graph));
    }
  });
});
