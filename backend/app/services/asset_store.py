from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

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
    """Content-addressed image storage with atomic file publication.

    Source and generation lineage are normalized to PNG. Final delivery assets use
    a separate ``exports/`` namespace so JPEG output can retain its chosen encoder
    settings without weakening the PNG-only generation invariant.
    """

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
                # We normalize every working asset to RGB/RGBA. Reusing a CMYK or
                # grayscale profile after that mode conversion would mislabel the
                # resulting pixels, so preserve profiles only for RGB-like inputs.
                if image.mode not in {"RGB", "RGBA", "P"}:
                    icc_profile = None
                exif = image.getexif()
                # Orientation has already been baked into the normalized pixel grid.
                # Retaining the original orientation tag would rotate an export again.
                if 274 in exif:
                    del exif[274]
                has_alpha = image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                )
                normalized = image.convert("RGBA" if has_alpha else "RGB")
                # Camera EXIF commonly records the encoded (pre-orientation) pixel
                # dimensions. Keep those tags only when present, but update them to
                # the normalized pixel grid that every downstream asset uses.
                for tag, value in (
                    (256, normalized.width),
                    (257, normalized.height),
                    (40962, normalized.width),
                    (40963, normalized.height),
                ):
                    if tag in exif:
                        exif[tag] = value
                exif_bytes = exif.tobytes() if len(exif) else None
                # Pillow carries source ``info`` across some mode conversions.
                # Clear it so rejected profiles or stale EXIF cannot be written
                # implicitly; only the validated metadata below is reattached.
                normalized.info.clear()
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
        if exif_bytes is not None and len(exif_bytes) <= 4 * 1024 * 1024:
            save_options["exif"] = exif_bytes
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
        self._publish_bytes(target=target, data=image.png_bytes)
        return AssetRef(
            asset_id=asset_id,
            path=str(target),
            sha256=image.sha256,
            mime_type="image/png",
            width=image.width,
            height=image.height,
        )

    def put_export_bytes(
        self,
        data: bytes,
        *,
        mime_type: Literal["image/png", "image/jpeg"],
    ) -> AssetRef:
        """Publish an already-encoded final image without normalizing it to PNG.

        The export service owns the Pillow encoder and metadata-copy policy. This
        store method only verifies the declared final format, derives a content
        address from the exact delivery bytes, and writes them atomically.
        """

        expected_format = "PNG" if mime_type == "image/png" else "JPEG"
        try:
            with Image.open(io.BytesIO(data)) as opened:
                if opened.format != expected_format:
                    raise ValueError("encoded export format does not match its MIME type")
                if getattr(opened, "is_animated", False):
                    raise ValueError("animated exports are not supported")
                width, height = opened.size
                if width <= 0 or height <= 0 or width * height > self.max_image_pixels:
                    raise ValueError("export dimensions exceed the image safety limit")
                opened.load()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError("Export is not a valid encoded image") from exc

        digest = hashlib.sha256(data).hexdigest()
        extension = "png" if mime_type == "image/png" else "jpg"
        target = (self.root / "exports" / digest[:2] / f"{digest}.{extension}").resolve()
        self._publish_bytes(target=target, data=data)
        return AssetRef(
            asset_id=f"exp_{digest[:32]}",
            path=str(target),
            sha256=digest,
            mime_type=mime_type,
            width=width,
            height=height,
        )

    def _publish_bytes(self, *, target: Path, data: bytes) -> None:
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
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()

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

    def assert_png_lineage_asset(self, asset: AssetRef) -> None:
        """Reject delivery assets or mislabeled bytes from generation lineage."""

        self.assert_intact(asset)
        path = asset.filesystem_path.resolve()
        export_root = (self.root / "exports").resolve()
        expected_path = (
            self.root / asset.sha256[:2] / f"{asset.sha256}.png"
        ).resolve()
        if (
            asset.mime_type != "image/png"
            or asset.asset_id != f"ast_{asset.sha256[:32]}"
            or path != expected_path
            or path.is_relative_to(export_root)
        ):
            raise SourceManifestMismatchError(
                f"Asset {asset.asset_id} is not an internal PNG lineage asset"
            )
        try:
            with Image.open(path) as opened:
                if opened.format != "PNG" or opened.size != (asset.width, asset.height):
                    raise SourceManifestMismatchError(
                        f"Asset {asset.asset_id} PNG metadata does not match its bytes"
                    )
        except (UnidentifiedImageError, OSError) as exc:
            raise SourceManifestMismatchError(
                f"Asset {asset.asset_id} is not a decodable PNG lineage asset"
            ) from exc

    def assert_export_asset(self, asset: AssetRef) -> None:
        """Ensure a public delivery asset remains isolated under ``exports/``."""

        self.assert_intact(asset)
        path = asset.filesystem_path.resolve()
        export_root = (self.root / "exports").resolve()
        expected_format = "PNG" if asset.mime_type == "image/png" else "JPEG"
        expected_suffix = ".png" if asset.mime_type == "image/png" else ".jpg"
        expected_path = (
            export_root / asset.sha256[:2] / f"{asset.sha256}{expected_suffix}"
        ).resolve()
        if (
            asset.mime_type not in {"image/png", "image/jpeg"}
            or asset.asset_id != f"exp_{asset.sha256[:32]}"
            or path != expected_path
        ):
            raise SourceManifestMismatchError(
                f"Asset {asset.asset_id} is not an isolated export asset"
            )
        try:
            with Image.open(path) as opened:
                if opened.format != expected_format or opened.size != (
                    asset.width,
                    asset.height,
                ):
                    raise SourceManifestMismatchError(
                        f"Export asset {asset.asset_id} metadata does not match its bytes"
                    )
        except (UnidentifiedImageError, OSError) as exc:
            raise SourceManifestMismatchError(
                f"Export asset {asset.asset_id} is not decodable"
            ) from exc
