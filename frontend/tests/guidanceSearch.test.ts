import { describe, expect, it, vi } from "vitest";
import { createGuidanceMaskUploadCache } from "../src/lib/guidanceSearch";

describe("Guidance Search 上传边界", () => {
  it("同一 project + document hash 并发/重试只创建一次文件并上传一次", async () => {
    const cache = createGuidanceMaskUploadCache();
    const createFile = vi.fn(async () => new File(["mask"], "guidance.png", { type: "image/png" }));
    const upload = vi.fn(async () => ({
      project_id: "project-01",
      source_manifest_hash: "a".repeat(64),
      asset: { asset_id: "ast-guidance" },
    }));

    const first = cache.getOrUpload("project-01", "document-hash", createFile, upload);
    const second = cache.getOrUpload("project-01", "document-hash", createFile, upload);
    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ asset: { asset_id: "ast-guidance" } }),
      expect.objectContaining({ asset: { asset_id: "ast-guidance" } }),
    ]);
    await cache.getOrUpload("project-01", "document-hash", createFile, upload);

    expect(createFile).toHaveBeenCalledTimes(1);
    expect(upload).toHaveBeenCalledTimes(1);
  });

  it("上传失败会清除缓存，允许用户重试", async () => {
    const cache = createGuidanceMaskUploadCache();
    const createFile = vi.fn(async () => new File(["mask"], "guidance.png", { type: "image/png" }));
    const upload = vi.fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce({
        project_id: "project-01",
        source_manifest_hash: "a".repeat(64),
        asset: { asset_id: "ast-guidance" },
      });

    await expect(cache.getOrUpload("project-01", "document-hash", createFile, upload)).rejects.toThrow("network");
    await expect(cache.getOrUpload("project-01", "document-hash", createFile, upload)).resolves.toMatchObject({
      asset: { asset_id: "ast-guidance" },
    });
    expect(upload).toHaveBeenCalledTimes(2);
  });
});
