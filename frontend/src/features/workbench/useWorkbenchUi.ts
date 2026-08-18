import { useCallback, useEffect, useMemo, useState } from "react";

export type WorkerMode = "create" | "generation" | "fusion";
export type SidebarTab = "assets" | "prompt" | "review" | "fusion";
export type AssetLayout = "list" | "grid";
export type BrushTool = "hand" | "paint" | "erase";

export interface WorkbenchUiState {
  workerMode: WorkerMode;
  sidebarTab: SidebarTab;
  assetLayout: AssetLayout;
  zoom: number;
  selectedNodeId: string | null;
  brushTool: BrushTool;
  brushSize: number;
  brushFlow: number;
  brushFeather: number;
}

export interface WorkbenchUiActions {
  setWorkerMode: (mode: WorkerMode) => void;
  setSidebarTab: (tab: SidebarTab) => void;
  setAssetLayout: (layout: AssetLayout) => void;
  setZoom: (zoom: number) => void;
  setSelectedNodeId: (nodeId: string | null) => void;
  setBrushTool: (tool: BrushTool) => void;
  setBrushSize: (size: number) => void;
  setBrushFlow: (flow: number) => void;
  setBrushFeather: (feather: number) => void;
  enterFusion: () => void;
}

export interface UseWorkbenchUiOptions {
  hasSearch: boolean;
  accepted: boolean;
  roundIndex?: number;
}

export const initialWorkbenchUiState: WorkbenchUiState = {
  workerMode: "create",
  sidebarTab: "assets",
  assetLayout: "list",
  zoom: 100,
  selectedNodeId: null,
  brushTool: "hand",
  brushSize: 72,
  brushFlow: 72,
  brushFeather: 24,
};

export function isFusionUnlocked(accepted: boolean): boolean {
  return accepted;
}

export function createWorkbenchUiState(overrides: Partial<WorkbenchUiState> = {}): WorkbenchUiState {
  return { ...initialWorkbenchUiState, ...overrides };
}

function clampZoom(value: number): number {
  return Math.min(400, Math.max(25, Math.round(value / 5) * 5));
}

/**
 * UI-only state for the editor shell.  It intentionally knows nothing about
 * API calls; App remains the owner of orchestration and durable data.
 */
export function useWorkbenchUi({ hasSearch, accepted, roundIndex }: UseWorkbenchUiOptions): [WorkbenchUiState, WorkbenchUiActions] {
  const [state, setState] = useState<WorkbenchUiState>(initialWorkbenchUiState);

  useEffect(() => {
    setState((current) => {
      const workerMode: WorkerMode = !hasSearch
        ? "create"
        : current.workerMode === "fusion" && !accepted
          ? "generation"
          : current.workerMode === "create"
            ? "generation"
            : current.workerMode;
      const sidebarTab: SidebarTab = current.sidebarTab === "fusion" && !accepted
        ? "assets"
        : current.sidebarTab;
      const zoom = current.workerMode === "create" && workerMode === "generation"
        ? 100
        : current.zoom;
      return workerMode === current.workerMode && sidebarTab === current.sidebarTab && zoom === current.zoom
        ? current
        : { ...current, workerMode, sidebarTab, zoom };
    });
  }, [accepted, hasSearch]);

  useEffect(() => {
    // A new automatic round is a new review context. Historical nodes remain
    // inspectable, but no prior selection becomes the next-round input.
    setState((current) => current.selectedNodeId === null
      ? current
      : { ...current, selectedNodeId: null });
  }, [roundIndex]);

  const setWorkerMode = useCallback((mode: WorkerMode) => {
    setState((current) => {
      if (mode === "fusion" && !accepted) return current;
      if (mode === "generation" && !hasSearch) return current;
      return { ...current, workerMode: mode };
    });
  }, [accepted, hasSearch]);

  const setSidebarTab = useCallback((sidebarTab: SidebarTab) => {
    setState((current) => sidebarTab === "fusion" && !accepted
      ? current
      : { ...current, sidebarTab });
  }, [accepted]);

  const enterFusion = useCallback(() => {
    if (!accepted) return;
    setState((current) => ({ ...current, workerMode: "fusion", sidebarTab: "fusion" }));
  }, [accepted]);

  const actions = useMemo<WorkbenchUiActions>(() => ({
    setWorkerMode,
    setSidebarTab,
    setAssetLayout: (assetLayout) => setState((current) => ({ ...current, assetLayout })),
    setZoom: (zoom) => setState((current) => {
      const nextZoom = clampZoom(zoom);
      return nextZoom === current.zoom ? current : { ...current, zoom: nextZoom };
    }),
    setSelectedNodeId: (selectedNodeId) => setState((current) => ({ ...current, selectedNodeId })),
    setBrushTool: (brushTool) => setState((current) => ({ ...current, brushTool })),
    setBrushSize: (brushSize) => setState((current) => ({ ...current, brushSize: Math.round(Math.max(4, Math.min(512, brushSize))) })),
    setBrushFlow: (brushFlow) => setState((current) => ({ ...current, brushFlow: Math.max(1, Math.min(100, Math.round(brushFlow))) })),
    setBrushFeather: (brushFeather) => setState((current) => ({ ...current, brushFeather: Math.max(0, Math.min(100, Math.round(brushFeather))) })),
    enterFusion,
  }), [enterFusion, setSidebarTab, setWorkerMode]);

  return [state, actions];
}
