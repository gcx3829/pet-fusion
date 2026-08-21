"""Conservative EXIF extraction for the immutable background photograph."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from PIL import Image

from app.domain.assets import AssetRef
from app.domain.photography import BackgroundCaptureMetadata
from app.services.asset_store import AssetStore

_EXPOSURE_PROGRAMS = {
    0: "not_defined",
    1: "manual",
    2: "normal_program",
    3: "aperture_priority",
    4: "shutter_priority",
    5: "creative_program",
    6: "action_program",
    7: "portrait_mode",
    8: "landscape_mode",
}
_METERING_MODES = {
    0: "unknown",
    1: "average",
    2: "center_weighted_average",
    3: "spot",
    4: "multi_spot",
    5: "pattern",
    6: "partial",
    255: "other",
}
_LIGHT_SOURCES = {
    0: "unknown",
    1: "daylight",
    2: "fluorescent",
    3: "tungsten",
    4: "flash",
    9: "fine_weather",
    10: "cloudy_weather",
    11: "shade",
    12: "daylight_fluorescent",
    13: "day_white_fluorescent",
    14: "cool_white_fluorescent",
    15: "white_fluorescent",
    17: "standard_light_a",
    18: "standard_light_b",
    19: "standard_light_c",
    20: "d55",
    21: "d65",
    22: "d75",
    23: "d50",
    24: "iso_studio_tungsten",
    255: "other",
}
_COLOR_SPACES = {1: "sRGB", 65535: "uncalibrated"}


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]  # Pillow IFDRational
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return round(number)


def _text(value: object, *, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    result = " ".join(str(value).replace("\x00", " ").split())
    if not result or not result.isprintable():
        return None
    return result[:limit]


def _enum(value: object, mapping: Mapping[int, str]) -> str | None:
    key = _integer(value)
    if key is None:
        return None
    return mapping.get(key, f"exif_code_{key}")


def _dms_degrees(value: object, reference: object) -> float | None:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    parts = tuple(_number(item) for item in value)
    if any(item is None for item in parts):
        return None
    degrees, minutes, seconds = parts
    assert degrees is not None and minutes is not None and seconds is not None
    result = degrees + minutes / 60 + seconds / 3600
    ref = (_text(reference, limit=2) or "").upper()
    if ref in {"S", "W"}:
        result = -result
    return round(result, 7)


class PhotographyMetadataService:
    """Read a safe EXIF summary from one content-addressed source asset."""

    def __init__(self, *, asset_store: AssetStore) -> None:
        self.asset_store = asset_store

    def extract_background(self, background: AssetRef) -> BackgroundCaptureMetadata:
        self.asset_store.assert_intact(background)
        with Image.open(background.filesystem_path) as image:
            exif = image.getexif()
            gps: Mapping[int, Any]
            try:
                gps = exif.get_ifd(34853)
            except (KeyError, TypeError, ValueError, AttributeError):
                gps = {}

        focal_length = _number(exif.get(37386))
        focal_35mm = _number(exif.get(41989))
        horizontal_fov = (
            math.degrees(2 * math.atan(36.0 / (2 * focal_35mm)))
            if focal_35mm is not None and focal_35mm > 0
            else None
        )
        latitude = _dms_degrees(gps.get(2), gps.get(1))
        longitude = _dms_degrees(gps.get(4), gps.get(3))
        altitude = _number(gps.get(6))
        if altitude is not None and _integer(gps.get(5)) == 1:
            altitude = -altitude

        white_balance_code = _integer(exif.get(41987))
        white_balance = (
            {
                0: "auto",
                1: "manual",
            }.get(white_balance_code)
            if white_balance_code is not None
            else None
        )
        flash_code = _integer(exif.get(37385))

        return BackgroundCaptureMetadata(
            source_asset_id=background.asset_id,
            source_asset_sha256=background.sha256,
            pixel_width=background.width,
            pixel_height=background.height,
            camera_make=_text(exif.get(271), limit=160),
            camera_model=_text(exif.get(272), limit=160),
            lens_model=_text(exif.get(42036), limit=200),
            captured_at_local=_text(exif.get(36867) or exif.get(306), limit=40),
            utc_offset=_text(exif.get(36881), limit=12),
            gps_latitude=latitude,
            gps_longitude=longitude,
            gps_altitude_m=round(altitude, 2) if altitude is not None else None,
            focal_length_mm=round(focal_length, 3) if focal_length is not None else None,
            focal_length_35mm_equivalent_mm=(
                round(focal_35mm, 3) if focal_35mm is not None else None
            ),
            horizontal_field_of_view_degrees=(
                round(horizontal_fov, 2) if horizontal_fov is not None else None
            ),
            aperture_f_number=_number(exif.get(33437)),
            exposure_time_seconds=_number(exif.get(33434)),
            iso=_integer(exif.get(34855) or exif.get(34867)),
            exposure_bias_ev=_number(exif.get(37380)),
            exposure_program=_enum(exif.get(34850), _EXPOSURE_PROGRAMS),
            metering_mode=_enum(exif.get(37383), _METERING_MODES),
            white_balance_mode=white_balance,
            light_source=_enum(exif.get(37384), _LIGHT_SOURCES),
            flash=(f"exif_bitfield_{flash_code}" if flash_code is not None else None),
            color_space=_enum(exif.get(40961), _COLOR_SPACES),
        )
