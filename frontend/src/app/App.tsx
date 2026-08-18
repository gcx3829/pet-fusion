import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Icon } from "../components/Icon";
import { CandidateGallery } from "../features/candidates/CandidateGallery";
import { FusionEditor } from "../features/fusion/FusionEditor";
import {
  GuidanceMaskEditor,
  type GuidanceMaskEditorHandle,
  type GuidanceMaskEditorState,
} from "../features/placement/GuidanceMaskEditor";
import { HumanReview } from "../features/search/HumanReview";
import { PromptHistory } from "../features/search/PromptHistory";
import { SearchControls } from "../features/search/SearchControls";
import { SearchTimeline } from "../features/search/SearchTimeline";
import { SourcePanel } from "../features/sources/SourcePanel";
import {
  createProject,
  createSearchIdempotencyKey,
  assetContentUrl,
  getSearch,
  uploadGuidanceMask,
  resumeSearch,
  startSearch,
} from "../lib/api";
import { useSearchEvents } from "../lib/events";
import { useObjectUrl } from "../lib/files";
import { createGuidanceMaskUploadCache } from "../lib/guidanceSearch";
import { maskDocumentFingerprint, resizeMaskDocument } from "../lib/maskDocument";
import { exportMaskFile } from "../lib/maskRasterizer";
import type {
  PlacementIntent,
  ProjectRecord,
  ResumeAction,
  SearchOptions,
  SearchRecord,
  SearchStatusValue,
  SourceDraft,
} from "../types";

const initialDraft: SourceDraft = {
  background: null,
  references: [],
  catName: "",
  catTraits: "",
};

const initialPlacement: PlacementIntent = {
  x: 0.63,
  y: 0.55,
  width: 0.18,
  height: 0.32,
  coordinate_space: "normalized",
  pose: "sitting",
  facing: "slightly_left",
  contact_surface: "",
};

const initialOptions: SearchOptions = {
  candidate_count: 3,
  max_rounds: 3,
  budget_usd: 2,
  review_each_round: false,
};

const terminalStatuses: SearchStatusValue[] = [
  "waiting_for_human",
  "accepted",
  "completed",
  "failed",
  "cancelled",
];

function errorMessage(error: unknown): string | null {
  if (!error) return null;
  return error instanceof Error ? error.message : "发生了未知错误";
}

