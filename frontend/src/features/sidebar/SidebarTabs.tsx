import { useRef } from "react";
import { Icon } from "../../components/Icon";
import type { SidebarTab } from "../workbench/useWorkbenchUi";

interface SidebarTabsProps {
  value: SidebarTab;
  accepted: boolean;
  onChange: (tab: SidebarTab) => void;
}

const tabs: { value: SidebarTab; label: string; short: string; icon: "image" | "spark" | "check" | "lock" }[] = [
  { value: "assets", label: "素材", short: "原片与参考", icon: "image" },
  { value: "prompt", label: "生成", short: "意图与设置", icon: "spark" },
  { value: "review", label: "审片", short: "评分与选择", icon: "check" },
  { value: "fusion", label: "融合", short: "局部合成", icon: "lock" },
];

export function SidebarTabs({ value, accepted, onChange }: SidebarTabsProps) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const moveFocus = (currentIndex: number, direction: -1 | 1) => {
    for (let offset = 1; offset <= tabs.length; offset += 1) {
      const nextIndex = (currentIndex + direction * offset + tabs.length) % tabs.length;
      const next = tabs[nextIndex];
      if (next.value === "fusion" && !accepted) continue;
      onChange(next.value);
      tabRefs.current[nextIndex]?.focus();
      return;
    }
  };

  return (
    <nav className="sidebar-tabs" aria-label="工作台侧栏">
      <div className="sidebar-tabs-list" role="tablist" aria-orientation="horizontal">
        {tabs.map((tab, index) => {
          const disabled = tab.value === "fusion" && !accepted;
          return (
            <button
              className={`sidebar-tab ${value === tab.value ? "is-active" : ""}`}
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={value === tab.value}
              aria-controls={`sidebar-panel-${tab.value}`}
              disabled={disabled}
              tabIndex={value === tab.value ? 0 : -1}
              title={disabled ? "接受一张候选图后可用" : tab.label}
              onClick={() => onChange(tab.value)}
              ref={(element) => { tabRefs.current[index] = element; }}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight") {
                  event.preventDefault();
                  moveFocus(index, 1);
                } else if (event.key === "ArrowLeft") {
                  event.preventDefault();
                  moveFocus(index, -1);
                } else if (event.key === "Home" || event.key === "End") {
                  event.preventDefault();
                  const targetIndex = event.key === "Home" ? 0 : accepted ? tabs.length - 1 : tabs.length - 2;
                  onChange(tabs[targetIndex].value);
                  tabRefs.current[targetIndex]?.focus();
                }
              }}
            >
              <Icon name={tab.icon} />
              <span>{tab.label}</span>
              <small>{tab.short}</small>
              {disabled && <i aria-hidden="true" />}
            </button>
          );
        })}
      </div>
    </nav>
  );
}
