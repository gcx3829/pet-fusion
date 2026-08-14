import { useEffect, useState } from "react";

export const ACCEPTED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export type UploadImageRole = "background" | "reference";

interface UploadPreparationMeta {
  compressed: boolean;
  originalBytes: number;
  originalName: string;
  width: number;
  height: number;
}

interface DecodedUploadImage {
  source: CanvasImageSource;
  width: number;
  height: number;
  close?: () => void;
}

interface ImageCompressionRuntime {
  decode: (file: File) => Promise<DecodedUploadImage>;
  encode: (
    image: DecodedUploadImage,
    width: number,
    height: number,
    mimeType: string,
    quality: number,
  ) => Promise<Blob>;
}

export interface PreparedUploadImage {
  file: File;
  compressed: boolean;
  originalBytes: number;
  width: number;
  height: number;
}

const uploadPreparation = new WeakMap<File, UploadPreparationMeta>();

const ROLE_LIMITS: Record<UploadImageRole, {
  targetBytes: number;
  maxPixels: number;
  maxSide: number;
}> = {
  background: {
    targetBytes: 18 * 1024 * 1024,
    maxPixels: 32_000_000,
    maxSide: 8_192,
  },
  reference: {
    targetBytes: 8 * 1024 * 1024,
    maxPixels: 8_000_000,
    maxSide: 4_096,
  },
};

const QUALITY_STEPS = [0.92, 0.86, 0.8, 0.74];

export function fitImageDimensions(
  width: number,
  height: number,
  maxPixels: number,
  maxSide: number,
): { width: number; height: number } {
  if (width <= 0 || height <= 0) throw new Error("图片尺寸无效");
  const sideScale = Math.min(1, maxSide / Math.max(width, height));
  const pixelScale = Math.min(1, Math.sqrt(maxPixels / (width * height)));
  const scale = Math.min(sideScale, pixelScale);
  return {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  };
}

function outputMimeType(file: File): "image/jpeg" | "image/webp" {
  return file.type === "image/jpeg" ? "image/jpeg" : "image/webp";
}

function uploadFilename(name: string, mimeType: string): string {
  const base = name.replace(/\.[^.]+$/, "") || "pet-fusion-image";
  const extension = mimeType === "image/jpeg"
    ? "jpg"
    : mimeType === "image/png" ? "png" : "webp";
  return `${base}.${extension}`;
}

async function decodeInBrowser(file: File): Promise<DecodedUploadImage> {
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    return {
      source: bitmap,
      width: bitmap.width,
      height: bitmap.height,
      close: () => bitmap.close(),
    };
  }

  const objectUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.decoding = "async";
    image.src = objectUrl;
    await image.decode();
    return {
      source: image,
      width: image.naturalWidth,
      height: image.naturalHeight,
    };
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function encodeInBrowser(
  image: DecodedUploadImage,
  width: number,
  height: number,
  mimeType: string,
  quality: number,
): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { alpha: mimeType !== "image/jpeg" });
  if (!context) throw new Error("当前浏览器无法处理图片画布");
  if (mimeType === "image/jpeg") {
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, width, height);
  }
  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = "high";
  context.drawImage(image.source, 0, 0, width, height);
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("浏览器图片编码失败")),
      mimeType,
      quality,
    );
  });
}

const browserCompressionRuntime: ImageCompressionRuntime = {
  decode: decodeInBrowser,
  encode: encodeInBrowser,
};

export async function prepareImageForUpload(
  file: File,
  role: UploadImageRole,
  runtime: ImageCompressionRuntime = browserCompressionRuntime,
): Promise<PreparedUploadImage> {
  if (!ACCEPTED_IMAGE_TYPES.includes(file.type)) {
    throw new Error("请选择 JPEG、PNG 或 WebP 图片");
  }
  const existing = uploadPreparation.get(file);
  if (existing) {
    return {
      file,
      compressed: existing.compressed,
      originalBytes: existing.originalBytes,
      width: existing.width,
      height: existing.height,
    };
  }

  let decoded: DecodedUploadImage;
  try {
    decoded = await runtime.decode(file);
  } catch {
    throw new Error(`无法读取图片：${file.name}`);
  }

  try {
    const limits = ROLE_LIMITS[role];
    const fitted = fitImageDimensions(
      decoded.width,
      decoded.height,
      limits.maxPixels,
      limits.maxSide,
    );
    const requiresResize = fitted.width !== decoded.width || fitted.height !== decoded.height;
    if (!requiresResize && file.size <= limits.targetBytes) {
      const result = {
        file,
        compressed: false,
        originalBytes: file.size,
        width: decoded.width,
        height: decoded.height,
      };
      uploadPreparation.set(file, {
        compressed: false,
        originalBytes: file.size,
        originalName: file.name,
        width: decoded.width,
        height: decoded.height,
      });
      return result;
    }

    const requestedMimeType = outputMimeType(file);
    let width = fitted.width;
    let height = fitted.height;
    let best: { blob: Blob; width: number; height: number } | null = null;
    for (let resizeAttempt = 0; resizeAttempt < 4; resizeAttempt += 1) {
      for (const quality of QUALITY_STEPS) {
        const blob = await runtime.encode(
          decoded,
          width,
          height,
          requestedMimeType,
          quality,
        );
        if (!best || blob.size < best.blob.size) best = { blob, width, height };
        if (blob.size <= limits.targetBytes) {
          best = { blob, width, height };
          resizeAttempt = 4;
          break;
        }
      }
      if (best && best.blob.size <= limits.targetBytes) break;
      width = Math.max(1, Math.round(width * 0.85));
      height = Math.max(1, Math.round(height * 0.85));
    }

    if (!best || best.blob.size > MAX_UPLOAD_BYTES) {
      throw new Error(`${file.name} 压缩后仍超过 25 MB，请先在相册中缩小尺寸`);
    }
    const actualMimeType = ACCEPTED_IMAGE_TYPES.includes(best.blob.type)
      ? best.blob.type
      : requestedMimeType;
    const preparedFile = new File(
      [best.blob],
      uploadFilename(file.name, actualMimeType),
      { type: actualMimeType, lastModified: file.lastModified },
    );
    uploadPreparation.set(preparedFile, {
      compressed: true,
      originalBytes: file.size,
      originalName: file.name,
      width: best.width,
      height: best.height,
    });
    return {
      file: preparedFile,
      compressed: true,
      originalBytes: file.size,
      width: best.width,
      height: best.height,
    };
  } finally {
    decoded.close?.();
  }
}

export function uploadPreparationLabel(file: File): string {
  const meta = uploadPreparation.get(file);
  if (!meta?.compressed) return fileSizeLabel(file.size);
  return `${fileSizeLabel(meta.originalBytes)} → ${fileSizeLabel(file.size)}`;
}

export function useObjectUrl(file: File | null): string | null {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!file) {
      setUrl(null);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);

  return url;
}

export function fileSizeLabel(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function selectImageFiles(files: FileList | File[]): File[] {
  return Array.from(files).filter((file) => ACCEPTED_IMAGE_TYPES.includes(file.type));
}
