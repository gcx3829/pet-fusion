interface RenderLocalFusionInput {
  originalSrc: string;
  generatedSrc: string;
  mask: Blob;
  width: number;
  height: number;
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`无法加载 Fusion mock 图片：${src}`));
    image.src = src;
  });
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => canvas.toBlob((blob) => {
    if (blob) resolve(blob);
    else reject(new Error("浏览器无法编码 Fusion mock PNG"));
  }, "image/png"));
}

/** Browser-only demo compositor. Alpha 0 keeps source; alpha 255 reveals Raw. */
export async function renderLocalFusion({ originalSrc, generatedSrc, mask, width, height }: RenderLocalFusionInput): Promise<Blob> {
  if (width <= 0 || height <= 0 || width * height > 40_000_000) {
    throw new Error("Fusion mock 画布尺寸无效或超过 4000 万像素");
  }
  const maskUrl = URL.createObjectURL(mask);
  try {
    const [original, generated, maskImage] = await Promise.all([
      loadImage(originalSrc),
      loadImage(generatedSrc),
      loadImage(maskUrl),
    ]);
    const output = document.createElement("canvas");
    const layer = document.createElement("canvas");
    output.width = layer.width = width;
    output.height = layer.height = height;
    const outputContext = output.getContext("2d");
    const layerContext = layer.getContext("2d");
    if (!outputContext || !layerContext) throw new Error("浏览器不支持 Fusion mock Canvas");

    outputContext.drawImage(original, 0, 0, width, height);
    layerContext.drawImage(generated, 0, 0, width, height);
    layerContext.globalCompositeOperation = "destination-in";
    layerContext.drawImage(maskImage, 0, 0, width, height);
    outputContext.drawImage(layer, 0, 0);
    return await canvasBlob(output);
  } finally {
    URL.revokeObjectURL(maskUrl);
  }
}

