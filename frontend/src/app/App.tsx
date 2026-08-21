import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Icon } from "../components/Icon";
import { EditorShell } from "../features/workbench/EditorShell";
import { WorkerToolbar } from "../features/workbench/WorkerToolbar";
import { WorkerViewport } from "../features/workbench/WorkerViewport";
import { useWorkbenchUi } from "../features/workbench/useWorkbenchUi";
import { ASSET_DRAG_TYPE, AssetBrowser, localAssetKey } from "../features/sources/AssetBrowser";
import type { FusionEditorHandle, FusionEditorState } from "../features/fusion/FusionEditor";
import {
  FUSION_DEMO_CANDIDATE_ID,
  FUSION_DEMO_HEIGHT,
  FUSION_DEMO_SNAPSHOT,
  FUSION_DEMO_SOURCE_URL,
  FUSION_DEMO_WIDTH,
  isFusionDemoEnabled,
} from "../features/fusion/fusionDemo";
import { PromptInspector } from "../features/search/PromptInspector";
import { CriticInspector } from "../features/review/CriticInspector";
import { shouldClearCandidateAfterResume } from "../features/review/selectionPolicy";
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
import { derivePromptRefinementState, useSearchEvents } from "../lib/events";
import { useObjectUrl } from "../lib/files";
import { createGuidanceMaskUploadCache } from "../lib/guidanceSearch";
import { maskDocumentFingerprint, resizeMaskDocument } from "../lib/maskDocument";
import type { MaskBrushEditorHandle, MaskHistoryState } from "../features/mask/MaskBrushEditor";
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

