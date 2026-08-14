import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PromptHistory } from "../src/features/search/PromptHistory";

describe("PromptHistory", () => {
  it("默认展开最新一轮的调优 prompt 并显示人工反馈", () => {
    render(<PromptHistory history={[
      {
        round_index: 0,
        canonical_prompt: "初始 canonical",
        generation_prompt: "初始 generation",
        active_directives: [],
        tuned: false,
      },
      {
        round_index: 1,
        canonical_prompt: "调优 canonical",
        generation_prompt: "调优 generation",
        active_directives: [],
        human_feedback: "猫再小一点",
        tuned: true,
      },
    ]} />);

    expect(screen.getByText("猫再小一点")).toBeInTheDocument();
    const latest = screen.getByText("调优 generation").closest("details");
    expect(latest).toHaveAttribute("open");
  });
});
