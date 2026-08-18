import type { LayoutConfig } from "./types";

export const DEFAULT_LAYOUT_CONFIG: Readonly<LayoutConfig> = Object.freeze({
  nodeWidth: 220,
  nodeHeight: 72,
  nodeGapY: 28,
  columnGapX: 140,
  branchGapY: 96,
  maxDepth: 3,
  reserveMaxDepth: true,
  alignNextGroupToContinuation: true,
});

export function resolveLayoutConfig(config: Partial<LayoutConfig> = {}): LayoutConfig {
  return { ...DEFAULT_LAYOUT_CONFIG, ...config };
}

