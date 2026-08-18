import { describe, expect, it } from "vitest";
import {
  FUSION_DEMO_CANDIDATE_ID,
  FUSION_DEMO_SNAPSHOT,
  isFusionDemoEnabled,
} from "../src/features/fusion/fusionDemo";

describe("fusion development demo", () => {
  it("只在显式 demo 查询参数下启用，并提供 accepted Raw candidate", () => {
    expect(isFusionDemoEnabled("?demo=fusion")).toBe(true);
    expect(isFusionDemoEnabled("?demo=other")).toBe(false);
    expect(isFusionDemoEnabled("")).toBe(false);
    expect(FUSION_DEMO_SNAPSHOT.status).toBe("accepted");
    expect(FUSION_DEMO_SNAPSHOT.global_winner_id).toBe(FUSION_DEMO_CANDIDATE_ID);
    expect(FUSION_DEMO_SNAPSHOT.candidates[0]?.raw_image_url).toBe("/mock/fusion-generated.svg");
  });
});
