import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CriticInspector } from "../src/features/review/CriticInspector";
import type { SearchSnapshot } from "../src/types";

const snapshot: SearchSnapshot = {
  search_id: "search-review",
  status: "waiting_for_human",
  round_index: 1,
  candidates: [{
    candidate_id: "candidate-selected",
    round_index: 1,
    variant_index: 0,
    image_url: "/api/v1/assets/candidate-selected",
    score: 63.5,
    is_round_winner: false,
    is_global_winner: false,
    evaluation: {
      total_score: 63.5,
      summary: "主体身份可信，但右前肢与座椅边缘关系不自然。",
      scores: {
        cat_identity: 91,
        pose_geometry: 43,
        perspective_scale: 78,
        lighting_color: 72,
        optical_consistency: 69,
        physical_integration: 48,
        scene_preservation: 95,
        overall_photographic_naturalness: 61,
      },
      issues: [{
        issue_id: "issue-limb",
        category: "pose_geometry",
        severity: "blocking",
        region: "猫右前肢与座椅交界",
        evidence: "右前肢在座椅边缘处发生不可能的折叠。",
        suggested_fix: "重建完整前肢，并保留清晰的接触边界。",
        confidence: 0.92,
      }],
      no_meaningful_defect: false,
      identity_match: true,
      prompt_adherent: true,
      recommended_action: "review",
    },
  }],
  global_winner_id: "candidate-other",
  prompt_history: [],
  active_directives: [],
  interrupt_payload: {
    allowed_actions: ["continue_one_round", "accept_candidate", "accept_global_winner", "cancel"],
  },
};

describe("CriticInspector", () => {
  it("把分数降级为参考，并完整展示结构化 Critic 证据", () => {
    render(
      <CriticInspector
        snapshot={snapshot}
        status="waiting_for_human"
        expectedCount={3}
        selectedCandidateId="candidate-selected"
        onAction={vi.fn()}
      />,
    );

    expect(screen.getByText("AI 参考")).toBeInTheDocument();
    expect(screen.getByLabelText("AI 参考分 63.5")).toBeInTheDocument();
    expect(screen.getByText("1 个阻断问题")).toBeInTheDocument();
    expect(screen.getAllByText("姿态结构")).toHaveLength(2);
    expect(screen.getByText("猫右前肢与座椅交界", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("92% 置信度")).toBeInTheDocument();
    expect(screen.getByText("重建完整前肢，并保留清晰的接触边界。")).toBeInTheDocument();
    expect(screen.getAllByText("场景保留")).not.toHaveLength(0);
  });

  it("没有时间线选择时不私自展示 Global Winner", () => {
    render(
      <CriticInspector
        snapshot={snapshot}
        status="waiting_for_human"
        expectedCount={3}
        onAction={vi.fn()}
      />,
    );

    expect(screen.getByText("从时间线选择一张图片")).toBeInTheDocument();
    expect(screen.queryByText("主体身份可信，但右前肢与座椅边缘关系不自然。")).not.toBeInTheDocument();
  });

  it("接受当前图片时提交时间线选中的候选", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    render(
      <CriticInspector
        snapshot={snapshot}
        status="waiting_for_human"
        expectedCount={3}
        selectedCandidateId="candidate-selected"
        onAction={onAction}
      />,
    );

    await user.click(screen.getByRole("button", { name: "接受当前图片" }));
    expect(onAction).toHaveBeenCalledWith("accept_candidate", "candidate-selected");
  });

  it("不会把疑似 0–10 量表的低分接受结论显示成无缺陷", () => {
    const conflictingSnapshot: SearchSnapshot = {
      ...snapshot,
      candidates: [{
        ...snapshot.candidates[0],
        score: 8.8,
        ranker_eligible: false,
        hard_fail_reasons: ["critic_semantic_conflict"],
        evaluation: {
          summary: "No meaningful defect was found.",
          scores: {
            cat_identity: 9,
            pose_geometry: 8,
            perspective_scale: 9,
            lighting_color: 8.5,
            optical_consistency: 8.5,
            physical_integration: 8.5,
            scene_preservation: 10,
            overall_photographic_naturalness: 8.5,
          },
          issues: [],
          no_meaningful_defect: true,
          identity_match: true,
          prompt_adherent: true,
          recommended_action: "accept",
          semantic_conflict: false,
        },
      }],
    };

    render(
      <CriticInspector
        snapshot={conflictingSnapshot}
        status="waiting_for_human"
        expectedCount={3}
        selectedCandidateId="candidate-selected"
        onAction={vi.fn()}
      />,
    );

    expect(screen.getByText("结果矛盾")).toBeInTheDocument();
    expect(screen.queryByText("未发现明确缺陷")).not.toBeInTheDocument();
    expect(screen.getByText("评分与结论不一致")).toBeInTheDocument();
  });
});
