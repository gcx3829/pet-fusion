import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ReactFlow,
  type Node as FlowNode,
  type NodeProps,
  type ReactFlowInstance,
  type Viewport,
} from "@xyflow/react";

export const EDITOR_MIN_ZOOM = 0.25;
export const EDITOR_MAX_ZOOM = 4;

/**
 * Timeline and the main worker intentionally use the same trackpad contract:
 * two-finger scroll pans, pinch zooms, and double-click never changes zoom.
 */
export const SHARED_VIEWPORT_GESTURES = {
  panOnScroll: true,
  zoomOnScroll: false,
  zoomOnPinch: true,
  zoomOnDoubleClick: false,
  preventScrolling: true,
} as const;

interface WorkerStageData extends Record<string, unknown> {
  children: ReactNode;
}

type WorkerStageNode = FlowNode<WorkerStageData, "worker-stage">;

function WorkerStage({ data }: NodeProps<WorkerStageNode>) {
  return <div className="worker-gesture-node">{data.children}</div>;
}

const nodeTypes = { "worker-stage": WorkerStage };

interface EditorGestureViewportProps {
  children: ReactNode;
  zoom: number;
  panEnabled: boolean;
  onZoomChange?: (zoom: number) => void;
}

export function EditorGestureViewport({ children, zoom, panEnabled, onZoomChange }: EditorGestureViewportProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<ReactFlowInstance<WorkerStageNode> | null>(null);
  const initializedRef = useRef(false);
  const gestureZoomRef = useRef<number | null>(null);
  const [size, setSize] = useState({ width: 1, height: 1 });
  const [moving, setMoving] = useState(false);
  const scale = Math.min(EDITOR_MAX_ZOOM, Math.max(EDITOR_MIN_ZOOM, zoom / 100));

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const measure = () => {
      const bounds = host.getBoundingClientRect();
      const width = Math.max(1, Math.round(bounds.width));
      const height = Math.max(1, Math.round(bounds.height));
      setSize((current) => current.width === width && current.height === height
        ? current
        : { width, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const instance = instanceRef.current;
    if (!instance || !initializedRef.current) return;
    if (gestureZoomRef.current === zoom) {
      gestureZoomRef.current = null;
      return;
    }
    const current = instance.getViewport();
    if (Math.abs(current.zoom - scale) < 0.001) return;
    void instance.zoomTo(scale, { duration: 120 });
  }, [scale, zoom]);

  const nodes = useMemo<WorkerStageNode[]>(() => [{
    id: "worker-stage",
    type: "worker-stage",
    position: { x: 0, y: 0 },
    width: size.width,
    height: size.height,
    draggable: false,
    selectable: false,
    focusable: false,
    style: { width: size.width, height: size.height },
    data: { children },
  }], [children, size.height, size.width]);

  const commitZoom = (viewport: Viewport) => {
    setMoving(false);
    const nextZoom = Math.min(400, Math.max(25, Math.round(viewport.zoom * 20) * 5));
    if (nextZoom === zoom) return;
    gestureZoomRef.current = nextZoom;
    onZoomChange?.(nextZoom);
  };

  return (
    <div
      ref={hostRef}
      className={`worker-gesture-flow ${panEnabled ? "is-pan-enabled" : ""} ${moving ? "is-panning" : ""}`}
      data-gesture-provider="xyflow"
      aria-label="主工作区画布"
    >
      <ReactFlow<WorkerStageNode>
        nodes={nodes}
        edges={[]}
        nodeTypes={nodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={panEnabled ? [0, 1] : false}
        minZoom={EDITOR_MIN_ZOOM}
        maxZoom={EDITOR_MAX_ZOOM}
        defaultViewport={{ x: 0, y: 0, zoom: scale }}
        onInit={(instance) => {
          instanceRef.current = instance;
          initializedRef.current = true;
          const x = size.width * (1 - scale) / 2;
          const y = size.height * (1 - scale) / 2;
          void instance.setViewport({ x, y, zoom: scale });
        }}
        onMoveStart={() => setMoving(true)}
        onMoveEnd={(_event, viewport) => commitZoom(viewport)}
        onNodeClick={() => undefined}
        proOptions={{ hideAttribution: true }}
        {...SHARED_VIEWPORT_GESTURES}
      />
    </div>
  );
}