const emptyMaskHistory: MaskHistoryState = {
  canUndo: false,
  canRedo: false,
  undoDepth: 0,
  redoDepth: 0,
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
  const fusionDemoMode = isFusionDemoEnabled(window.location.search);
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
  const [fusionHistory, setFusionHistory] = useState<MaskHistoryState>(emptyMaskHistory);
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
      if (!editor) throw new Error("引导区域画布尚未准备好");
      const currentDocument = editor.getDocument();
      if (!currentDocument.strokes.length) {
        throw new Error("请先在原片上画出希望模型重点修改的区域");
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
      // Keep the reviewed candidate visible while the queued revision prompt
      // is being prepared. Clearing it here makes the worker briefly fall back
      // to Global Winner even though the backend correctly anchors the paid
      // request to the user's selection. The round-index effect clears it when
      // the next round actually materializes.
      if (shouldClearCandidateAfterResume(variables.action)) setSelectedCandidateId(null);
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

  const snapshot = fusionDemoMode ? FUSION_DEMO_SNAPSHOT : searchQuery.data;
  const status: SearchStatusValue = snapshot?.status ?? search?.status ?? "idle";
  const promptRefinement = derivePromptRefinementState(events, snapshot?.round_index);
  const candidates = snapshot?.candidates ?? [];
  const sourceLocked = fusionDemoMode || Boolean(project) || submitMutation.isPending;
  const canStart = Boolean(
    draft.background
    && draft.references.length >= 1
    && draft.references.length <= 5
    && userIntent.trim(),
  );
  const accepted = status === "accepted";
  const [ui, uiActions] = useWorkbenchUi({
    hasSearch: fusionDemoMode || Boolean(search),
    accepted,
    roundIndex: snapshot?.round_index,
  });

  useEffect(() => {
    // Keep the explicit candidate selection aligned with the round fencing
    // policy: same-round SSE refreshes preserve it; a new round clears it.
    setSelectedCandidateId(null);
    setAcceptedCandidateId(null);
  }, [snapshot?.round_index]);

  useEffect(() => {
    if (!fusionDemoMode) return;
    setAcceptedCandidateId((current) => current === FUSION_DEMO_CANDIDATE_ID ? current : FUSION_DEMO_CANDIDATE_ID);
    setSelectedCandidateId((current) => current === FUSION_DEMO_CANDIDATE_ID ? current : FUSION_DEMO_CANDIDATE_ID);
    uiActions.setSelectedNodeId(`candidate:${FUSION_DEMO_CANDIDATE_ID}`);
    uiActions.enterFusion();
  }, [fusionDemoMode, uiActions.enterFusion, uiActions.setSelectedNodeId]);

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
    setAcceptedCandidateId(null);
    setGuidanceState(null);
    setFusionHistory(emptyMaskHistory);
    setFusionState({ pending: false, error: null, result: null, ready: false });
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
  const effectiveBackgroundUrl = fusionDemoMode ? FUSION_DEMO_SOURCE_URL : backgroundUrl;
  const effectiveBackgroundWidth = fusionDemoMode ? FUSION_DEMO_WIDTH : backgroundAsset?.width;
  const effectiveBackgroundHeight = fusionDemoMode ? FUSION_DEMO_HEIGHT : backgroundAsset?.height;
  const effectiveFusionBackgroundUrl = fusionDemoMode ? FUSION_DEMO_SOURCE_URL : fusionBackgroundUrl;

  useEffect(() => {
    if (!fusionImageUrl?.startsWith("blob:")) return;
    return () => URL.revokeObjectURL(fusionImageUrl);
  }, [fusionImageUrl]);

  const exportFullSizeFusion = useCallback(() => {
    if (!fusionImageUrl || !fusionState.result) return;
    const anchor = document.createElement("a");
    anchor.href = fusionImageUrl;
    anchor.download = `pet-fusion-${fusionState.result.candidate_id}.png`;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }, [fusionImageUrl, fusionState.result]);

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
      refinementState={promptRefinement}
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
    <section className="fusion-sidebar" id="sidebar-panel-fusion" role="tabpanel" aria-label="局部融合">
      <div className="inspector-title"><h2>局部融合</h2><p>只修改你画出的区域，不会改动已经接受的原始候选。</p></div>
      {fusionDemoMode && <div className="fusion-demo-notice" role="status"><strong>本地预览</strong><span>图片只在浏览器中处理，不会发送到后端。</span></div>}
      <div className="fusion-sidebar-card"><Icon name="spark" /><strong>{fusionState.result ? "预览已生成" : accepted ? "可以开始" : "请先接受一张候选图"}</strong><span>{fusionState.error ?? (accepted ? "在主画布画出需要融合的区域，然后应用。" : "接受候选图后才能使用局部融合。")}</span></div>
      {accepted && ui.workerMode !== "fusion" && <button className="primary-button" type="button" onClick={() => uiActions.setWorkerMode("fusion")}>打开局部融合</button>}
      {accepted && ui.workerMode === "fusion" && !fusionState.result && <button className="primary-button" type="button" disabled={!fusionState.ready || fusionState.pending} onClick={() => void fusionEditorRef.current?.apply()}>{fusionState.pending ? "正在融合…" : "应用融合范围"}</button>}
      {fusionState.result && <button className="fusion-export-button" type="button" onClick={exportFullSizeFusion}><Icon name="export" /><span><strong>导出全尺寸 PNG</strong><small>{effectiveBackgroundWidth ?? fusionState.result.fusion_asset.width ?? "原图"} × {effectiveBackgroundHeight ?? fusionState.result.fusion_asset.height ?? "尺寸"}</small></span></button>}
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
      jobLabel={search ? `任务 ${search.search_id.slice(0, 8)}` : "本地任务"}
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
          historyDepth={ui.workerMode === "create" ? guidanceState?.document.strokes.length ?? 0 : ui.workerMode === "fusion" ? fusionHistory.undoDepth : 0}
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
            backgroundUrl={ui.workerMode === "fusion" ? effectiveFusionBackgroundUrl : effectiveBackgroundUrl}
            backgroundWidth={effectiveBackgroundWidth}
            backgroundHeight={effectiveBackgroundHeight}
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
            fusionDemoMode={fusionDemoMode}
            restoredFusionResult={fusionState.result}
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
            setSelectedCandidateId(null);
            uiActions.setSelectedNodeId("source");
            uiActions.setWorkerMode("create");
          }}
          onSelectFinal={(candidateId) => {
            setSelectedCandidateId(candidateId);
            uiActions.setSelectedNodeId("final");
            uiActions.setWorkerMode(fusionImageUrl ? "fusion" : "generation");
          }}
          sourceImageUrl={effectiveBackgroundUrl}
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
