import type { ReactNode } from "react";
import { Icon } from "../../components/Icon";
import { SidebarTabs } from "../sidebar/SidebarTabs";
import type { EventConnectionState } from "../../lib/events";
import type { SearchStatusValue } from "../../types";
import type { SidebarTab } from "./useWorkbenchUi";

interface EditorShellProps {
  sidebarTab: SidebarTab;
  accepted: boolean;
  onSidebarTabChange: (tab: SidebarTab) => void;
  sidebar: ReactNode;
  toolbar: ReactNode;
  main: ReactNode;
  timeline: ReactNode;
  status: SearchStatusValue;
  connectionState: EventConnectionState;
  jobLabel?: string;
}

function statusLabel(status: SearchStatusValue): string {
  const labels: Record<SearchStatusValue, string> = {
    idle: "就绪",
    queued: "排队中",
    running: "生成中",
    waiting_for_human: "待审片",
    accepted: "已接受",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status];
}

export function EditorShell({
  sidebarTab,
  accepted,
  onSidebarTabChange,
  sidebar,
  toolbar,
  main,
  timeline,
  status,
  connectionState,
  jobLabel = "本地任务",
}: EditorShellProps) {
  return (
    <div className="editor-shell">
      <a className="skip-link" href="#editor-main">跳到工作区</a>
      <header className="editor-appbar">
        <a className="editor-brand" href="#editor-main" aria-label="Pet Fusion 工作台">
          <span className="editor-brand-mark"><Icon name="aperture" /></span>
          <span><strong>PET FUSION</strong><small>宠物合影工作台</small></span>
        </a>
        <div className="editor-document-title"><span>未命名合影</span><small>{accepted ? "候选已接受，可选局部融合" : "从原片生成候选"}</small></div>
        <div className="editor-appbar-status"><span className={`appbar-status-dot is-${status}`} /><span><small>{connectionState === "open" ? "实时更新中" : connectionState === "settled" ? "进度已保存" : "工作台"}</small><strong>{jobLabel} · {statusLabel(status)}</strong></span></div>
      </header>
      <div className="editor-body">
        <aside className="editor-sidebar" aria-label="工作台侧栏">
          <SidebarTabs value={sidebarTab} accepted={accepted} onChange={onSidebarTabChange} />
          <div className="editor-sidebar-scroll">{sidebar}</div>
        </aside>
        <div className="editor-workspace-column">
          <main id="editor-main" className="editor-main">
            <div className="editor-toolbar-dock">{toolbar}</div>
            <div className="editor-viewport-scroll">{main}</div>
          </main>
          <section className="editor-timeline-dock" aria-label="搜索时间线">{timeline}</section>
        </div>
      </div>
    </div>
  );
}
