import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Icon } from "../components/Icon";
import { CandidateGallery } from "../features/candidates/CandidateGallery";
import { PlacementCanvas } from "../features/placement/PlacementCanvas";
import { HumanReview } from "../features/search/HumanReview";
import { SearchControls } from "../features/search/SearchControls";
import { SearchTimeline } from "../features/search/SearchTimeline";
import { SourcePanel } from "../features/sources/SourcePanel";
import {
  createProject,
  createSearchIdempotencyKey,
  getSearch,
  resumeSearch,
  startSearch,
} from "../lib/api";
import { useSearchEvents } from "../lib/events";
import { useObjectUrl } from "../lib/files";
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
  const [placement, setPlacement] = useState<PlacementIntent>(initialPlacement);
  const [userIntent, setUserIntent] = useState("让宠物自然地坐在这里，像旅行中由同一台相机一起拍到的照片。");
  const [options, setOptions] = useState<SearchOptions>(initialOptions);
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [search, setSearch] = useState<SearchRecord | null>(null);
  const searchIdempotencyKey = useRef<string | null>(null);
  const backgroundUrl = useObjectUrl(draft.background);

  const invalidateSearch = useCallback(() => {
    if (!search) return;
    void queryClient.invalidateQueries({ queryKey: ["search", search.search_id] });
  }, [queryClient, search]);
  const { events, connectionState } = useSearchEvents(search, invalidateSearch);

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
      searchIdempotencyKey.current ??= createSearchIdempotencyKey();
      const nextSearch = await startSearch(
        nextProject.project_id,
        placement,
        userIntent,
        options,
        searchIdempotencyKey.current,
      );
      return nextSearch;
    },
    onSuccess: (nextSearch) => {
      searchIdempotencyKey.current = null;
      setSearch(nextSearch);
    },
  });

  const resumeMutation = useMutation({
    mutationFn: (action: ResumeAction) => {
      if (!search) throw new Error("没有可恢复的搜索");
      return resumeSearch(search.search_id, action);
    },
    onSuccess: () => invalidateSearch(),
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
    setPlacement(initialPlacement);
    setOptions(initialOptions);
    setProject(null);
    setSearch(null);
    searchIdempotencyKey.current = null;
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
        <a className="brand" href="#top" aria-label="Pet Fusion 首页">
          <span className="brand-mark"><Icon name="aperture" /></span>
          <span>
            <strong>PET FUSION</strong>
            <small>DIGITAL DARKROOM</small>
          </span>
        </a>
        <div className="header-center" aria-hidden="true">
          <span>IMMUTABLE NEGATIVE</span>
          <i />
          <span>LANGGRAPH SEARCH</span>
          <i />
          <span>COMPOSITE FLOOR</span>
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
        <section className="workbench-intro" id="top">
          <div>
            <p className="kicker"><span>PF—01</span> PHOTOGRAPHIC COMPOSITING WORKBENCH</p>
            <h1>让它真的<br /><em>出现在那一刻。</em></h1>
          </div>
          <div className="intro-note">
            <span className="note-rule" />
            <p>从原始旅行照重新采样每一轮，保护背景，保留历史最佳。</p>
            <small>生成广泛 · 独立审片 · 精确修正</small>
          </div>
        </section>

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
            <PlacementCanvas
              backgroundUrl={backgroundUrl}
              value={placement}
              onChange={setPlacement}
              disabled={Boolean(search) || submitMutation.isPending}
            />

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

            {snapshot && (
              <HumanReview
                snapshot={snapshot}
                isPending={resumeMutation.isPending}
                error={errorMessage(resumeMutation.error)}
                onAction={(action) => resumeMutation.mutate(action)}
              />
            )}

            <CandidateGallery
              candidates={candidates}
              status={status}
              expectedCount={options.candidate_count}
            />

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
