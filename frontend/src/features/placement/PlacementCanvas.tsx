import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "../../components/Icon";
import { clamp, movePlacement, resizePlacement, updatePlacementNumber } from "../../lib/geometry";
import type { Facing, PlacementIntent, Pose } from "../../types";

interface PlacementCanvasProps {
  backgroundUrl: string | null;
  value: PlacementIntent;
  onChange: (value: PlacementIntent) => void;
  disabled?: boolean;
}

interface PointerInteraction {
  pointerId: number;
  mode: "move" | "resize";
  lastX: number;
  lastY: number;
}

const poses: { value: Pose; label: string }[] = [
  { value: "sitting", label: "坐姿" },
  { value: "standing", label: "站立" },
  { value: "lying", label: "趴卧" },
  { value: "walking", label: "行走" },
];

const facings: { value: Facing; label: string }[] = [
  { value: "camera", label: "看向镜头" },
  { value: "slightly_left", label: "略向左" },
  { value: "slightly_right", label: "略向右" },
  { value: "left", label: "向左" },
  { value: "right", label: "向右" },
  { value: "away", label: "背向镜头" },
];

export function PlacementCanvas({
  backgroundUrl,
  value,
  onChange,
  disabled = false,
}: PlacementCanvasProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const interactionRef = useRef<PointerInteraction | null>(null);
  const [imageSize, setImageSize] = useState({ width: 3, height: 2 });
  const [showMasks, setShowMasks] = useState(true);
  const maskToggleId = useId();

  useEffect(() => {
    if (!backgroundUrl) return;
    const image = new Image();
    image.onload = () => {
      if (image.naturalWidth && image.naturalHeight) {
        setImageSize({ width: image.naturalWidth, height: image.naturalHeight });
      }
    };
    image.src = backgroundUrl;
  }, [backgroundUrl]);

  const canvasHeight = 1000 * imageSize.height / imageSize.width;
  const x = value.x * 1000;
  const y = value.y * canvasHeight;
  const width = value.width * 1000;
  const height = value.height * canvasHeight;
  const modelX = clamp(value.x - 0.045, 0, 1) * 1000;
  const modelY = clamp(value.y - 0.065, 0, 1) * canvasHeight;
  const modelRight = clamp(value.x + value.width + 0.045, 0, 1) * 1000;
  const modelBottom = clamp(value.y + value.height + 0.065, 0, 1) * canvasHeight;

  const startPointer = (
    event: React.PointerEvent<SVGGElement | SVGCircleElement>,
    mode: PointerInteraction["mode"],
  ) => {
    if (disabled) return;
    event.preventDefault();
    interactionRef.current = {
      pointerId: event.pointerId,
      mode,
      lastX: event.clientX,
      lastY: event.clientY,
    };
    svgRef.current?.setPointerCapture(event.pointerId);
  };

  const movePointer = (event: React.PointerEvent<SVGSVGElement>) => {
    const interaction = interactionRef.current;
    const svg = svgRef.current;
    if (!interaction || !svg || interaction.pointerId !== event.pointerId) return;
    const bounds = svg.getBoundingClientRect();
    const deltaX = (event.clientX - interaction.lastX) / bounds.width;
    const deltaY = (event.clientY - interaction.lastY) / bounds.height;
    interaction.lastX = event.clientX;
    interaction.lastY = event.clientY;
    onChange(interaction.mode === "move"
      ? movePlacement(value, deltaX, deltaY)
      : resizePlacement(value, deltaX, deltaY));
  };

  const stopPointer = (event: React.PointerEvent<SVGSVGElement>) => {
    if (interactionRef.current?.pointerId === event.pointerId) {
      interactionRef.current = null;
      if (svgRef.current?.hasPointerCapture(event.pointerId)) {
        svgRef.current.releasePointerCapture(event.pointerId);
      }
    }
  };

  const handleKeyboard = (event: React.KeyboardEvent<SVGGElement>) => {
    if (disabled || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const step = event.shiftKey ? 0.03 : 0.01;
    const deltaX = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
    const deltaY = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
    onChange(movePlacement(value, deltaX, deltaY));
  };

  return (
    <section className="panel placement-panel" aria-labelledby="placement-heading">
      <div className="panel-heading placement-heading-row">
        <div>
          <h2 id="placement-heading">位置与姿态</h2>
        </div>
        <label className="switch-label" htmlFor={maskToggleId}>
          <input
            id={maskToggleId}
            type="checkbox"
            checked={showMasks}
            onChange={(event) => setShowMasks(event.target.checked)}
          />
          <span className="switch-track"><span /></span>
          显示引导区域
        </label>
      </div>

      <div className={`placement-stage ${backgroundUrl ? "has-image" : "is-empty"}`}>
        {backgroundUrl ? (
          <svg
            ref={svgRef}
            className="placement-svg"
            style={{ aspectRatio: `${imageSize.width} / ${imageSize.height}` }}
            viewBox={`0 0 1000 ${canvasHeight}`}
            role="img"
            aria-label="旅行照片位置画布；可拖动目标框，拖动右下角调整大小"
            onPointerMove={movePointer}
            onPointerUp={stopPointer}
            onPointerCancel={stopPointer}
          >
            <image href={backgroundUrl} x="0" y="0" width="1000" height={canvasHeight} preserveAspectRatio="none" />
            <rect className="canvas-vignette" x="0" y="0" width="1000" height={canvasHeight} />
            {showMasks && (
              <>
                <rect
                  className="model-mask"
                  x={modelX}
                  y={modelY}
                  width={modelRight - modelX}
                  height={modelBottom - modelY}
                  rx="12"
                />
              </>
            )}
            <g
              className={`placement-target ${disabled ? "is-disabled" : ""}`}
              role="group"
              aria-label={`宠物目标框，横向 ${Math.round(value.x * 100)}%，纵向 ${Math.round(value.y * 100)}%`}
              tabIndex={disabled ? -1 : 0}
              onKeyDown={handleKeyboard}
              onPointerDown={(event) => startPointer(event, "move")}
            >
              <rect className="target-fill" x={x} y={y} width={width} height={height} rx="6" />
              <path className="target-corners" d={`M${x} ${y + 32}V${y}H${x + 32} M${x + width - 32} ${y}H${x + width}V${y + 32} M${x + width} ${y + height - 32}V${y + height}H${x + width - 32} M${x + 32} ${y + height}H${x}V${y + height - 32}`} />
              <line className="target-axis" x1={x + width / 2} y1={y + height * .72} x2={x + width / 2} y2={y + height + 32} />
              <ellipse className="target-contact" cx={x + width / 2} cy={y + height} rx={Math.max(18, width * .28)} ry="8" />
              <g className="target-label" transform={`translate(${x}, ${Math.max(32, y - 26)})`}>
                <rect x="0" y="-24" width="128" height="28" rx="2" />
                <text x="10" y="-6">宠物位置</text>
              </g>
            </g>
            <circle
              className="resize-handle"
              aria-label="拖动调整宠物目标框大小"
              cx={x + width}
              cy={y + height}
              r="13"
              onPointerDown={(event) => {
                event.stopPropagation();
                startPointer(event, "resize");
              }}
            />
          </svg>
        ) : (
          <div className="empty-canvas">
            <span><Icon name="image" /></span>
            <strong>等待旅行原片</strong>
            <p>装入原片后，在画面中为宠物安排位置。</p>
          </div>
        )}
        {backgroundUrl && (
          <div className="canvas-legend" aria-hidden="true">
            <span><i className="legend-target" />目标边界</span>
            {showMasks && <span><i className="legend-model" />引导区域 · 发给模型</span>}
            <span className="legend-fusion-note"><i className="legend-fusion" />局部融合 · 接受候选后可用</span>
          </div>
        )}
      </div>

      <div className="mask-contract-note" role="note">
        <div>
          <strong>引导区域不会锁住原图</strong>
          <span>它只提示图像模型重点修改哪里。评分和审片始终使用模型的原始候选图。</span>
        </div>
        <small>用下方画笔标出希望模型重点修改的区域。接受候选后，还可以单独做局部融合。</small>
      </div>

      <fieldset className="placement-fields" disabled={disabled || !backgroundUrl}>
        <legend className="sr-only">宠物位置与姿态参数</legend>
        <label className="select-field">
          <span>姿态</span>
          <span className="select-wrap">
            <select value={value.pose} onChange={(event) => onChange({ ...value, pose: event.target.value as Pose })}>
              {poses.map((pose) => <option key={pose.value} value={pose.value}>{pose.label}</option>)}
            </select>
            <Icon name="chevron" />
          </span>
        </label>
        <label className="select-field">
          <span>朝向</span>
          <span className="select-wrap">
            <select value={value.facing} onChange={(event) => onChange({ ...value, facing: event.target.value as Facing })}>
              {facings.map((facing) => <option key={facing.value} value={facing.value}>{facing.label}</option>)}
            </select>
            <Icon name="chevron" />
          </span>
        </label>
        <label className="text-field contact-field">
          <span>接触面</span>
          <input
            value={value.contact_surface ?? ""}
            maxLength={120}
            placeholder="例如：石阶、沙地"
            onChange={(event) => onChange({ ...value, contact_surface: event.target.value || null })}
          />
        </label>
      </fieldset>

      <details className="precision-controls">
        <summary>精确坐标 <span>键盘方向键亦可移动</span></summary>
        <div className="coordinate-grid">
          {(["x", "y", "width", "height"] as const).map((field) => (
            <label key={field}>
              <span>{field.toUpperCase()}</span>
              <input
                aria-label={field.toUpperCase()}
                type="number"
                min="0"
                max="100"
                step="1"
                disabled={disabled || !backgroundUrl}
                value={Math.round(value[field] * 100)}
                onChange={(event) => onChange(updatePlacementNumber(value, field, Number(event.target.value) / 100))}
              />
              <small>%</small>
            </label>
          ))}
        </div>
      </details>
    </section>
  );
}
