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

  it("展示多模态结构化计划、版本继承和所选 Raw 视觉锚点", () => {
    render(<PromptHistory history={[{
      round_index: 1,
      canonical_prompt: "保留原片场景",
      generation_prompt: "以所选 Raw 为视觉参考，缩小主体",
      prompt_version_id: "pv_revision_01",
      prompt_version_hash: "a".repeat(64),
      based_on_prompt_version_id: "pv_initial_00",
      refinement_mode: "revision",
      generation_mode: "candidate_anchored_rebase",
      prompt_model: "gpt-5.6-luna",
      generation_model: "gpt-image-2",
      prompt_schema_version: "professional-prompt-plan/v1",
      prompt_template_version: "multimodal-prompt-refiner/v1",
      professional_prompt_plan: {
        role_of_inputs: ["Image 1 是不可变原片"],
        identity_invariants: ["保留眼睛与毛色"],
        change_from_anchor: ["主体缩小约 12%"],
        summary: "保留上一轮成功特征，只改主体尺度",
      },
      visual_anchor: {
        candidate_id: "candidate-raw-01",
        round_index: 0,
        raw_asset_id: "ast-raw-01",
        raw_asset_url: "/api/v1/assets/ast-raw-01",
      },
      active_directives: [],
      tuned: true,
    }]} />);

    expect(screen.getByText("多模态专业描述计划")).toBeInTheDocument();
    expect(screen.getByText("保留眼睛与毛色")).toBeInTheDocument();
    expect(screen.getByText("主体缩小约 12%")).toBeInTheDocument();
    expect(screen.getByText("所选 Raw 作为视觉参考")).toBeInTheDocument();
    expect(screen.getByText(/pv_initial/)).toBeInTheDocument();
    expect(screen.getByText(/候选视觉锚点/)).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("在 refiner SSE 期间只显示状态，不假设事件包含完整 prompt", () => {
    render(<PromptHistory
      history={[]}
      refinementState={{ status: "started", roundIndex: 1, mode: "revision" }}
    />);

    expect(screen.getByRole("status")).toHaveTextContent("正在整理");
    expect(screen.getByRole("status")).toHaveTextContent("完整 Prompt 会在版本落库后显示");
  });

  it("旧记录不会被错误标成多模态 Prompt", () => {
    render(<PromptHistory history={[{
      round_index: 0,
      canonical_prompt: "旧基准",
      generation_prompt: "旧 generation",
      active_directives: [],
      tuned: false,
    }]} />);

    expect(screen.getByText("初始 Prompt（兼容记录）")).toBeInTheDocument();
    expect(screen.queryByText("多模态初始理解")).not.toBeInTheDocument();
  });
});
