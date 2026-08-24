import { useId, useMemo, useState, type DragEvent } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../../components/Icon";
import { fileSizeLabel, selectImageFiles, useObjectUrl } from "../../lib/files";
import type { SourceDraft } from "../../types";
import type { AssetLayout } from "../workbench/useWorkbenchUi";

export const ASSET_DRAG_TYPE = "application/x-pet-fusion-asset";

export function localAssetKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

interface AssetBrowserProps {
  value: SourceDraft;
  onChange: (value: SourceDraft) => void;
  locked: boolean;
  onReset: () => void;
  layout: AssetLayout;
  onLayoutChange: (layout: AssetLayout) => void;
}

function dedupeFiles(files: File[]): File[] {
  return [...new Map(files.map((file) => [localAssetKey(file), file])).values()];
}

interface AssetPreview {
  src: string;
  label: string;
  anchor: { top: number; right: number; bottom: number };
}

function AssetItem({ file, layout, role, disabled, onPreview, onAssignBackground, onAssignReference }: {
  file: File;
  layout: AssetLayout;
  role?: "background" | "reference";
  disabled: boolean;
  onPreview: (preview: AssetPreview | null) => void;
  onAssignBackground: () => void;
  onAssignReference: () => void;
}) {
  const url = useObjectUrl(file);
  const showPreview = (element: HTMLElement) => {
    if (!url) return;
    const bounds = element.getBoundingClientRect();
    onPreview({
      src: url,
      label: file.name,
      anchor: { top: bounds.top, right: bounds.right, bottom: bounds.bottom },
    });
  };
  return (
    <div
      className={`library-asset ${layout === "grid" ? "is-grid" : ""}`}
      draggable={!disabled}
      data-asset-key={localAssetKey(file)}
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData(ASSET_DRAG_TYPE, localAssetKey(file));
        event.dataTransfer.setData("text/plain", file.name);
      }}
      onMouseEnter={(event) => showPreview(event.currentTarget)}
      onMouseLeave={() => onPreview(null)}
      onFocus={(event) => showPreview(event.currentTarget)}
      onBlur={() => onPreview(null)}
    >
      <button className="library-asset-preview" type="button" title={file.name}>
        {url ? <img src={url} alt="" /> : <Icon name="image" />}
      </button>
      <span className="library-asset-copy"><strong>{file.name}</strong><small>{fileSizeLabel(file.size)}</small></span>
      {role && <span className={`library-role is-${role}`}>{role === "background" ? "底片" : "参考"}</span>}
      {!disabled && <span className="library-asset-actions">
        <button type="button" onClick={onAssignBackground} aria-label={`将 ${file.name} 设为底片`}>底片</button>
        <button type="button" onClick={onAssignReference} aria-label={`将 ${file.name} 加入宠物参考`}>参考</button>
      </span>}
    </div>
  );
}

function AssetThumb({ file }: { file: File }) {
  const url = useObjectUrl(file);
  return url ? <img src={url} alt="" /> : <Icon name="image" />;
}

