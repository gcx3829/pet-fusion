from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageOps, UnidentifiedImageError

from app.domain.assets import AssetRef
from app.domain.errors import SourceManifestMismatchError, UploadValidationError


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    png_bytes: bytes
    sha256: str
    width: int
    height: int


class AssetStore:
    """Content-addressed PNG storage with atomic file publication."""

    _ALLOWED_FORMATS: ClassVar[set[str]] = {"JPEG", "PNG", "WEBP"}

    def __init__(self, root: Path, *, max_image_pixels: int) -> None:
        self.root = root.resolve()
        self.max_image_pixels = max_image_pixels

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def normalize_image(self, data: bytes) -> NormalizedImage:
        try:
            with Image.open(io.BytesIO(data)) as opened:
                if opened.format not in self._ALLOWED_FORMATS:
                    raise UploadValidationError("Only JPEG, PNG, and WebP uploads are supported")
                if getattr(opened, "is_animated", False):
                    raise UploadValidationError("Animated images are not supported")
                width, height = opened.size
                if width <= 0 or height <= 0:
                    raise UploadValidationError("Image dimensions must be positive")
                if width * height > self.max_image_pixels:
                    raise UploadValidationError(
                        f"Image exceeds the {self.max_image_pixels} pixel safety limit"
                    )
                opened.load()
                image = ImageOps.exif_transpose(opened)
                icc_profile = opened.info.get("icc_profile")
                has_alpha = image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                )
                normalized = image.convert("RGBA" if has_alpha else "RGB")
        except UploadValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise UploadValidationError("Upload is not a decodable image") from exc

        output = io.BytesIO()
        save_options: dict[str, Any] = {
            "format": "PNG",
            "compress_level": 9,
            "optimize": False,
        }
        if isinstance(icc_profile, bytes) and len(icc_profile) <= 4 * 1024 * 1024:
            save_options["icc_profile"] = icc_profile
        normalized.save(output, **save_options)
        png_bytes = output.getvalue()
        digest = hashlib.sha256(png_bytes).hexdigest()
        return NormalizedImage(
            png_bytes=png_bytes,
            sha256=digest,
            width=normalized.width,
            height=normalized.height,
        )

    def put_image_bytes(self, data: bytes) -> AssetRef:
        return self.put_normalized(self.normalize_image(data))

    def put_normalized(self, image: NormalizedImage) -> AssetRef:
        asset_id = f"ast_{image.sha256[:32]}"
        target = (self.root / image.sha256[:2] / f"{image.sha256}.png").resolve()
        if not target.is_relative_to(self.root):
            raise UploadValidationError("Resolved asset path escaped the asset store")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            handle = tempfile.NamedTemporaryFile(
                prefix=".asset-", suffix=".tmp", dir=target.parent, delete=False
            )
            temporary = Path(handle.name)
            try:
                with handle:
                    handle.write(image.png_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return AssetRef(
            asset_id=asset_id,
            path=str(target),
            sha256=image.sha256,
            mime_type="image/png",
            width=image.width,
            height=image.height,
        )

    def assert_intact(self, asset: AssetRef) -> None:
        path = asset.filesystem_path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise SourceManifestMismatchError(
                f"Source asset {asset.asset_id} is unavailable or outside the asset store"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != asset.sha256:
            raise SourceManifestMismatchError(
                f"Source asset {asset.asset_id} content hash mismatch"
            )
