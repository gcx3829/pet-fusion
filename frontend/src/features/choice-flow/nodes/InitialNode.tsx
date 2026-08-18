import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ChoiceFlowNodeData } from "./node-types";

type InitialFlowNode = Node<ChoiceFlowNodeData, "initial">;

export function InitialNode({ data }: NodeProps<InitialFlowNode>) {
  return (
    <article className="choice-flow-node choice-flow-node--initial" style={{ width: data.nodeWidth, height: data.nodeHeight }}>
      <strong>{data.label}</strong>
      {data.detail && <small>{data.detail}</small>}
      <Handle type="source" position={Position.Right} id="out" isConnectable={false} />
    </article>
  );
}

