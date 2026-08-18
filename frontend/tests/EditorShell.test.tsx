import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EditorShell } from "../src/features/workbench/EditorShell";

describe("EditorShell layout", () => {
  it("sider 贯穿右侧画布与 timeline，timeline 与画布共用右栏", () => {
    const view = render(<EditorShell sidebarTab="assets" accepted={false} onSidebarTabChange={() => undefined} sidebar={<div>side</div>} toolbar={<div>tools</div>} main={<div>canvas</div>} timeline={<div>photos</div>} status="idle" connectionState="idle" />);
    const body = view.container.querySelector(".editor-body")!;
    const sidebar = view.container.querySelector(".editor-sidebar")!;
    const workspace = view.container.querySelector(".editor-workspace-column")!;
    const main = view.container.querySelector(".editor-main")!;
    const timeline = view.container.querySelector(".editor-timeline-dock")!;
    expect(body.children[0]).toBe(sidebar);
    expect(body.children[1]).toBe(workspace);
    expect(workspace.children[0]).toBe(main);
    expect(workspace.children[1]).toBe(timeline);
  });
});
