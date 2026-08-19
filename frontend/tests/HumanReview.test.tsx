import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { HumanReview } from "../src/features/search/HumanReview";
import type { SearchSnapshot } from "../src/types";

const snapshot: SearchSnapshot = {
  search_id: "search-01",
  status: "waiting_for_human",
  round_index: 0,
  candidates: [{
    candidate_id: "candidate-01",
    round_index: 0,
    variant_index: 0,
    image_url: "/api/v1/assets/candidate-01",
    is_round_winner: true,
    is_global_winner: true,
  }],
  global_winner_id: "candidate-01",
  prompt_history: [],
  active_directives: [],
  interrupt_payload: {
    allowed_actions: ["continue_one_round", "accept_global_winner", "cancel"],
  },
};

describe("HumanReview", () => {
  it("把所选 Raw 候选和人工反馈一起提交到下一轮", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(<HumanReview snapshot={snapshot} isPending={false} selectedCandidateId="candidate-01" onAction={onAction} />);

    await user.type(
      screen.getByLabelText("人工反馈（可选，用于下一轮 prompt）"),
      "猫再小一点，保留眼睛和毛色。",
    );
    await user.click(screen.getByRole("button", { name: "再生成一轮" }));

    expect(onAction).toHaveBeenCalledWith(
      "continue_one_round",
      "candidate-01",
      "猫再小一点，保留眼睛和毛色。",
    );
  });

  it("没有 Timeline 选择时显式走 source-only，不偷换 Global Winner", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(<HumanReview snapshot={snapshot} isPending={false} onAction={onAction} />);

    expect(screen.getByText(/仅使用不可变原图与宠物参考图/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "再生成一轮" }));

    expect(onAction).toHaveBeenCalledWith("continue_one_round", undefined, undefined);
  });

  it("选中候选时明确提示原图是底片、Raw 是视觉参考", () => {
    render(<HumanReview snapshot={snapshot} isPending={false} selectedCandidateId="candidate-01" onAction={vi.fn()} />);

    expect(screen.getAllByText(/原图仍为编辑底片，所选 Raw 作为视觉参考/)).toHaveLength(2);
  });

  it("历史轮 Timeline 候选只允许查看或接受，不提交为当前轮视觉锚点", () => {
    const currentSnapshot: SearchSnapshot = {
      ...snapshot,
      round_index: 1,
      candidates: [
        snapshot.candidates[0],
        {
          candidate_id: "candidate-02",
          round_index: 1,
          variant_index: 0,
          image_url: "/api/v1/assets/candidate-02",
          is_round_winner: false,
          is_global_winner: false,
        },
      ],
    };
    render(
      <HumanReview
        snapshot={currentSnapshot}
        isPending={false}
        selectedCandidateId="candidate-01"
        onAction={vi.fn()}
      />,
    );

    expect(screen.getByText(/当前查看的是历史轮 Raw/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "再生成一轮" })).toBeDisabled();
  });
});
