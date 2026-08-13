from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Literal

from PIL import Image, ImageChops

from app.domain.assets import AssetRef
from app.domain.compositing import CompositeResult, CropMapping, Mask, PixelBox
from app.domain.searches import PlacementIntent
from app.services.asset_store import AssetStore


def normalized_placement_to_pixel_box(
    placement: PlacementIntent, *, width: int, height: int
) -> PixelBox:
    """Convert the UI's normalized placement rectangle to an in-bounds pixel box.

    The left/top edges round outward with ``floor`` and the right/bottom edges with
    ``ceil``. That protects every source pixel represented by the user's placement,
    including fractional edge pixels.
    """

    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    left = math.floor(placement.x * width)
    top = math.floor(placement.y * height)
    right = min(width, math.ceil((placement.x + placement.width) * width))
    bottom = min(height, math.ceil((placement.y + placement.height) * height))
    if right <= left or bottom <= top:
        raise ValueError("placement does not cover a source pixel")
    return PixelBox(x=left, y=top, width=right - left, height=bottom - top)


def _png_bytes(
    image: Image.Image,
    *,
    icc_profile: bytes | None = None,
    exif: bytes | None = None,
) -> bytes:
    output = io.BytesIO()
    save_options: dict[str, object] = {
        "format": "PNG",
        "compress_level": 9,
        "optimize": False,
    }
    if icc_profile is not None:
        save_options["icc_profile"] = icc_profile
    if exif is not None:
        save_options["exif"] = exif
    image.save(output, **save_options)  # type: ignore[arg-type]
    return output.getvalue()


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    icc_profile: bytes | None
    exif: bytes | None


