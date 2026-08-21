import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Handle, MarkerType, Position, ReactFlow, type Edge as FlowEdge, type Node as FlowNode, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { SearchEvent, SearchSnapshot } from "../../types";
import { buildTimelineGraph, type TimelineMedia, type TimelineNode } from "./buildTimelineGraph";
import { layoutTimelineGraph } from "./timelineLayout";
import { EDITOR_MAX_ZOOM, EDITOR_MIN_ZOOM, SHARED_VIEWPORT_GESTURES } from "../workbench/EditorGestureViewport";

interface TimelineCanvasProps extends TimelineMedia {
  events: SearchEvent[];
  snapshot?: SearchSnapshot | null;
  selectedNodeId?: string | null;
  onSelectCandidate?: (candidateId: string) => void;
  onSelectSource?: () => void;
  onSelectFinal?: (candidateId: string) => void;
}

interface PhotoNodeData extends Record<string, unknown> {
  node: TimelineNode;
  selected: boolean;
  onActivate: (node: TimelineNode) => void;
  onPreview: (preview: TimelinePreviewState | null) => void;
}
type PhotoFlowNode = FlowNode<PhotoNodeData, "photo">;

interface TimelinePreviewState {
  id: string;
  anchor: { top: number; left: number; right: number };
  focus: { x: number; y: number };
}

function previewState(nodeId: string, element: HTMLElement, x = 0.5, y = 0.5): TimelinePreviewState {
  const bounds = element.getBoundingClientRect();
  return {
    id: nodeId,
    anchor: { top: bounds.top, left: bounds.left, right: bounds.right },
    focus: {
      x: Math.min(1, Math.max(0, x)),
      y: Math.min(1, Math.max(0, y)),
    },
  };
}

function PhotoNode({ data }: NodeProps<PhotoFlowNode>) {
  const { node, selected, onActivate, onPreview } = data;
  return <>
    {node.kind !== "source" && <Handle type="target" position={Position.Left} id="in" isConnectable={false} />}
    <button
      type="button"
      className={`timeline-photo-node is-${node.kind} is-${node.status} ${selected ? "is-selected" : ""} nodrag nopan`}
      aria-label={`${node.label} ${node.detail ?? ""}`}
      aria-pressed={selected}
      disabled={node.placeholder}
      onPointerEnter={(event) => onPreview(previewState(node.id, event.currentTarget))}
      onPointerMove={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect();
        onPreview(previewState(
          node.id,
          event.currentTarget,
          bounds.width ? (event.clientX - bounds.left) / bounds.width : 0.5,
          bounds.height ? (event.clientY - bounds.top) / bounds.height : 0.5,
        ));
      }}
      onPointerLeave={() => onPreview(null)}
      onFocus={(event) => onPreview(previewState(node.id, event.currentTarget))}
      onBlur={() => onPreview(null)}
      data-timeline-node-id={node.id}
      onClick={(event) => {
        // Mouse/pointer selection is delegated to React Flow's node handler.
        // Keeping the keyboard-generated click here preserves the
        // native button interaction without firing the selection twice.
        if (event.detail === 0) {
          event.stopPropagation();
          onActivate(node);
        }
      }}
    >
      <img src={node.imageUrl} alt="" />
      {node.kind === "source" && node.badges.includes("引导") && <span className="timeline-mask-overlay" />}
      <span className="timeline-photo-meta"><strong>{node.label}</strong><small>{node.detail}</small></span>
      <span className="timeline-photo-badges">{node.badges.map((badge) => <i key={badge}>{badge}</i>)}</span>
      {node.progress && <span className="timeline-progress" aria-label="处理中"><b /><b /><b /></span>}
      {typeof node.score === "number" && <output>{node.score.toFixed(1)}</output>}
    </button>
    {node.kind !== "final" && <Handle type="source" position={Position.Right} id="out" isConnectable={false} />}
  </>;
}

const nodeTypes = { photo: PhotoNode };

