import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GuidanceMaskEditor } from "../src/features/placement/GuidanceMaskEditor";
import type { GuidanceMaskEditorState } from "../src/features/placement/GuidanceMaskEditor";
import type { PlacementIntent } from "../src/types";

const context = {
  clearRect: vi.fn(),
  fillRect: vi.fn(),
  drawImage: vi.fn(),
  getImageData: vi.fn((width: number, height: number) => ({
    data: new Uint8ClampedArray(width * height * 4),
  })),
  putImageData: vi.fn(),
  beginPath: vi.fn(),
  ellipse: vi.fn(),
  stroke: vi.fn(),
  createImageData: vi.fn((width: number, height: number) => ({
    data: new Uint8ClampedArray(width * height * 4),
  })),
};

class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 0;
  }
}

const placement: PlacementIntent = {
  x: 0.58,
  y: 0.61,
  width: 0.18,
  height: 0.29,
  coordinate_space: "normalized",
  pose: "sitting",
  facing: "slightly_left",
  contact_surface: null,
};

describe("GuidanceMaskEditor", () => {
  beforeEach(() => {
    vi.stubGlobal("PointerEvent", TestPointerEvent);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockImplementation(() => context as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({
        width: 100,
        height: 100,
        top: 0,
        left: 0,
        right: 100,
        bottom: 100,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      });
  });

  afterEach(() => vi.restoreAllMocks());

  it("默认 placement seed 可编辑，笔刷 stroke 只改变本地状态且不触发 fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const states: GuidanceMaskEditorState[] = [];
    render(
      <GuidanceMaskEditor
        backgroundSrc="blob:background"
        width={100}
        height={100}
        placement={placement}
        onStateChange={(state) => states.push(state)}
      />,
    );
    const canvas = await screen.findByLabelText("Mask 画布，按住鼠标绘制");

    await waitFor(() => expect(states.at(-1)?.dirty).toBe(false));
    fireEvent.pointerDown(canvas, { pointerId: 1, button: 0, clientX: 20, clientY: 50 });
    fireEvent.pointerUp(canvas, { pointerId: 1, clientX: 80, clientY: 50 });
    await waitFor(() => expect(states.at(-1)?.dirty).toBe(true));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("CUSTOM MASK")).toBeInTheDocument();
  });

  it("手绘后 placement 变化不会静默覆盖，重置才回到当前位置 seed", async () => {
    const states: GuidanceMaskEditorState[] = [];
    const view = render(
      <GuidanceMaskEditor
        backgroundSrc="blob:background"
        width={100}
        height={100}
        placement={placement}
        onStateChange={(state) => states.push(state)}
      />,
    );
    const canvas = await screen.findByLabelText("Mask 画布，按住鼠标绘制");
    fireEvent.pointerDown(canvas, { pointerId: 2, button: 0, clientX: 20, clientY: 50 });
    fireEvent.pointerUp(canvas, { pointerId: 2, clientX: 80, clientY: 50 });
    await waitFor(() => expect(states.at(-1)?.dirty).toBe(true));

    view.rerender(
      <GuidanceMaskEditor
        backgroundSrc="blob:background"
        width={100}
        height={100}
        placement={{ ...placement, x: 0.7 }}
        onStateChange={(state) => states.push(state)}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("不会静默覆盖");
    fireEvent.click(screen.getByRole("button", { name: "重置为当前位置" }));
    await waitFor(() => expect(states.at(-1)?.dirty).toBe(false));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Search 启动后锁定画笔并显示新 Search 提示", async () => {
    const states: GuidanceMaskEditorState[] = [];
    render(
      <GuidanceMaskEditor
        backgroundSrc="blob:background"
        width={100}
        height={100}
        placement={placement}
        locked
        onStateChange={(state) => states.push(state)}
      />,
    );
    const canvas = await screen.findByLabelText("Mask 画布，按住鼠标绘制");
    expect(screen.getByText(/Guidance Mask 已锁定/)).toBeInTheDocument();
    expect(canvas).toHaveAttribute("aria-disabled", "true");
    fireEvent.pointerDown(canvas, { pointerId: 3, button: 0, clientX: 20, clientY: 50 });
    fireEvent.pointerUp(canvas, { pointerId: 3, clientX: 80, clientY: 50 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(states.at(-1)?.dirty).toBe(false);
  });
});
