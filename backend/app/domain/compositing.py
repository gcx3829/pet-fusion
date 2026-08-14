from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef


class PixelBox(BaseModel):
    """An integer rectangle in image pixels, represented as an exclusive-end box."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


class FloatBox(BaseModel):
    """A float rectangle used while mapping between crop and full-size coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_finite_values(self) -> FloatBox:
        if not all(math.isfinite(value) for value in self.model_dump().values()):
            raise ValueError("coordinate values must be finite")
        return self


class CropPadding(BaseModel):
    """Pixels around a model crop that do not map back to source-image pixels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: int = Field(default=0, ge=0)
    top: int = Field(default=0, ge=0)
    right: int = Field(default=0, ge=0)
    bottom: int = Field(default=0, ge=0)


class CropMapping(BaseModel):
    """Checkpoint-safe mapping between a generated crop and its original photograph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["crop-mapping/v1"] = "crop-mapping/v1"
    full_width: int = Field(gt=0)
    full_height: int = Field(gt=0)
    crop_box: PixelBox
    canvas_width: int = Field(gt=0)
    canvas_height: int = Field(gt=0)
    padding: CropPadding = Field(default_factory=CropPadding)

    @model_validator(mode="after")
    def validate_mapping_bounds(self) -> CropMapping:
        if self.crop_box.right > self.full_width or self.crop_box.bottom > self.full_height:
            raise ValueError("crop_box must remain within the full-resolution image")
        if self.padding.left + self.padding.right >= self.canvas_width:
            raise ValueError("horizontal padding leaves no crop content")
        if self.padding.top + self.padding.bottom >= self.canvas_height:
            raise ValueError("vertical padding leaves no crop content")
        return self

    @property
    def content_width(self) -> int:
        return self.canvas_width - self.padding.left - self.padding.right

    @property
    def content_height(self) -> int:
        return self.canvas_height - self.padding.top - self.padding.bottom

    @property
    def canvas_content_box(self) -> PixelBox:
        return PixelBox(
            x=self.padding.left,
            y=self.padding.top,
            width=self.content_width,
            height=self.content_height,
        )

    def full_to_canvas(self, point: tuple[float, float]) -> tuple[float, float]:
        """Map a full-resolution point to the generated crop canvas."""

        full_x, full_y = point
        return (
            self.padding.left
            + ((full_x - self.crop_box.x) / self.crop_box.width) * self.content_width,
            self.padding.top
            + ((full_y - self.crop_box.y) / self.crop_box.height) * self.content_height,
        )

    def canvas_to_full(self, point: tuple[float, float]) -> tuple[float, float]:
        """Invert ``full_to_canvas`` without rounding away coordinate precision."""

        canvas_x, canvas_y = point
        return (
            self.crop_box.x
            + ((canvas_x - self.padding.left) / self.content_width) * self.crop_box.width,
            self.crop_box.y
            + ((canvas_y - self.padding.top) / self.content_height) * self.crop_box.height,
        )

    def full_box_to_canvas(self, box: FloatBox) -> FloatBox:
        left, top = self.full_to_canvas((box.x, box.y))
        right, bottom = self.full_to_canvas((box.x + box.width, box.y + box.height))
        return FloatBox(x=left, y=top, width=right - left, height=bottom - top)

    def canvas_box_to_full(self, box: FloatBox) -> FloatBox:
        left, top = self.canvas_to_full((box.x, box.y))
        right, bottom = self.canvas_to_full((box.x + box.width, box.y + box.height))
        return FloatBox(x=left, y=top, width=right - left, height=bottom - top)


class Mask(BaseModel):
    """A stored composite-floor alpha mask; bytes stay in its referenced asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["composite-mask/v1"] = "composite-mask/v1"
    asset: AssetRef
    coordinate_space: Literal["full_resolution"] = "full_resolution"
    allowed_box: PixelBox
    feather_radius_px: int = Field(ge=0)
    # ``placement`` is the tight user box. ``model_window`` is the deterministic
    # expanded legacy floor. ``full_frame`` is used only by Local Fix when its
    # root is a raw-first Search candidate: the user's tight mask then preserves
    # every raw pixel outside the requested repair without inventing a Search
    # floor after the fact.
    mask_scope: Literal["placement", "model_window", "full_frame"] = "placement"


class CompositeResult(BaseModel):
    """Auditable, checkpoint-safe result of applying the local composite floor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["composite-result/v1"] = "composite-result/v1"
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_background: AssetRef
    raw_candidate: AssetRef
    protected_asset: AssetRef
    mask: Mask
    crop_mapping: CropMapping | None = None
    outside_mask_exact: bool
