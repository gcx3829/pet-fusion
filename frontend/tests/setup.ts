import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

if (!URL.createObjectURL) {
  URL.createObjectURL = vi.fn(() => "blob:test-preview");
}

if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = vi.fn();
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom deliberately leaves Canvas unimplemented. Keep a stable harmless
// baseline so tests in parallel files can temporarily spy on getContext and
// restore it without exposing another still-rendering component to jsdom's
// noisy "not implemented" fallback.
const defaultCanvasContext = {
  beginPath() {},
  bezierCurveTo() {},
  clearRect() {},
  closePath() {},
  createImageData(width: number, height: number) {
    return { data: new Uint8ClampedArray(width * height * 4), width, height };
  },
  drawImage() {},
  ellipse() {},
  fill() {},
  fillRect() {},
  lineTo() {},
  moveTo() {},
  putImageData() {},
  setLineDash() {},
  stroke() {},
};
Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
  configurable: true,
  value: () => defaultCanvasContext,
  writable: true,
});
