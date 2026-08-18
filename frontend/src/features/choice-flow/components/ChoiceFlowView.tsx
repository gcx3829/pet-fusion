import { useMemo } from "react";
import { Background, Controls, ReactFlow, type Edge, type Node, type NodeTypes } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { layoutChoiceFlow } from "../layout/layout-choice-flow";
import type { ChoiceFlowGraph, FlowNodeData, LayoutConfig } from "../layout/types";
import { ChoiceNode } from "../nodes/ChoiceNode";
import { InitialNode } from "../nodes/InitialNode";
import type { ChoiceFlowNodeData } from "../nodes/node-types";
import { OutputNode } from "../nodes/OutputNode";

const nodeTypes: NodeTypes = { initial: InitialNode, choice: ChoiceNode, output: OutputNode };

export interface ChoiceFlowViewProps<TData extends FlowNodeData> {
  graph: ChoiceFlowGraph<TData>;
  config?: Partial<LayoutConfig>;
  className?: string;
  fitViewPadding?: number;
  onNodeActivate?: (nodeId: string) => void;
}

export function ChoiceFlowView<TData extends FlowNodeData>({
  graph,
  config,
  className = "",
  fitViewPadding = 0.16,
  onNodeActivate,
}: ChoiceFlowViewProps<TData>) {
  const layout = useMemo(() => layoutChoiceFlow(graph, config), [config, graph]);
  const nodes = useMemo<Node<ChoiceFlowNodeData>[]>(() => layout.nodes.map((node) => ({
    id: node.id,
    type: node.role,
    position: node.position,
    width: layout.config.nodeWidth,
    height: layout.config.nodeHeight,
    style: { width: layout.config.nodeWidth, height: layout.config.nodeHeight },
    draggable: false,
    connectable: false,
    selectable: false,
    focusable: false,
    data: {
      ...node.data,
      nodeWidth: layout.config.nodeWidth,
      nodeHeight: layout.config.nodeHeight,
      onActivate: onNodeActivate ? () => onNodeActivate(node.id) : undefined,
    },
  })), [layout, onNodeActivate]);
  const edges = useMemo<Edge[]>(() => layout.edges.map((edge) => ({
    ...edge,
    selectable: false,
    focusable: false,
  })), [layout.edges]);

  return (
    <div className={`choice-flow-view ${className}`.trim()}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        nodeOrigin={[0.5, 0.5]}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        fitView
        fitViewOptions={{ padding: fitViewPadding }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={24} size={1} color="rgba(148, 163, 184, 0.12)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
