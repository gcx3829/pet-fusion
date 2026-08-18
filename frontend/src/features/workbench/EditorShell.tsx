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
    idle: "READY",
    queued: "QUEUED",
    running: "PROCESSING",
    waiting_for_human: "REVIEW",
    accepted: "ACCEPTED",
    completed: "COMPLETE",
    failed: "FAILED",
    cancelled: "CANCELLED",
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
  jobLabel = "LOCAL / 001",
}: EditorShellProps) {
  return (
    <div className="editor-shell">
      <a className="skip-link" href="#editor-main">跳到工作区</a>
      <header className="editor-appbar">
        <a className="editor-brand" href="#editor-main" aria-label="Pet Fusion 工作台">
          <span className="editor-brand-mark"><Icon name="aperture" /></span>
          <span><strong>PET FUSION</strong><small>IMAGE WORKBENCH</small></span>
        </a>
        <div className="editor-document-title"><span>UNTITLED FUSION</span><small>{accepted ? "ACCEPTED RAW / OPTIONAL FUSION" : "RAW-FIRST SEARCH SESSION"}</small></div>
        <div className="editor-appbar-status"><span className={`appbar-status-dot is-${status}`} /><span><small>{connectionState === "open" ? "EVENT STREAM LIVE" : connectionState === "settled" ? "CHECKPOINT SETTLED" : "WORKBENCH"}</small><strong>{jobLabel} · {statusLabel(status)}</strong></span></div>
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
