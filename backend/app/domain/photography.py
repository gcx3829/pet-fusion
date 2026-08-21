"""Checkpoint-safe photographic metadata used by prompt refinement.

Only a curated subset of EXIF is exposed.  Raw EXIF bytes remain attached to
the immutable source asset for export and never enter LangGraph state.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BackgroundCaptureMetadata(BaseModel):
    """Locally extracted capture facts and conservative derived values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["background-capture-metadata/v1"] = "background-capture-metadata/v1"
    source_asset_id: str = Field(min_length=1, max_length=120)
    source_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pixel_width: int = Field(gt=0)
    pixel_height: int = Field(gt=0)
    camera_make: str | None = Field(default=None, max_length=160)
    camera_model: str | None = Field(default=None, max_length=160)
    lens_model: str | None = Field(default=None, max_length=200)
    captured_at_local: str | None = Field(default=None, max_length=40)
    utc_offset: str | None = Field(default=None, max_length=12)
    gps_latitude: float | None = Field(default=None, ge=-90, le=90)
    gps_longitude: float | None = Field(default=None, ge=-180, le=180)
    gps_altitude_m: float | None = Field(default=None, ge=-20_000, le=100_000)
    focal_length_mm: float | None = Field(default=None, gt=0, le=10_000)
    focal_length_35mm_equivalent_mm: float | None = Field(default=None, gt=0, le=10_000)
    horizontal_field_of_view_degrees: float | None = Field(default=None, gt=0, lt=180)
    aperture_f_number: float | None = Field(default=None, gt=0, le=256)
    exposure_time_seconds: float | None = Field(default=None, gt=0, le=86_400)
    iso: int | None = Field(default=None, gt=0, le=10_000_000)
    exposure_bias_ev: float | None = Field(default=None, ge=-100, le=100)
    exposure_program: str | None = Field(default=None, max_length=80)
    metering_mode: str | None = Field(default=None, max_length=80)
    white_balance_mode: str | None = Field(default=None, max_length=80)
    light_source: str | None = Field(default=None, max_length=80)
    flash: str | None = Field(default=None, max_length=120)
    color_space: str | None = Field(default=None, max_length=80)

    @property
    def has_capture_data(self) -> bool:
        return any(
            value is not None
            for name, value in self.model_dump(mode="json").items()
            if name
            not in {
                "schema_version",
                "source_asset_id",
                "source_asset_sha256",
                "pixel_width",
                "pixel_height",
            }
        )
