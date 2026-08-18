import type { FlowNodeData } from "../layout/types";

export interface ChoiceFlowNodeData extends FlowNodeData {
  nodeWidth: number;
  nodeHeight: number;
  imageUrl?: string;
  badges?: string[];
  selected?: boolean;
  onActivate?: () => void;
}

