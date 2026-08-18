import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { ChoiceFlowNodeData } from "./node-types";

type ChoiceFlowNode = Node<ChoiceFlowNodeData, "choice">;

export function ChoiceNode({ data }: NodeProps<ChoiceFlowNode>) {
  const content = (
    <>
      {data.imageUrl && <img src={data.imageUrl} alt="" />}
      <span><strong>{data.label}</strong>{data.detail && <small>{data.detail}</small>}</span>
      {!!data.badges?.length && <em>{data.badges.join(" · ")}</em>}
    </>
  );
  return (
    <article
      className={`choice-flow-node choice-flow-node--choice ${data.selected ? "is-selected" : ""}`}
      style={{ width: data.nodeWidth, height: data.nodeHeight }}
    >
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
      {data.onActivate ? <button type="button" onClick={data.onActivate}>{content}</button> : content}
      <Handle type="source" position={Position.Right} id="out" isConnectable={false} />
    </article>
  );
}