export function AssetBrowser({ value, onChange, locked, onReset, layout, onLayoutChange }: AssetBrowserProps) {
  const inputId = useId();
  const folderInputId = useId();
  const [preview, setPreview] = useState<AssetPreview | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [preparing, setPreparing] = useState(false);
  const library = useMemo(
    () => dedupeFiles([...(value.assets ?? []), ...(value.background ? [value.background] : []), ...value.references]),
    [value.assets, value.background, value.references],
  );
  const groups = useMemo(() => {
    const result = new Map<string, File[]>();
    for (const file of library) {
      const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath ?? "";
      const folder = relativePath.includes("/") ? relativePath.split("/")[0] : "项目素材";
      result.set(folder, [...(result.get(folder) ?? []), file]);
    }
    return [...result.entries()];
  }, [library]);

  const updateLibrary = (assets: File[]) => onChange({ ...value, assets: dedupeFiles(assets) });
  const addFiles = async (files: File[]) => {
    const images = selectImageFiles(files);
    if (!images.length) { setMessage("请选择 JPEG、PNG 或 WebP 图片"); return; }
    setPreparing(true);
    updateLibrary([...library, ...images]);
    setMessage(`已导入 ${images.length} 张`);
    setPreparing(false);
  };
  const assignBackground = (file: File) => {
    if (!locked) onChange({ ...value, assets: library, background: file });
  };
  const assignReference = (file: File) => {
    if (locked || value.references.some((item) => localAssetKey(item) === localAssetKey(file))) return;
    if (value.references.length >= 5) { setMessage("宠物参考最多 5 张"); return; }
    onChange({ ...value, assets: library, references: [...value.references, file] });
  };

  return (
    <div className="asset-browser" id="sidebar-panel-assets" role="tabpanel" aria-label="素材">
      <div className="asset-browser-heading">
        <div><h2>素材</h2></div>
        <div className="asset-layout-toggle" role="group" aria-label="素材布局">
          <button type="button" aria-label="列表" title="列表" aria-pressed={layout === "list"} onClick={() => onLayoutChange("list")}>列</button>
          <button type="button" aria-label="网格" title="网格" aria-pressed={layout === "grid"} onClick={() => onLayoutChange("grid")}>格</button>
        </div>
      </div>

      <input className="sr-only" id={inputId} type="file" multiple accept="image/jpeg,image/png,image/webp" onChange={async (event) => {
        const input = event.currentTarget;
        if (input.files) await addFiles(Array.from(input.files));
        input.value = "";
      }} />
      <input className="sr-only" id={folderInputId} type="file" multiple accept="image/jpeg,image/png,image/webp" {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} onChange={async (event) => {
        const input = event.currentTarget;
        if (input.files) await addFiles(Array.from(input.files));
        input.value = "";
      }} />
      <div className="library-import-row">
        <label className="library-import" htmlFor={inputId} aria-disabled={preparing}><Icon name="plus" /><span>{preparing ? "导入中…" : "导入"}</span></label>
        <label className="library-import library-import--folder" htmlFor={folderInputId} aria-disabled={preparing}><Icon name="plus" /><span>文件夹</span></label>
      </div>

      <div
        className={`reference-drop-slot ${value.references.length ? "has-items" : ""}`}
        onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }}
        onDrop={(event) => {
          event.preventDefault();
          const key = event.dataTransfer.getData(ASSET_DRAG_TYPE);
          const file = library.find((item) => localAssetKey(item) === key);
          if (file) assignReference(file);
        }}
        data-testid="reference-drop-slot"
      >
        <span><Icon name="image" /> 宠物参考</span><strong>{value.references.length} / 5</strong>
        <div className="reference-slot-thumbs">
          {value.references.map((file) => <button key={localAssetKey(file)} type="button" title={`移除 ${file.name}`} disabled={locked} onClick={() => onChange({ ...value, references: value.references.filter((item) => item !== file) })}><AssetThumb file={file} /></button>)}
          {!value.references.length && <small>拖入参考图</small>}
        </div>
      </div>

      <div className={`library-list library-list--${layout}`} aria-label="素材列表">
        {groups.map(([folder, files]) => <section className="library-group" key={folder} aria-label={folder}>
          <header><span>{folder}</span><small>{files.length}</small></header>
          <div className={`library-group-items library-group-items--${layout}`}>{files.map((file) => {
          const key = localAssetKey(file);
          const role = value.background && localAssetKey(value.background) === key
            ? "background"
            : value.references.some((item) => localAssetKey(item) === key) ? "reference" : undefined;
          return <AssetItem key={key} file={file} layout={layout} role={role} disabled={locked} onPreview={setPreview} onAssignBackground={() => assignBackground(file)} onAssignReference={() => assignReference(file)} />;
        })}</div></section>)}
        {!library.length && <div className="library-empty"><Icon name="image" /><strong>导入图片</strong></div>}
      </div>
      {message && <p className="field-message" role="status">{message}</p>}
      {locked && <button className="text-button reset-button" type="button" onClick={onReset}>新建任务</button>}
      {preview && typeof document !== "undefined" && createPortal(
        <div
          className="asset-hover-preview"
          role="status"
          style={{
            left: Math.min(preview.anchor.right + 12, Math.max(12, window.innerWidth - 452)),
            top: Math.min(
              Math.max(12, preview.anchor.top - 48),
              Math.max(12, window.innerHeight - 512),
            ),
          }}
        >
          <img src={preview.src} alt="" />
          <span>{preview.label}</span>
        </div>,
        document.body,
      )}
    </div>
  );
}
