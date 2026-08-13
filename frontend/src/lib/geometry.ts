import type { PlacementIntent } from "../types";

export const MIN_PLACEMENT_SIZE = 0.08;
const NORMALIZED_PRECISION = 1_000_000;

export function clamp(value: number, min: number, max: number): number {
  const bounded = Math.min(max, Math.max(min, value));
  return Math.round(bounded * NORMALIZED_PRECISION) / NORMALIZED_PRECISION;
}

export function movePlacement(
  placement: PlacementIntent,
  deltaX: number,
  deltaY: number,
): PlacementIntent {
  return {
    ...placement,
    x: clamp(placement.x + deltaX, 0, 1 - placement.width),
    y: clamp(placement.y + deltaY, 0, 1 - placement.height),
  };
}

export function resizePlacement(
  placement: PlacementIntent,
  deltaX: number,
  deltaY: number,
): PlacementIntent {
  return {
    ...placement,
    width: clamp(
      placement.width + deltaX,
      MIN_PLACEMENT_SIZE,
      1 - placement.x,
    ),
    height: clamp(
      placement.height + deltaY,
      MIN_PLACEMENT_SIZE,
      1 - placement.y,
    ),
  };
}

export function updatePlacementNumber(
  placement: PlacementIntent,
  field: "x" | "y" | "width" | "height",
  value: number,
): PlacementIntent {
  const normalizedValue = clamp(value, 0, 1);
  if (field === "width") {
    return resizePlacement(
      placement,
      normalizedValue - placement.width,
      0,
    );
  }
  if (field === "height") {
    return resizePlacement(
      placement,
      0,
      normalizedValue - placement.height,
    );
  }
  if (field === "x") {
    return {
      ...placement,
      x: clamp(normalizedValue, 0, 1 - placement.width),
    };
  }
  return {
    ...placement,
    y: clamp(normalizedValue, 0, 1 - placement.height),
  };
}