export function TimelineCanvas({ events, snapshot, selectedNodeId, onSelectCandidate, onSelectSource, onSelectFinal, sourceImageUrl, guidanceActive, fusionImageUrl, acceptedCandidateId, expectedCandidateCount, generationActive, currentRound }: TimelineCanvasProps) {
  const media = useMemo(() => ({ sourceImageUrl, guidanceActive, fusionImageUrl, acceptedCandidateId, expectedCandidateCount, generationActive, currentRound }), [acceptedCandidateId, currentRound, expectedCandidateCount, fusionImageUrl, generationActive, guidanceActive, sourceImageUrl]);
  const graph = useMemo(() => buildTimelineGraph(events, snapshot, media), [events, media, snapshot]);
  const layout = useMemo(() => layoutTimelineGraph(graph), [graph]);
  const [preview, setPreview] = useState<TimelinePreviewState | null>(null);
  const activate = (node: TimelineNode) => node.kind === "source"
    ? onSelectSource?.()
    : node.kind === "final" && node.candidateId
      ? onSelectFinal?.(node.candidateId)
      : node.candidateId && onSelectCandidate?.(node.candidateId);
  const nodes = useMemo<PhotoFlowNode[]>(() => graph.nodes.map((node) => {
    const position = layout.positions[node.id] ?? { x: 0, y: 0, width: 132, height: 94 };
    return { id: node.id, type: "photo", position: { x: position.x, y: position.y }, width: position.width, height: position.height, draggable: false, selectable: false, focusable: false, data: { node, selected: node.id === selectedNodeId, onActivate: activate, onPreview: setPreview } };
  }), [graph.nodes, layout.positions, onSelectCandidate, onSelectFinal, onSelectSource, selectedNodeId]);
  const relationByEdgeId = useMemo(() => new Map(
    graph.edges.map((edge) => [`${edge.from}__${edge.to}`, edge.relation]),
  ), [graph.edges]);
  const edges = useMemo<FlowEdge[]>(() => layout.edges.map((edge) => {
    const relation = relationByEdgeId.get(edge.id) ?? "continue";
    const color = relation === "accept" ? "#36b37e" : relation === "continue" ? "#4c9aff" : "#8b96a8";
    return {
      ...edge,
      selectable: false,
      focusable: false,
      animated: false,
      zIndex: 1,
      style: { stroke: color, strokeWidth: 2, strokeDasharray: relation === "rebase" ? "6 4" : undefined, opacity: 1 },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
    };
  }), [layout.edges, relationByEdgeId]);
  const hovered = graph.nodes.find((node) => node.id === preview?.id);

  return <div className="timeline-flow" aria-label="照片生成时间线" data-gesture-provider="xyflow">
    <ReactFlow<PhotoFlowNode, FlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      nodeOrigin={[0.5, 0.5]}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      onNodeClick={(_event, flowNode) => {
        if (!flowNode.data.node.placeholder) activate(flowNode.data.node);
      }}
      minZoom={EDITOR_MIN_ZOOM}
      maxZoom={EDITOR_MAX_ZOOM}
      fitView
      fitViewOptions={{ padding: 0.08, minZoom: 0.55, maxZoom: 1 }}
      proOptions={{ hideAttribution: true }}
      {...SHARED_VIEWPORT_GESTURES}
    />
    {hovered && preview && typeof document !== "undefined" && createPortal(
      <div
        className="timeline-preview-popover"
        role="status"
        style={{
          left: Math.min(Math.max(12, preview.anchor.left), Math.max(12, window.innerWidth - 452)),
          top: Math.max(12, preview.anchor.top - 338),
        }}
      >
        <div className="timeline-preview-zoom" aria-label="节点局部放大预览">
          <img
            src={hovered.imageUrl}
            alt=""
            style={{ transform: `translate(${-preview.focus.x * 54.55}%, ${-preview.focus.y * 54.55}%)` }}
          />
        </div>
        <div className="timeline-preview-footer">
          <div className="timeline-preview-navigator" aria-hidden="true">
            <img src={hovered.imageUrl} alt="" />
            <i style={{ left: `${20 + preview.focus.x * 60}%`, top: `${20 + preview.focus.y * 60}%` }} />
          </div>
          <span><strong>{hovered.label}</strong><small>{hovered.detail}</small></span>
          <b>{Math.round(preview.focus.x * 100)} / {Math.round(preview.focus.y * 100)}</b>
        </div>
      </div>,
      document.body,
    )}
  </div>;
}