class CompositeFloorService:
    """Apply an auditable local pixel floor over generated image candidates.

    Generated pixels are allowed only where a stored full-resolution alpha mask is
    non-zero. The alpha feather is entirely inside the allowed rectangle, so every
    pixel outside it remains byte-for-byte equal to the immutable background.
    """

    def __init__(self, asset_store: AssetStore) -> None:
        self.asset_store = asset_store

    @staticmethod
    def source_metadata(source_background: AssetRef) -> ImageMetadata:
        with Image.open(source_background.filesystem_path) as source:
            icc_profile = source.info.get("icc_profile")
            exif = source.info.get("exif")
        return ImageMetadata(
            icc_profile=icc_profile if isinstance(icc_profile, bytes) else None,
            exif=exif if isinstance(exif, bytes) else None,
        )

    @staticmethod
    def _full_resolution_mask(
        *,
        width: int,
        height: int,
        allowed_box: PixelBox,
        feather_radius_px: int,
    ) -> Image.Image:
        """Build a deterministic inside-only linear feather mask.

        With a non-zero feather, alpha is proportional to the distance from the
        rectangle edge measured at pixel centres. Pixels outside ``allowed_box``
        are always exactly zero; the interior reaches 255 after ``feather_radius``
        pixels. A zero radius produces a hard-edged, fully opaque rectangle.
        """

        if allowed_box.right > width or allowed_box.bottom > height:
            raise ValueError("mask box exceeds the output image bounds")
        mask = Image.new("L", (width, height), 0)
        if feather_radius_px == 0:
            mask.paste(255, (allowed_box.x, allowed_box.y, allowed_box.right, allowed_box.bottom))
            return mask

        def alpha_at(distance: float) -> int:
            return round(255 * min(1.0, distance / feather_radius_px))

        # Two linear one-dimensional ramps plus `darker` reproduce the square
        # inside-only feather without a Python loop over potentially 40M pixels.
        x_ramp = Image.new("L", (allowed_box.width, 1))
        x_ramp.putdata(
            [
                alpha_at(min(index + 0.5, allowed_box.width - (index + 0.5)))
                for index in range(allowed_box.width)
            ]
        )
        y_ramp = Image.new("L", (1, allowed_box.height))
        y_ramp.putdata(
            [
                alpha_at(min(index + 0.5, allowed_box.height - (index + 0.5)))
                for index in range(allowed_box.height)
            ]
        )
        horizontal = x_ramp.resize((allowed_box.width, allowed_box.height))
        vertical = y_ramp.resize((allowed_box.width, allowed_box.height))
        mask.paste(ImageChops.darker(horizontal, vertical), (allowed_box.x, allowed_box.y))
        return mask

    def create_mask(
        self,
        *,
        source_background: AssetRef,
        placement: PlacementIntent,
        feather_radius_px: int = 2,
    ) -> Mask:
        if feather_radius_px < 0:
            raise ValueError("feather_radius_px must be non-negative")
        self.asset_store.assert_intact(source_background)
        allowed_box = normalized_placement_to_pixel_box(
            placement,
            width=source_background.width,
            height=source_background.height,
        )
        image = self._full_resolution_mask(
            width=source_background.width,
            height=source_background.height,
            allowed_box=allowed_box,
            feather_radius_px=feather_radius_px,
        )
        mask_asset = self.asset_store.put_image_bytes(_png_bytes(image))
        return Mask(
            asset=mask_asset,
            allowed_box=allowed_box,
            feather_radius_px=feather_radius_px,
        )

    def _load_mask(self, mask: Mask, *, size: tuple[int, int]) -> Image.Image:
        self.asset_store.assert_intact(mask.asset)
        if (mask.asset.width, mask.asset.height) != size:
            raise ValueError("composite mask dimensions do not match the background")
        with Image.open(mask.asset.filesystem_path) as opened:
            return opened.convert("L")

    @staticmethod
    def _candidate_on_full_canvas(
        *,
        background: Image.Image,
        candidate: Image.Image,
        crop_mapping: CropMapping | None,
    ) -> Image.Image:
        background_rgba = background.convert("RGBA")
        if candidate.size == background.size:
            full_candidate = background_rgba.copy()
            full_candidate.alpha_composite(candidate.convert("RGBA"))
            return full_candidate
        if crop_mapping is None:
            raise ValueError(
                "A generated candidate that is not full-resolution requires a crop mapping"
            )
        if (crop_mapping.full_width, crop_mapping.full_height) != background.size:
            raise ValueError("crop mapping does not describe the immutable background")
        if candidate.size != (crop_mapping.canvas_width, crop_mapping.canvas_height):
            raise ValueError("candidate dimensions do not match the crop mapping canvas")
        content_box = crop_mapping.canvas_content_box
        content = candidate.crop(
            (content_box.x, content_box.y, content_box.right, content_box.bottom)
        ).convert("RGBA")
        resampled = content.resize(
            (crop_mapping.crop_box.width, crop_mapping.crop_box.height),
            Image.Resampling.LANCZOS,
        )
        background_rgba.alpha_composite(
            resampled,
            dest=(crop_mapping.crop_box.x, crop_mapping.crop_box.y),
        )
        return background_rgba

    @staticmethod
    def outside_mask_is_exact(
        *, background: Image.Image, protected: Image.Image, mask: Image.Image
    ) -> bool:
        """Check only alpha-zero mask pixels, including their alpha channel."""

        background_rgba = background.convert("RGBA")
        protected_rgba = protected.convert("RGBA")
        difference = ImageChops.difference(background_rgba, protected_rgba)
        zero_mask = mask.convert("L").point(lambda alpha: 255 if alpha == 0 else 0)
        outside_difference = Image.composite(
            difference, Image.new("RGBA", difference.size), zero_mask
        )
        return outside_difference.getbbox(alpha_only=False) is None

    def protect_candidate(
        self,
        *,
        source_manifest_hash: str,
        source_background: AssetRef,
        raw_candidate: AssetRef,
        placement: PlacementIntent,
        crop_mapping: CropMapping | None = None,
        mask: Mask | None = None,
        feather_radius_px: int = 2,
        copy_icc: bool = True,
        copy_exif: bool = True,
    ) -> CompositeResult:
        """Create a full-resolution protected PNG from a raw generated candidate."""

        self.asset_store.assert_intact(source_background)
        self.asset_store.assert_intact(raw_candidate)
        resolved_mask = mask or self.create_mask(
            source_background=source_background,
            placement=placement,
            feather_radius_px=feather_radius_px,
        )
        with Image.open(source_background.filesystem_path) as source_opened:
            background_mode: Literal["RGB", "RGBA"] = (
                "RGBA" if "A" in source_opened.getbands() else "RGB"
            )
            background = source_opened.convert("RGBA")
        with Image.open(raw_candidate.filesystem_path) as candidate_opened:
            generated_full = self._candidate_on_full_canvas(
                background=background,
                candidate=candidate_opened,
                crop_mapping=crop_mapping,
            )
        mask_image = self._load_mask(resolved_mask, size=background.size)
        protected_rgba = Image.composite(generated_full, background, mask_image)
        protected = protected_rgba.convert(background_mode)
        exact = self.outside_mask_is_exact(
            background=background, protected=protected, mask=mask_image
        )
        if not exact:
            raise RuntimeError("Composite floor failed to preserve pixels outside its mask")
        metadata = self.source_metadata(source_background)
        protected_asset = self.asset_store.put_image_bytes(
            _png_bytes(
                protected,
                icc_profile=metadata.icc_profile if copy_icc else None,
                exif=metadata.exif if copy_exif else None,
            )
        )
        return CompositeResult(
            source_manifest_hash=source_manifest_hash,
            source_background=source_background,
            raw_candidate=raw_candidate,
            protected_asset=protected_asset,
            mask=resolved_mask,
            crop_mapping=crop_mapping,
            outside_mask_exact=exact,
        )
