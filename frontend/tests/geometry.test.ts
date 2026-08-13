import { describe, expect, it } from "vitest";
import { movePlacement, resizePlacement, updatePlacementNumber } from "../src/lib/geometry";
import type { PlacementIntent } from "../src/types";

const placement: PlacementIntent = {
  x: 0.6,
  y: 0.55,
  width: 0.2,
  height: 0.3,
  coordinate_space: "normalized",
  pose: "sitting",
  facing: "camera",
  contact_surface: "石阶",
};

describe("placement geometry", () => {
  it("移动时将目标框限制在归一化画布内", () => {
    expect(movePlacement(placement, 0.8, 0.8)).toMatchObject({ x: 0.8, y: 0.7 });
    expect(movePlacement(placement, -2, -2)).toMatchObject({ x: 0, y: 0 });
  });

  it("缩放时保留最小尺寸且不越过画布边缘", () => {
    expect(resizePlacement(placement, 1, 1)).toMatchObject({ width: 0.4, height: 0.45 });
    expect(resizePlacement(placement, -1, -1)).toMatchObject({ width: 0.08, height: 0.08 });
  });

  it("精确坐标输入使用同一套边界规则", () => {
    expect(updatePlacementNumber(placement, "x", 0.95).x).toBe(0.8);
    expect(updatePlacementNumber(placement, "height", 0.9).height).toBe(0.45);
  });
});
