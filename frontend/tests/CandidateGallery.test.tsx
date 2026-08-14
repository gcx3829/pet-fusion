import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CandidateGallery } from "../src/features/candidates/CandidateGallery";
import type { SearchCandidate } from "../src/types";

const candidates: SearchCandidate[] = [
  {
    candidate_id: "candidate-a",
    round_index: 0,
    variant_index: 0,
    image_url: "/api/v1/assets/a",
    score: 87.5,
    is_round_winner: false,
    is_global_winner: false,
    evaluation: { issues: [], summary: "第一张候选" },
  },
  {
    candidate_id: "candidate-b",
    round_index: 0,
    variant_index: 1,
    image_url: "/api/v1/assets/b",
    score: 91.2,
    is_round_winner: true,
    is_global_winner: true,
    evaluation: {
      issues: [{ category: "lighting", severity: "warning", evidence: "高光略强" }],
      summary: "历史最佳候选",
    },
  },
];

describe("CandidateGallery", () => {
  it("显示历史最佳而非默认把最后输出当作最佳", () => {
    render(<CandidateGallery candidates={candidates} status="waiting_for_human" expectedCount={3} />);
    expect(screen.getByText("GLOBAL WINNER")).toBeInTheDocument();
    expect(screen.getByText("91.2")).toBeInTheDocument();
    expect(screen.getByText("历史最佳候选")).toBeInTheDocument();
  });

  it("选择另一张印样后更新审片摘要", async () => {
    const user = userEvent.setup();
    render(<CandidateGallery candidates={candidates} status="waiting_for_human" expectedCount={3} />);
    await user.click(screen.getByRole("button", { name: /第 1 轮，第 1 张候选/ }));
    expect(screen.getByText("第一张候选")).toBeInTheDocument();
  });

  it("同轮数据刷新保留人工选择，跨轮时清理选择", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const view = render(
      <CandidateGallery
        candidates={candidates}
        status="waiting_for_human"
        expectedCount={3}
        activeRoundIndex={0}
        onSelect={onSelect}
      />,
    );
    await user.click(screen.getByRole("button", { name: /第 1 轮，第 1 张候选/ }));
    onSelect.mockClear();

    view.rerender(
      <CandidateGallery
        candidates={[...candidates]}
        status="waiting_for_human"
        expectedCount={3}
        activeRoundIndex={0}
        onSelect={onSelect}
      />,
    );
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByText("第一张候选")).toBeInTheDocument();

    view.rerender(
      <CandidateGallery
        candidates={candidates.map((candidate) => ({ ...candidate, round_index: 1 }))}
        status="waiting_for_human"
        expectedCount={3}
        activeRoundIndex={1}
        onSelect={onSelect}
      />,
    );
    expect(onSelect).toHaveBeenCalledWith(null);
    expect(screen.getByText("历史最佳候选")).toBeInTheDocument();
  });
});
