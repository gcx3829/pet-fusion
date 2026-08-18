export interface FlowNodeData extends Record<string, unknown> {
  label: string;
  detail?: string;
}

export interface FlowNodeSpec<TData extends FlowNodeData = FlowNodeData> {
  id: string;
  data: TData;
}

export interface ChoiceGroup<TData extends FlowNodeData = FlowNodeData> {
  id: string;
  nodes: FlowNodeSpec<TData>[];
  continueFromNodeId?: string;
}

export interface FlowBranch<TData extends FlowNodeData = FlowNodeData> {
  id: string;
  order: number;
  groups: ChoiceGroup<TData>[];
  terminalNodeId: string;
}

export interface ChoiceFlowGraph<TData extends FlowNodeData = FlowNodeData> {
  initialNode: FlowNodeSpec<TData>;
  outputNode: FlowNodeSpec<TData>;
  branches: FlowBranch<TData>[];
}

export interface LayoutConfig {
  nodeWidth: number;
  nodeHeight: number;
  nodeGapY: number;
  columnGapX: number;
  branchGapY: number;
  maxDepth: number;
  reserveMaxDepth: boolean;
  alignNextGroupToContinuation: boolean;
}

export interface FlowPosition {
  x: number;
  y: number;
}

export interface FlowBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
  width: number;
  height: number;
}

export interface PositionedFlowNode<TData extends FlowNodeData = FlowNodeData> extends FlowNodeSpec<TData> {
  role: "initial" | "choice" | "output";
  position: FlowPosition;
}

export interface FlowEdgeSpec {
  id: string;
  source: string;
  target: string;
  sourceHandle: "out";
  targetHandle: "in";
  type: "straight" | "default";
}

export interface BranchLayoutBounds extends FlowBounds {
  branchId: string;
}

export interface LayoutResult<TData extends FlowNodeData = FlowNodeData> {
  nodes: PositionedFlowNode<TData>[];
  edges: FlowEdgeSpec[];
  positions: Record<string, FlowPosition>;
  branchBounds: BranchLayoutBounds[];
  bounds: FlowBounds;
  config: LayoutConfig;
}

export interface LocalBranchLayout<TData extends FlowNodeData = FlowNodeData> {
  branch: FlowBranch<TData>;
  positions: Record<string, FlowPosition>;
  bounds: FlowBounds;
}
