import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ASSET_DRAG_TYPE, AssetBrowser } from "../src/features/sources/AssetBrowser";
import type { SourceDraft } from "../src/types";

function Harness() {
  const [draft, setDraft] = useState<SourceDraft>({ background: null, references: [], assets: [] });
  return <><AssetBrowser value={draft} onChange={setDraft} locked={false} onReset={() => undefined} layout="list" onLayoutChange={() => undefined} /><output data-testid="background">{draft.background?.name}</output></>;
}

describe("AssetBrowser", () => {
  it("一次导入多张素材，并支持拖到参考槽或指定为底片", async () => {
    const view = render(<Harness />);
    const input = view.container.querySelector('input[type="file"]') as HTMLInputElement;
    const files = ["a.jpg", "b.jpg", "c.jpg"].map((name) => new File([name], name, { type: "image/jpeg", lastModified: 1 }));
    fireEvent.change(input, { target: { files } });
    await waitFor(() => expect(screen.getByText("已导入 3 张")).toBeInTheDocument());
    expect(screen.getAllByText(/\.jpg$/)).toHaveLength(3);

    const transfer = { effectAllowed: "", dropEffect: "", values: new Map<string, string>(), setData(type: string, value: string) { this.values.set(type, value); }, getData(type: string) { return this.values.get(type) ?? ""; } };
    fireEvent.dragStart(screen.getByText("a.jpg").closest(".library-asset")!, { dataTransfer: transfer });
    expect(transfer.getData(ASSET_DRAG_TYPE)).toContain("a.jpg");
    fireEvent.drop(screen.getByTestId("reference-drop-slot"), { dataTransfer: transfer });
    expect(screen.getByText("1 / 5")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "将 b.jpg 设为底片" }));
    expect(screen.getByTestId("background")).toHaveTextContent("b.jpg");

    fireEvent.mouseEnter(screen.getByText("c.jpg").closest(".library-asset")!);
    const preview = document.body.querySelector(".asset-hover-preview");
    expect(preview).not.toBeNull();
    expect(view.container.querySelector(".asset-hover-preview")).toBeNull();
    expect(preview).toHaveTextContent("c.jpg");
  });
});