export function App() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<SourceDraft>(initialDraft);
  // The visible placement/orientation controls were replaced by the local
  // Guidance Mask brush. Keep this legacy payload only because the current
  // API and older SQLite rows still require a placement object when no custom
  // mask is uploaded.
  const placement = initialPlacement;
  const [userIntent, setUserIntent] = useState("让宠物自然地坐在这里，像旅行中由同一台相机一起拍到的照片。");
  const [options, setOptions] = useState<SearchOptions>(initialOptions);
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [search, setSearch] = useState<SearchRecord | null>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [guidanceState, setGuidanceState] = useState<GuidanceMaskEditorState | null>(null);
  const [eventRevision, setEventRevision] = useState(0);
  const searchIdempotencyKey = useRef<string | null>(null);
  const guidanceEditorRef = useRef<GuidanceMaskEditorHandle | null>(null);
  const guidanceUploadCacheRef = useRef(createGuidanceMaskUploadCache());
  const backgroundUrl = useObjectUrl(draft.background);
  const backgroundAsset = project?.source_manifest?.background;
  const fusionBackgroundUrl = backgroundAsset
    ? backgroundAsset.asset_url
      ?? backgroundAsset.content_url
      ?? backgroundAsset.url
      ?? assetContentUrl(backgroundAsset.asset_id)
    : backgroundUrl;

  const invalidateSearch = useCallback(() => {
    if (!search) return;
    void queryClient.invalidateQueries({ queryKey: ["search", search.search_id] });
  }, [queryClient, search]);
  const { events, connectionState } = useSearchEvents(search, invalidateSearch, eventRevision);

  const searchQuery = useQuery({
    queryKey: ["search", search?.search_id],
    queryFn: () => getSearch(search!.search_id),
    enabled: Boolean(search),
    retry: 2,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status && terminalStatuses.includes(status)) return false;
      return connectionState === "open" ? 12_000 : 2_500;
    },
  });

  const submitMutation = useMutation({
    mutationFn: async () => {
      let nextProject = project;
      if (!nextProject) {
        nextProject = await createProject(draft);
        setProject(nextProject);
      }
      let guidanceMaskAssetId: string | null = null;
      if (guidanceState?.dirty) {
        const editor = guidanceEditorRef.current;
        if (!editor) throw new Error("Guidance Mask 画布尚未准备好");
        const currentDocument = editor.getDocument();
        if (!currentDocument.strokes.length) {
          throw new Error("自定义 Guidance Mask 不能为空；请至少绘制一个可编辑区域");
        }
        const projectBackground = nextProject.source_manifest?.background;
        const uploadDocument = (
          projectBackground?.width
          && projectBackground.height
        )
          ? resizeMaskDocument(
              currentDocument,
              projectBackground.width,
              projectBackground.height,
            )
          : currentDocument;
        const documentHash = maskDocumentFingerprint(uploadDocument);
        const registration = await guidanceUploadCacheRef.current.getOrUpload(
          nextProject.project_id,
          documentHash,
          () => exportMaskFile(uploadDocument, "guidance-mask.png"),
          uploadGuidanceMask,
        );
        guidanceMaskAssetId = registration.asset.asset_id;
      }
      searchIdempotencyKey.current ??= createSearchIdempotencyKey();
      const nextSearch = await startSearch(
        nextProject.project_id,
        placement,
        userIntent,
        options,
        searchIdempotencyKey.current,
        guidanceMaskAssetId,
      );
      return nextSearch;
    },
    onSuccess: (nextSearch) => {
      searchIdempotencyKey.current = null;
      setSelectedCandidateId(null);
      setSearch(nextSearch);
    },
  });

  const resumeMutation = useMutation({
    mutationFn: ({
      action,
      candidateId,
      humanFeedback,
    }: {
      action: ResumeAction;
      candidateId?: string;
      humanFeedback?: string;
    }) => {
      if (!search) throw new Error("没有可恢复的搜索");
      return resumeSearch(
        search.search_id,
        action,
        candidateId,
        humanFeedback,
        snapshot?.round_index,
      );
    },
    onSuccess: (nextSnapshot) => {
      setSelectedCandidateId(null);
      queryClient.setQueryData(["search", nextSnapshot.search_id], nextSnapshot);
      setEventRevision((revision) => revision + 1);
      setSearch((current) => current
        ? {
            ...current,
            status: nextSnapshot.status,
            events_url: nextSnapshot.events_url ?? current.events_url,
          }
        : current);
      void queryClient.invalidateQueries({ queryKey: ["search", nextSnapshot.search_id] });
    },
  });

  const snapshot = searchQuery.data;
  const status: SearchStatusValue = snapshot?.status ?? search?.status ?? "idle";
  const candidates = snapshot?.candidates ?? [];
  const sourceLocked = Boolean(project) || submitMutation.isPending;
  const canStart = Boolean(
    draft.background
    && draft.references.length >= 1
    && draft.references.length <= 5
    && userIntent.trim(),
  );

  const resetWorkbench = () => {
    if (search) queryClient.removeQueries({ queryKey: ["search", search.search_id] });
    setDraft(initialDraft);
    setOptions(initialOptions);
    setProject(null);
    setSearch(null);
    setSelectedCandidateId(null);
    setGuidanceState(null);
    setEventRevision(0);
    searchIdempotencyKey.current = null;
    guidanceUploadCacheRef.current.clear();
    submitMutation.reset();
    resumeMutation.reset();
  };

  const connectionLabel = search
    ? connectionState === "open"
      ? "EVENT STREAM LIVE"
      : connectionState === "settled"
        ? "EVENT STREAM COMPLETE"
        : "POLLING FALLBACK"
    : "LOCAL WORKBENCH";

  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="brand" href="#workbench" aria-label="Pet Fusion 首页">
          <span className="brand-mark"><Icon name="aperture" /></span>
          <span>
            <strong>PET FUSION</strong>
            <small>DIGITAL DARKROOM</small>
          </span>
        </a>
        <div className="header-center" aria-hidden="true">
          <span>IMMUTABLE NEGATIVE</span>
          <i />
          <span>RAW REVIEW</span>
          <i />
          <span>OPTIONAL FUSION</span>
        </div>
        <div className="header-status">
          <span className={`live-dot ${connectionState === "open" ? "is-live" : ""}`} />
          <span>
            <small>{connectionLabel}</small>
            <strong>{search ? `JOB ${search.search_id.slice(0, 8)}` : "READY / 01"}</strong>
          </span>
        </div>
      </header>

      <main id="workbench" className="workbench">
        <div className="workspace-grid">
          <aside className="control-rail">
            <SourcePanel
              value={draft}
              onChange={setDraft}
              project={project}
              locked={sourceLocked}
              onReset={resetWorkbench}
            />
            <SearchControls
              userIntent={userIntent}
              onUserIntentChange={setUserIntent}
              options={options}
              onOptionsChange={setOptions}
              canStart={canStart}
              isSubmitting={submitMutation.isPending}
              status={status}
              error={errorMessage(submitMutation.error)}
              onStart={() => submitMutation.mutate()}
            />
          </aside>

          <div className="review-stage">
            {backgroundUrl && (
              <GuidanceMaskEditor
                ref={guidanceEditorRef}
                backgroundSrc={backgroundUrl}
                width={backgroundAsset?.width}
                height={backgroundAsset?.height}
                placement={placement}
                disabled={submitMutation.isPending}
                locked={Boolean(search)}
                onStateChange={setGuidanceState}
              />
            )}

            {searchQuery.isError && (
              <div className="network-notice" role="alert">
                <Icon name="warning" />
                <span>
                  <strong>暂时无法刷新搜索状态</strong>
                  {errorMessage(searchQuery.error)}；系统会继续尝试连接。
                </span>
                <button type="button" onClick={() => searchQuery.refetch()}>立即重试</button>
              </div>
            )}

            {search && <PromptHistory history={snapshot?.prompt_history ?? []} />}

            {snapshot && (
              <HumanReview
                snapshot={snapshot}
                isPending={resumeMutation.isPending}
                error={errorMessage(resumeMutation.error)}
                selectedCandidateId={selectedCandidateId}
                onAction={(action, candidateId, humanFeedback) => resumeMutation.mutate({
                  action,
                  candidateId,
                  humanFeedback,
                })}
              />
            )}

            <CandidateGallery
              candidates={candidates}
              status={status}
              expectedCount={options.candidate_count}
              activeRoundIndex={snapshot?.round_index}
              onSelect={setSelectedCandidateId}
            />

            {snapshot && (
              <FusionEditor
                snapshot={snapshot}
                selectedCandidateId={selectedCandidateId}
                placement={placement}
                backgroundSrc={fusionBackgroundUrl}
                backgroundWidth={backgroundAsset?.width}
                backgroundHeight={backgroundAsset?.height}
              />
            )}

            <SearchTimeline
              events={events}
              status={status}
              connectionState={connectionState}
              roundIndex={snapshot?.round_index ?? 0}
              activeDirectives={snapshot?.active_directives ?? []}
            />
          </div>
        </div>
      </main>

      <footer className="app-footer">
        <span>PF / GREENFIELD SEARCH</span>
        <p>浏览器不接触 API Key · 图状态只保存资产引用</p>
        <span>© 2026 / FRAME 001</span>
      </footer>
    </div>
  );
}
