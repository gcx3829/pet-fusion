import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChoiceFlowView } from "../src/features/choice-flow/components/ChoiceFlowView";
import type { ChoiceFlowGraph, FlowNodeData } from "../src/features/choice-flow/layout/types";

interface ViewData extends FlowNodeData { imageUrl?: string }

describe("ChoiceFlowView", () => {
  it("renders fixed-role handles and disables graph editing", () => {
    const graph: ChoiceFlowGraph<ViewData> = {
      initialNode: { id: "initial", data: { label: "Source" } },
      outputNode: { id: "output", data: { label: "Output" } },
      branches: [{
        id: "branch",
        order: 0,
        terminalNodeId: "choice",
        groups: [{
          id: "group",
          nodes: [{ id: "choice", data: { label: "Candidate", imageUrl: "/candidate.png" } }],
        }],
      }],
    };
    const onActivate = vi.fn();
    const { container } = render(
      <div style={{ width: 900, height: 400 }}>
        <ChoiceFlowView graph={graph} onNodeActivate={onActivate} />
      </div>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Candidate/ }));
    expect(onActivate).toHaveBeenCalledWith("choice");
    expect(container.querySelector('[data-id="initial"] [data-handleid="out"]')).not.toBeNull();
    expect(container.querySelector('[data-id="initial"] [data-handleid="in"]')).toBeNull();
    expect(container.querySelector('[data-id="choice"] [data-handleid="in"]')).not.toBeNull();
    expect(container.querySelector('[data-id="choice"] [data-handleid="out"]')).not.toBeNull();
    expect(container.querySelector('[data-id="output"] [data-handleid="in"]')).not.toBeNull();
    expect(container.querySelector('[data-id="output"] [data-handleid="out"]')).toBeNull();
  });
});
