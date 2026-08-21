from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services.asset_store import AssetStore
from app.services.photography_metadata_service import PhotographyMetadataService


def _camera_jpeg() -> bytes:
    exif = Image.Exif()
    exif[271] = "Example Camera Co."
    exif[272] = "TravelCam X"
    exif[42036] = "24 mm f/1.8"
    exif[36867] = "2026:08:20 18:42:11"
    exif[36881] = "+08:00"
    exif[37386] = 4.25
    exif[41989] = 26
    exif[33437] = 1.8
    exif[33434] = 1 / 125
    exif[34855] = 200
    exif[37380] = -0.3
    exif[34850] = 2
    exif[37383] = 5
    exif[41987] = 0
    exif[37384] = 10
    exif[40961] = 1
    exif[34853] = {
        1: "N",
        2: (31.0, 13.0, 48.0),
        3: "E",
        4: (121.0, 28.0, 12.0),
        5: 0,
        6: 18.4,
    }
    output = io.BytesIO()
    Image.new("RGB", (120, 80), (80, 100, 120)).save(
        output,
        format="JPEG",
        quality=95,
        exif=exif,
    )
    return output.getvalue()


def test_extracts_curated_capture_facts_and_derived_field_of_view(tmp_path) -> None:
    store = AssetStore(tmp_path / "assets", max_image_pixels=10_000_000)
    store.initialize()
    background = store.put_image_bytes(_camera_jpeg())

    metadata = PhotographyMetadataService(asset_store=store).extract_background(background)

    assert metadata.source_asset_id == background.asset_id
    assert metadata.pixel_width == 120
    assert metadata.pixel_height == 80
    assert metadata.camera_make == "Example Camera Co."
    assert metadata.camera_model == "TravelCam X"
    assert metadata.lens_model == "24 mm f/1.8"
    assert metadata.captured_at_local == "2026:08:20 18:42:11"
    assert metadata.utc_offset == "+08:00"
    assert metadata.gps_latitude == pytest.approx(31.23)
    assert metadata.gps_longitude == pytest.approx(121.47)
    assert metadata.gps_altitude_m == pytest.approx(18.4)
    assert metadata.focal_length_mm == pytest.approx(4.25)
    assert metadata.focal_length_35mm_equivalent_mm == pytest.approx(26)
    assert metadata.horizontal_field_of_view_degrees == pytest.approx(69.39)
    assert metadata.aperture_f_number == pytest.approx(1.8)
    assert metadata.exposure_time_seconds == pytest.approx(1 / 125)
    assert metadata.iso == 200
    assert metadata.exposure_bias_ev == pytest.approx(-0.3)
    assert metadata.exposure_program == "normal_program"
    assert metadata.metering_mode == "pattern"
    assert metadata.white_balance_mode == "auto"
    assert metadata.light_source == "cloudy_weather"
    assert metadata.color_space == "sRGB"
    assert metadata.has_capture_data


def test_missing_exif_stays_missing_instead_of_becoming_a_guess(tmp_path) -> None:
    store = AssetStore(tmp_path / "assets", max_image_pixels=10_000_000)
    store.initialize()
    output = io.BytesIO()
    Image.new("RGB", (32, 24), (1, 2, 3)).save(output, format="PNG")
    background = store.put_image_bytes(output.getvalue())

    metadata = PhotographyMetadataService(asset_store=store).extract_background(background)

    assert metadata.pixel_width == 32
    assert metadata.pixel_height == 24
    assert metadata.focal_length_mm is None
    assert metadata.horizontal_field_of_view_degrees is None
    assert metadata.white_balance_mode is None
    assert metadata.gps_latitude is None
    assert not metadata.has_capture_data
