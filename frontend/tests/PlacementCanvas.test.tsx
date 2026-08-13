import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PlacementCanvas } from "../src/features/placement/PlacementCanvas";
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

describe("PlacementCanvas", () => {
  it("可用键盘移动目标框", () => {
    const onChange = vi.fn();
    render(
      <PlacementCanvas
        backgroundUrl="blob:background"
        value={placement}
        onChange={onChange}
      />,
    );

    fireEvent.keyDown(screen.getByRole("group", { name: /宠物目标框/ }), { key: "ArrowLeft" });

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ x: 0.59, y: 0.55 }));
  });

  it("提供不依赖拖拽的精确表单控制", () => {
    const onChange = vi.fn();
    render(
      <PlacementCanvas
        backgroundUrl="blob:background"
        value={placement}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByText("精确坐标"));
    const widthInput = screen.getByRole("spinbutton", { name: "WIDTH" });
    fireEvent.change(widthInput, { target: { value: "25" } });

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ width: 0.25 }));
  });
});
