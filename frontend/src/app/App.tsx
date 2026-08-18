import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Icon } from "../components/Icon";
import { EditorShell } from "../features/workbench/EditorShell";
import { WorkerToolbar } from "../features/workbench/WorkerToolbar";
import { WorkerViewport } from "../features/workbench/WorkerViewport";
import { useWorkbenchUi } from "../features/workbench/useWorkbenchUi";
import { ASSET_DRAG_TYPE, AssetBrowser, localAssetKey } from "../features/sources/AssetBrowser";
import type { FusionEditorHandle, FusionEditorState } from "../features/fusion/FusionEditor";
import { PromptInspector } from "../features/search/PromptInspector";
import { CriticInspector } from "../features/review/CriticInspector";
import { TimelineCanvas } from "../features/timeline/TimelineCanvas";
import {
  type GuidanceMaskEditorHandle,
  type GuidanceMaskEditorState,
} from "../features/placement/GuidanceMaskEditor";
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
import type { MaskBrushEditorHandle } from "../features/mask/MaskBrushEditor";
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
  assets: [],
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
  const [acceptedCandidateId, setAcceptedCandidateId] = useState<string | null>(null);
  const [guidanceState, setGuidanceState] = useState<GuidanceMaskEditorState | null>(null);
  const [eventRevision, setEventRevision] = useState(0);
  const searchIdempotencyKey = useRef<string | null>(null);
  const guidanceEditorRef = useRef<GuidanceMaskEditorHandle | null>(null);
  const fusionBrushRef = useRef<MaskBrushEditorHandle | null>(null);
  const fusionEditorRef = useRef<FusionEditorHandle | null>(null);
  const [fusionHistory, setFusionHistory] = useState({ canUndo: false, canRedo: false });
  const [fusionState, setFusionState] = useState<FusionEditorState>({ pending: false, error: null, result: null, ready: false });
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
      const editor = guidanceEditorRef.current;
      if (!editor) throw new Error("Guidance Mask 画布尚未准备好");
      const currentDocument = editor.getDocument();
      if (!currentDocument.strokes.length) {
        throw new Error("请先在底片上绘制 Guidance Mask；系统不会再自动添加初始 Mask");
      }
      let nextProject = project;
      if (!nextProject) {
        nextProject = await createProject(draft);
        setProject(nextProject);
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
      const guidanceMaskAssetId = registration.asset.asset_id;
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
      setAcceptedCandidateId(null);
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
    onSuccess: (nextSnapshot, variables) => {
      if (nextSnapshot.status === "accepted") {
        setAcceptedCandidateId(variables.action === "accept_candidate"
          ? variables.candidateId ?? null
          : nextSnapshot.global_winner_id ?? null);
      }
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
  const accepted = status === "accepted";
  const [ui, uiActions] = useWorkbenchUi({
    hasSearch: Boolean(search),
    accepted,
    roundIndex: snapshot?.round_index,
  });

  useEffect(() => {
    // Keep the explicit candidate selection aligned with the round fencing
    // policy: same-round SSE refreshes preserve it; a new round clears it.
    setSelectedCandidateId(null);
    setAcceptedCandidateId(null);
  }, [snapshot?.round_index]);

  const selectCandidate = useCallback((candidateId: string | null) => {
    setSelectedCandidateId(candidateId);
    uiActions.setSelectedNodeId(candidateId ? `candidate:${candidateId}` : null);
    if (candidateId) uiActions.setWorkerMode("generation");
  }, [uiActions]);

  const selectTimelineCandidate = useCallback((candidateId: string) => {
    // Review, Timeline and the main worker intentionally share one inspection
    // selection. The backend still rebases every generated round from the
    // immutable source; this UI state never becomes an image input.
    selectCandidate(candidateId);
  }, [selectCandidate]);

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

  const onReviewAction = useCallback((action: ResumeAction, candidateId?: string, humanFeedback?: string) => {
    resumeMutation.mutate({ action, candidateId, humanFeedback });
  }, [resumeMutation]);

  const registerFusionBrush = useCallback((handle: MaskBrushEditorHandle | null) => {
    fusionBrushRef.current = handle;
    if (!handle) setFusionHistory({ canUndo: false, canRedo: false });
  }, []);

  const handleBackgroundDrop = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (sourceLocked) return;
    const key = event.dataTransfer.getData(ASSET_DRAG_TYPE);
    const file = (draft.assets ?? []).find((item) => localAssetKey(item) === key);
    if (file) setDraft((current) => ({ ...current, background: file }));
  }, [draft.assets, sourceLocked]);

  const fusionImageUrl = fusionState.result
    ? fusionState.result.fusion_asset.asset_url ?? fusionState.result.fusion_asset.content_url ?? fusionState.result.fusion_asset.url ?? null
    : null;

  const sidebarContent = ui.sidebarTab === "assets" ? (
    <AssetBrowser
      value={draft}
      onChange={setDraft}
      locked={sourceLocked}
      onReset={resetWorkbench}
      layout={ui.assetLayout}
      onLayoutChange={uiActions.setAssetLayout}
    />
  ) : ui.sidebarTab === "prompt" ? (
    <PromptInspector
      userIntent={userIntent}
      onUserIntentChange={setUserIntent}
      options={options}
      onOptionsChange={setOptions}
      canStart={canStart}
      isSubmitting={submitMutation.isPending}
      status={status}
      error={errorMessage(submitMutation.error)}
      onStart={() => submitMutation.mutate()}
      history={snapshot?.prompt_history ?? []}
    />
  ) : ui.sidebarTab === "review" ? (
    <CriticInspector
      snapshot={snapshot}
      status={status}
      expectedCount={options.candidate_count}
      selectedCandidateId={selectedCandidateId}
      isPending={resumeMutation.isPending}
      error={errorMessage(resumeMutation.error)}
      onAction={onReviewAction}
    />
  ) : (
    <section className="fusion-sidebar" id="sidebar-panel-fusion" role="tabpanel" aria-label="Fusion 融合">
      <div className="inspector-title"><p className="workbench-kicker">FUSION / EXPLICIT ACTION</p><h2>Fusion</h2><p>Fusion 是 accepted Raw 之后的独立预览层，不会改写 Search/Critic 的权威资产。</p></div>
      <div className="fusion-sidebar-card"><Icon name="spark" /><strong>{fusionState.result ? "Fusion 已生成" : accepted ? "Fusion ready" : "接受 Raw 后可用"}</strong><span>{fusionState.error ?? (accepted ? "在主画布绘制区域，然后在这里应用 Fusion Mask。" : "当前 Search 仍以 Raw candidate 为唯一审片来源。")}</span></div>
      {accepted && ui.workerMode !== "fusion" && <button className="primary-button" type="button" onClick={() => uiActions.setWorkerMode("fusion")}>在主画布打开 Fusion</button>}
      {accepted && ui.workerMode === "fusion" && !fusionState.result && <button className="primary-button" type="button" disabled={!fusionState.ready || fusionState.pending} onClick={() => void fusionEditorRef.current?.apply()}>{fusionState.pending ? "正在融合…" : "应用 Fusion Mask"}</button>}
    </section>
  );

  return (
    <EditorShell
      sidebarTab={ui.sidebarTab}
      accepted={accepted}
      onSidebarTabChange={uiActions.setSidebarTab}
      sidebar={sidebarContent}
      status={status}
      connectionState={connectionState}
      jobLabel={search ? `JOB ${search.search_id.slice(0, 8)}` : "READY / 01"}
      toolbar={(
        <WorkerToolbar
          mode={ui.workerMode}
          zoom={ui.zoom}
          brushTool={ui.brushTool}
          brushSize={ui.brushSize}
          brushFlow={ui.brushFlow}
          brushFeather={ui.brushFeather}
          canUndo={ui.workerMode === "create" ? Boolean(guidanceState?.canUndo) : ui.workerMode === "fusion" && fusionHistory.canUndo}
          canRedo={ui.workerMode === "create" ? Boolean(guidanceState?.canRedo) : ui.workerMode === "fusion" && fusionHistory.canRedo}
          fusionUnlocked={accepted}
          onUndo={() => ui.workerMode === "fusion" ? fusionBrushRef.current?.undo() : guidanceEditorRef.current?.undo()}
          onRedo={() => ui.workerMode === "fusion" ? fusionBrushRef.current?.redo() : guidanceEditorRef.current?.redo()}
          onBrushToolChange={uiActions.setBrushTool}
          onBrushSizeChange={uiActions.setBrushSize}
          onBrushFlowChange={uiActions.setBrushFlow}
          onBrushFeatherChange={uiActions.setBrushFeather}
          onZoomChange={uiActions.setZoom}
          onFit={() => uiActions.setZoom(100)}
          onEnterFusion={uiActions.enterFusion}
        />
      )}
      main={(
        <div className="main-worker-stack">
          {searchQuery.isError && (
            <div className="network-notice" role="alert">
              <Icon name="warning" />
              <span><strong>暂时无法刷新搜索状态</strong>{errorMessage(searchQuery.error)}；系统会继续尝试连接。</span>
              <button type="button" onClick={() => searchQuery.refetch()}>立即重试</button>
            </div>
          )}
          <WorkerViewport
            mode={ui.workerMode}
            backgroundUrl={ui.workerMode === "fusion" ? fusionBackgroundUrl : backgroundUrl}
            backgroundWidth={backgroundAsset?.width}
            backgroundHeight={backgroundAsset?.height}
            placement={placement}
            guidanceEditorRef={guidanceEditorRef}
            guidanceState={guidanceState}
            onGuidanceStateChange={setGuidanceState}
            guidanceDisabled={submitMutation.isPending}
            guidanceLocked={Boolean(search)}
            snapshot={snapshot}
            selectedCandidateId={ui.workerMode === "fusion" ? acceptedCandidateId ?? selectedCandidateId : selectedCandidateId}
            expectedCandidateCount={options.candidate_count}
            onBackgroundDrop={handleBackgroundDrop}
            brushTool={ui.brushTool}
            brushSettings={{ size: ui.brushSize, flow: ui.brushFlow / 100, feather: ui.brushFeather / 100 }}
            zoom={ui.zoom}
            onZoomChange={uiActions.setZoom}
            onFusionBrushHandleChange={registerFusionBrush}
            onFusionBrushHistoryChange={setFusionHistory}
            fusionEditorRef={fusionEditorRef}
            onFusionStateChange={setFusionState}
          />
        </div>
      )}
      timeline={(
        <TimelineCanvas
          events={events}
          snapshot={snapshot}
          selectedNodeId={ui.selectedNodeId}
          onSelectCandidate={selectTimelineCandidate}
          onSelectSource={() => {
            uiActions.setSelectedNodeId("source");
            uiActions.setWorkerMode("create");
          }}
          onSelectFinal={(candidateId) => {
            setSelectedCandidateId(candidateId);
            uiActions.setSelectedNodeId("final");
            uiActions.setWorkerMode(fusionImageUrl ? "fusion" : "generation");
          }}
          sourceImageUrl={backgroundUrl}
          guidanceActive={Boolean(guidanceState?.dirty || guidanceState?.document.strokes.length)}
          fusionImageUrl={fusionImageUrl}
          acceptedCandidateId={acceptedCandidateId}
          expectedCandidateCount={options.candidate_count}
          generationActive={status === "queued" || status === "running"}
          currentRound={snapshot?.round_index ?? 0}
        />
      )}
    />
  );
}
