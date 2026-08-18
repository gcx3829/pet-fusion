import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ChoiceFlowNodeData } from "./node-types";

type OutputFlowNode = Node<ChoiceFlowNodeData, "output">;

export function OutputNode({ data }: NodeProps<OutputFlowNode>) {
  return (
    <article className="choice-flow-node choice-flow-node--output" style={{ width: data.nodeWidth, height: data.nodeHeight }}>
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
      <strong>{data.label}</strong>
      {data.detail && <small>{data.detail}</small>}
    </article>
  );
}

