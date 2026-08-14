import { useId, useState } from "react";
import { Icon } from "../../components/Icon";
import {
  fileSizeLabel,
  prepareImageForUpload,
  selectImageFiles,
  uploadPreparationLabel,
  useObjectUrl,
} from "../../lib/files";
import type { ProjectRecord, SourceDraft } from "../../types";

interface SourcePanelProps {
  value: SourceDraft;
  onChange: (value: SourceDraft) => void;
  project: ProjectRecord | null;
  locked: boolean;
  onReset: () => void;
}

function FilePreview({ file, alt }: { file: File; alt: string }) {
  const url = useObjectUrl(file);
  return url ? <img src={url} alt={alt} /> : null;
}

function shortHash(hash?: string): string {
  return hash ? `${hash.slice(0, 7)}…${hash.slice(-5)}` : "已写入内容寻址存储";
}

export function SourcePanel({
  value,
  onChange,
  project,
  locked,
  onReset,
}: SourcePanelProps) {
  const backgroundInputId = useId();
  const referenceInputId = useId();
  const backgroundUrl = useObjectUrl(value.background);
  const [fileMessage, setFileMessage] = useState<string | null>(null);
  const [isPreparing, setIsPreparing] = useState(false);

  const addReferences = async (files: File[]) => {
    const images = selectImageFiles(files);
    if (!images.length) {
      setFileMessage("请选择 JPEG、PNG 或 WebP 图片");
      return;
    }
    setIsPreparing(true);
    setFileMessage("正在优化参考图，请稍候……");
    try {
      const unique = [...value.references];
      const compressedLabels: string[] = [];
      for (const file of images) {
        const duplicate = unique.some((current) =>
          current.name === file.name
          && current.size === file.size
          && current.lastModified === file.lastModified,
        );
        if (duplicate || unique.length >= 5) continue;
        const prepared = await prepareImageForUpload(file, "reference");
        unique.push(prepared.file);
        if (prepared.compressed) {
          compressedLabels.push(
            `${file.name} ${fileSizeLabel(prepared.originalBytes)} → ${fileSizeLabel(prepared.file.size)}`,
          );
        }
      }
      const limited = images.length + value.references.length > 5;
      setFileMessage(
        compressedLabels.length
          ? `已优化 ${compressedLabels.join("；")}${limited ? "；参考图最多保留 5 张" : ""}`
          : limited ? "参考图最多 5 张，已保留前 5 张" : null,
      );
      onChange({ ...value, references: unique });
    } catch (error) {
      setFileMessage(error instanceof Error ? error.message : "参考图处理失败");
    } finally {
      setIsPreparing(false);
    }
  };

  return (
    <section className="panel source-panel" aria-labelledby="source-heading">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">01 / SOURCE NEGATIVES</p>
          <h2 id="source-heading">不可变原片</h2>
        </div>
        {project ? (
          <span className="seal-badge" title={project.source_manifest?.manifest_hash}>
            <Icon name="lock" /> 已封存
          </span>
        ) : (
          <span className="panel-count">1 + 1—5</span>
        )}
      </div>

      {project && (
        <div className="manifest-strip" role="status">
          <span className="manifest-dot" />
          <div>
            <strong>Source Manifest</strong>
            <code>{shortHash(project.source_manifest?.manifest_hash)}</code>
          </div>
        </div>
      )}

      <fieldset disabled={locked || isPreparing} className="source-fields" aria-busy={isPreparing}>
        <legend className="sr-only">上传摄影素材</legend>
        <div className="field-label-row">
          <label htmlFor={backgroundInputId}>旅行原片</label>
          <span>BASE · 1 张</span>
        </div>
        <input
          className="sr-only"
          id={backgroundInputId}
          type="file"
          accept="image/jpeg,image/png,image/webp"
          onChange={async (event) => {
            const input = event.currentTarget;
            const selected = event.target.files?.[0] ?? null;
            if (selected && !selectImageFiles([selected]).length) {
              setFileMessage("旅行原片需为 JPEG、PNG 或 WebP");
              return;
            }
            if (!selected) {
              onChange({ ...value, background: null });
              return;
            }
            setIsPreparing(true);
            setFileMessage("正在优化旅行原片，请稍候……");
            try {
              const prepared = await prepareImageForUpload(selected, "background");
              setFileMessage(prepared.compressed
                ? `已优化 ${selected.name}：${fileSizeLabel(prepared.originalBytes)} → ${fileSizeLabel(prepared.file.size)}`
                : null);
              onChange({ ...value, background: prepared.file });
            } catch (error) {
              setFileMessage(error instanceof Error ? error.message : "旅行原片处理失败");
            } finally {
              setIsPreparing(false);
              input.value = "";
            }
          }}
        />

        {value.background && backgroundUrl ? (
          <div className="source-file source-file--hero">
            <img src={backgroundUrl} alt="旅行原片预览" />
            <div className="source-file-shade" />
            <div className="source-file-meta">
              <span>ORIGINAL FRAME</span>
              <strong>{value.background.name}</strong>
              <small>{uploadPreparationLabel(value.background)}</small>
            </div>
            {!locked && (
              <button
                className="icon-button source-remove"
                type="button"
                aria-label="移除旅行原片"
                onClick={() => onChange({ ...value, background: null })}
              >
                <Icon name="trash" />
              </button>
            )}
          </div>
        ) : (
          <label className="upload-drop upload-drop--hero" htmlFor={backgroundInputId}>
            <span className="upload-icon"><Icon name="image" /></span>
            <strong>装入旅行原片</strong>
            <small>JPEG / PNG / WebP · 大图自动优化</small>
          </label>
        )}

        <div className="field-label-row reference-heading">
          <label htmlFor={referenceInputId}>同一只宠物的参考</label>
          <span>{value.references.length} / 5</span>
        </div>
        <input
          className="sr-only"
          id={referenceInputId}
          type="file"
          multiple
          accept="image/jpeg,image/png,image/webp"
          onChange={async (event) => {
            const input = event.currentTarget;
            if (input.files) await addReferences(Array.from(input.files));
            input.value = "";
          }}
        />
        <div className="reference-grid">
          {value.references.map((file, index) => (
            <article className="reference-card" key={`${file.name}-${file.lastModified}`}>
              <FilePreview file={file} alt={`宠物参考 ${index + 1}`} />
              <span className="reference-index">{String(index + 1).padStart(2, "0")}</span>
              {index === 0 && <span className="primary-flag">主参考</span>}
              <span className="sr-only">{uploadPreparationLabel(file)}</span>
              {!locked && (
                <button
                  className="icon-button reference-remove"
                  type="button"
                  aria-label={`移除宠物参考 ${index + 1}`}
                  onClick={() => onChange({
                    ...value,
                    references: value.references.filter((_, itemIndex) => itemIndex !== index),
                  })}
                >
                  <Icon name="trash" />
                </button>
              )}
            </article>
          ))}
          {value.references.length < 5 && (
            <label className="upload-drop upload-drop--reference" htmlFor={referenceInputId}>
              <Icon name="plus" />
              <span>添加角度</span>
            </label>
          )}
        </div>

        <div className="form-grid">
          <label className="text-field">
            <span>宠物名字 <small>可选</small></span>
            <input
              value={value.catName}
              maxLength={80}
              placeholder="例如：栗子"
              onChange={(event) => onChange({ ...value, catName: event.target.value })}
            />
          </label>
          <label className="text-field">
            <span>身份特征 <small>可选</small></span>
            <textarea
              value={value.catTraits}
              maxLength={500}
              rows={3}
              placeholder="左眼下方有白色泪痕，尾巴末端较深……"
              onChange={(event) => onChange({ ...value, catTraits: event.target.value })}
            />
          </label>
        </div>
      </fieldset>

      {fileMessage && <p className="field-message" role="alert">{fileMessage}</p>}
      {locked && (
        <button className="text-button reset-button" type="button" onClick={onReset}>
          更换素材并新建任务
        </button>
      )}
    </section>
  );
}
