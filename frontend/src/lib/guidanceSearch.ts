import type { GuidanceMaskRegistration } from "../types";

export interface GuidanceMaskUploadCache {
  getOrUpload(
    projectId: string,
    documentHash: string,
    createFile: () => Promise<File>,
    upload: (projectId: string, file: File) => Promise<GuidanceMaskRegistration>,
  ): Promise<GuidanceMaskRegistration>;
  clear: () => void;
}

/**
 * Dedupe the only network boundary of the local Guidance brush. The key is
 * intentionally project + document hash: a retry after startSearch failed
 * reuses the already registered asset, while the same mask in another project
 * remains independently authorized by the backend.
 */
export function createGuidanceMaskUploadCache(): GuidanceMaskUploadCache {
  const pending = new Map<string, Promise<GuidanceMaskRegistration>>();

  return {
    getOrUpload(projectId, documentHash, createFile, upload) {
      const key = `${projectId}:${documentHash}`;
      const existing = pending.get(key);
      if (existing) return existing;

      const operation = Promise.resolve()
        .then(createFile)
        .then((file) => upload(projectId, file));
      pending.set(key, operation);
      // A failed upload must be retryable; successful registrations stay in
      // the cache for idempotent start-search retries.
      void operation.catch(() => {
        if (pending.get(key) === operation) pending.delete(key);
      });
      return operation;
    },
    clear() {
      pending.clear();
    },
  };
}
