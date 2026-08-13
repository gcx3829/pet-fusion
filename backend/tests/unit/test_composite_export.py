from __future__ import annotations

import concurrent.futures
import io
import sqlite3

import pytest
from PIL import Image

from app.container import AppContainer
from app.domain.candidates import CandidateRecord
from app.domain.compositing import (
    CompositeResult,
    CropMapping,
    CropPadding,
    FloatBox,
    Mask,
    PixelBox,
)
from app.domain.errors import ConflictError, SourceManifestMismatchError
from app.domain.exports import ExportRequest
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, PlacementIntent, SearchStatus
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import AppStore, utcnow
from app.persistence.migrations import MIGRATION_VERSION
from app.services.export_service import ExportService
from app.services.generator_service import GeneratedImage, GenerationRequest
from app.services.image_pipeline import CompositeFloorService, normalized_placement_to_pixel_box


def _png_bytes(
    image: Image.Image,
    *,
    icc_profile: bytes | None = None,
    exif: bytes | None = None,
) -> bytes:
    output = io.BytesIO()
    save_options: dict[str, object] = {"format": "PNG"}
    if icc_profile is not None:
        save_options["icc_profile"] = icc_profile
    if exif is not None:
        save_options["exif"] = exif
    image.save(output, **save_options)  # type: ignore[arg-type]
    return output.getvalue()


def _placement() -> PlacementIntent:
    return PlacementIntent(
        x=0.25,
        y=0.25,
        width=0.5,
        height=0.5,
        pose="sitting",
        facing="left",
    )


def test_composite_floor_keeps_outside_mask_pixels_exact(tmp_path) -> None:
    from app.services.asset_store import AssetStore

    asset_store = AssetStore(tmp_path / "assets", max_image_pixels=1_000_000)
    asset_store.initialize()
    source = Image.new("RGB", (8, 8))
    source.putdata([(x * 17, y * 19, (x + y) * 11) for y in range(8) for x in range(8)])
    generated = Image.new("RGB", (8, 8), (250, 3, 90))
    source_asset = asset_store.put_image_bytes(_png_bytes(source))
    raw_asset = asset_store.put_image_bytes(_png_bytes(generated))
    service = CompositeFloorService(asset_store)

    result = service.protect_candidate(
        source_manifest_hash="a" * 64,
        source_background=source_asset,
        raw_candidate=raw_asset,
        placement=_placement(),
        feather_radius_px=1,
    )

    with Image.open(source_asset.filesystem_path) as expected, Image.open(
        result.protected_asset.filesystem_path
    ) as protected, Image.open(result.mask.asset.filesystem_path) as mask:
        expected_rgb = expected.convert("RGB")
        protected_rgb = protected.convert("RGB")
        alpha = mask.convert("L")
        for y in range(expected.height):
            for x in range(expected.width):
                if alpha.getpixel((x, y)) == 0:
                    assert protected_rgb.getpixel((x, y)) == expected_rgb.getpixel((x, y))
        assert protected_rgb.getpixel((4, 4)) == (250, 3, 90)
    assert result.outside_mask_exact is True
    assert_checkpoint_safe(result.model_dump(mode="json"))


def test_export_schema_migrates_an_existing_application_database(tmp_path) -> None:
    database = tmp_path / "existing.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (5, 'fixture')"
        )
    store = AppStore(database)
    store.initialize()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA table_info(exports)").fetchall()
        }
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
    assert {
        "export_key",
        "search_id",
        "candidate_id",
        "format",
        "jpeg_quality",
        "asset_mime_type",
        "result_json",
    } <= columns.keys()
    assert MIGRATION_VERSION in versions


def test_composite_exactness_detects_rgb_and_alpha_regressions() -> None:
    background = Image.new("RGBA", (3, 3), (10, 20, 30, 255))
    mask = Image.new("L", (3, 3), 0)
    rgb_changed = background.copy()
    rgb_changed.putpixel((0, 0), (99, 20, 30, 255))
    alpha_changed = background.copy()
    alpha_changed.putpixel((0, 0), (10, 20, 30, 1))

    assert not CompositeFloorService.outside_mask_is_exact(
        background=background, protected=rgb_changed, mask=mask
    )
    assert not CompositeFloorService.outside_mask_is_exact(
        background=background, protected=alpha_changed, mask=mask
    )


def test_composite_mask_feather_is_inside_only_and_deterministic() -> None:
    mask = CompositeFloorService._full_resolution_mask(
        width=8,
        height=8,
        allowed_box=PixelBox(x=1, y=1, width=6, height=6),
        feather_radius_px=2,
    )
    assert mask.getpixel((0, 1)) == 0
    assert mask.getpixel((1, 1)) == 64
    assert mask.getpixel((2, 2)) == 191
    assert mask.getpixel((3, 3)) == 255


def test_composite_floor_respects_transparent_and_semtransparent_raw_pixels(tmp_path) -> None:
    from app.services.asset_store import AssetStore

    asset_store = AssetStore(tmp_path / "assets", max_image_pixels=1_000_000)
    asset_store.initialize()
    background = Image.new("RGB", (8, 8), (40, 50, 60))
    raw = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    raw.putpixel((4, 4), (250, 1, 2, 0))
    raw.putpixel((3, 3), (200, 100, 0, 128))
    source_asset = asset_store.put_image_bytes(_png_bytes(background))
    raw_asset = asset_store.put_image_bytes(_png_bytes(raw))

    result = CompositeFloorService(asset_store).protect_candidate(
        source_manifest_hash="a" * 64,
        source_background=source_asset,
        raw_candidate=raw_asset,
        placement=_placement(),
        feather_radius_px=0,
    )
    with Image.open(result.protected_asset.filesystem_path) as protected:
        pixels = protected.convert("RGB")
        assert pixels.getpixel((4, 4)) == (40, 50, 60)
        assert pixels.getpixel((3, 3)) == (120, 75, 30)


def test_crop_mapping_and_normalized_placement_round_trip() -> None:
    placement = _placement()
    assert normalized_placement_to_pixel_box(placement, width=100, height=80) == PixelBox(
        x=25, y=20, width=50, height=40
    )
    mapping = CropMapping(
        full_width=400,
        full_height=300,
        crop_box=PixelBox(x=80, y=60, width=200, height=150),
        canvas_width=220,
        canvas_height=170,
        padding=CropPadding(left=10, top=10, right=10, bottom=10),
    )
    full_box = FloatBox(x=120.5, y=90.25, width=80.0, height=60.0)
    canvas_box = mapping.full_box_to_canvas(full_box)
    restored = mapping.canvas_box_to_full(canvas_box)
    assert restored.x == pytest.approx(full_box.x)
    assert restored.y == pytest.approx(full_box.y)
    assert restored.width == pytest.approx(full_box.width)
    assert restored.height == pytest.approx(full_box.height)


def _accepted_winner(
    settings,
    fake_generator,
    *,
    background_bytes: bytes | None = None,
) -> tuple[AppContainer, CandidateRecord]:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    source_image = Image.new("RGB", (16, 12), (20, 30, 40))
    raw_image = Image.new("RGB", (16, 12), (220, 30, 60))
    background = container.asset_store.put_image_bytes(
        background_bytes or _png_bytes(source_image)
    )
    reference = container.asset_store.put_image_bytes(_png_bytes(Image.new("RGB", (4, 4))))
    raw = container.asset_store.put_image_bytes(_png_bytes(raw_image))
    from app.domain.assets import SourceManifest

    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="project-export", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    command = CreateSearchRequest(placement=_placement(), user_intent="same cat")
    search = container.app_store.create_search(
        search_id="search-export", thread_id="search-export", project=project, request=command
    )
    candidate = CandidateRecord(
        candidate_id="candidate-winner",
        round_index=0,
        variant_index=0,
        raw_asset=raw,
        protected_asset=raw,
        source_manifest_hash=manifest.manifest_hash,
        prompt_hash="a" * 64,
        request_key="b" * 64,
        model="fake-gpt-image-2",
        quality="medium",
        size="16x12",
    )
    container.app_store.add_candidate(search.search_id, candidate)
    loser = candidate.model_copy(
        update={"candidate_id": "candidate-loser", "variant_index": 1}
    )
    container.app_store.add_candidate(search.search_id, loser)
    container.app_store.update_search(
        search.search_id,
        status=SearchStatus.WAITING_FOR_HUMAN,
        global_winner_id=candidate.candidate_id,
        global_winner_score=92.0,
    )
    assert container.app_store.accept_search(search.search_id)
    return container, candidate


class SmallCanvasGenerator:
    def __init__(self) -> None:
        self.call_count = 0

    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]:
        self.call_count += 1
        image = Image.new("RGB", (8, 6), (230, 25, 60))
        return [GeneratedImage(png_bytes=_png_bytes(image), variant_index=0)]


def test_export_is_content_addressed_idempotent_and_rejects_invalid_lineage(
    settings, fake_generator
) -> None:
    container, candidate = _accepted_winner(settings, fake_generator)
    service = ExportService(app_store=container.app_store, asset_store=container.asset_store)
    command = ExportRequest(search_id="search-export")

    first = service.export_global_winner(command)
    second = service.export_global_winner(command)
    assert first.export_key == second.export_key
    assert first.asset == second.asset
    assert first.asset.filesystem_path.is_file()
    assert first.composite.outside_mask_exact is True
    assert first.copied_exif is False
    assert_checkpoint_safe(first.model_dump(mode="json"))
    assert first.export_key == service.export_global_winner(
        ExportRequest(search_id="search-export", candidate_id=candidate.candidate_id)
    ).export_key
    no_metadata_copy = service.export_global_winner(
        ExportRequest(search_id="search-export", copy_icc=False, copy_exif=False)
    )
    assert no_metadata_copy.export_key != first.export_key
    # No source profile exists in this fixture, so distinct requested delivery
    # options are allowed to reference the same content-addressed bytes.
    assert no_metadata_copy.asset == first.asset
    assert container.app_store.get_export(
        search_id="search-export", export_key=no_metadata_copy.export_key
    ) == no_metadata_copy
    with pytest.raises(ConflictError, match="historical global winner"):
        service.export_global_winner(
            ExportRequest(search_id="search-export", candidate_id="candidate-loser")
        )

    mismatched = candidate.model_copy(update={"source_manifest_hash": "f" * 64})
    container.app_store.add_candidate("search-export", mismatched)
    with pytest.raises(ConflictError, match="lineage"):
        service.export_global_winner(command)


def test_concurrent_export_requests_persist_one_replayable_record(
    settings, fake_generator
) -> None:
    container, _candidate = _accepted_winner(settings, fake_generator)
    service = ExportService(app_store=container.app_store, asset_store=container.asset_store)
    command = ExportRequest(search_id="search-export", format="jpeg", jpeg_quality=91)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: service.export_global_winner(command),
                range(16),
            )
        )

    assert len({item.export_key for item in results}) == 1
    assert len({item.asset.sha256 for item in results}) == 1
    with sqlite3.connect(container.app_store.path) as connection:
        assert connection.execute("SELECT count(*) FROM exports").fetchone() == (1,)


def test_export_rejects_a_composite_mask_that_widens_the_placement(
    settings, fake_generator
) -> None:
    container, candidate = _accepted_winner(settings, fake_generator)
    search = container.app_store.get_search("search-export")
    project = container.app_store.get_project(search.project_id)
    widened_mask_asset = container.asset_store.put_image_bytes(
        _png_bytes(Image.new("L", (16, 12), 255))
    )
    allowed_box = normalized_placement_to_pixel_box(
        search.placement,
        width=project.source_manifest.background.width,
        height=project.source_manifest.background.height,
    )
    widened_mask = Mask(
        asset=widened_mask_asset,
        allowed_box=allowed_box,
        feather_radius_px=2,
    )
    polluted = candidate.model_copy(
        update={
            "composite": CompositeResult(
                source_manifest_hash=search.source_manifest_hash,
                source_background=project.source_manifest.background,
                raw_candidate=candidate.raw_asset,
                protected_asset=candidate.protected_asset,
                mask=widened_mask,
                outside_mask_exact=True,
            )
        }
    )
    container.app_store.add_candidate(search.search_id, polluted)

    with pytest.raises(ConflictError, match="mask"):
        container.export_service.export_global_winner(
            ExportRequest(search_id=search.search_id)
        )


def test_export_assets_cannot_reenter_internal_candidate_lineage(
    settings, fake_generator
) -> None:
    container, candidate = _accepted_winner(settings, fake_generator)
    exported = container.export_service.export_global_winner(
        ExportRequest(search_id="search-export")
    )
    assert exported.asset.asset_id.startswith("exp_")
    assert "exports" in exported.asset.filesystem_path.parts
    polluted = candidate.model_copy(
        update={
            "raw_asset": exported.asset,
            "protected_asset": exported.asset,
            "composite": None,
        }
    )
    with sqlite3.connect(container.app_store.path) as connection:
        connection.execute(
            "UPDATE candidates SET record_json = ? WHERE candidate_id = ?",
            (polluted.model_dump_json(), polluted.candidate_id),
        )

    with pytest.raises(SourceManifestMismatchError, match="internal PNG"):
        container.export_service.export_global_winner(
            ExportRequest(search_id="search-export")
        )


def test_export_rejects_searches_that_are_not_accepted(settings, fake_generator) -> None:
    container, _candidate = _accepted_winner(settings, fake_generator)
    container.app_store.update_search(
        "search-export", status=SearchStatus.WAITING_FOR_HUMAN
    )
    with pytest.raises(ConflictError, match="accepted"):
        ExportService(
            app_store=container.app_store, asset_store=container.asset_store
        ).export_global_winner(ExportRequest(search_id="search-export"))


def test_jpeg_export_copies_requested_metadata_and_is_content_addressed(
    settings, fake_generator
) -> None:
    icc_profile = b"fixture-icc-profile"
    exif = Image.Exif()
    exif[270] = "pet-fusion-export-fixture"
    background = _png_bytes(
        Image.new("RGB", (16, 12), (20, 30, 40)),
        icc_profile=icc_profile,
        exif=exif.tobytes(),
    )
    container, candidate = _accepted_winner(
        settings,
        fake_generator,
        background_bytes=background,
    )
    service = ExportService(app_store=container.app_store, asset_store=container.asset_store)

    png_copy = service.export_global_winner(
        ExportRequest(search_id="search-export", format="png")
    )
    assert png_copy.copied_icc is True
    assert png_copy.copied_exif is True
    with Image.open(png_copy.asset.filesystem_path) as image:
        assert image.format == "PNG"
        assert image.info.get("icc_profile") == icc_profile
        assert image.getexif().get(270) == "pet-fusion-export-fixture"

    copied = service.export_global_winner(
        ExportRequest(search_id="search-export", format="jpeg", jpeg_quality=82)
    )
    replay = service.export_global_winner(
        ExportRequest(
            search_id="search-export",
            candidate_id=candidate.candidate_id,
            format="jpeg",
            jpeg_quality=82,
        )
    )
    assert copied == replay
    assert copied.format == "jpeg"
    assert copied.jpeg_quality == 82
    assert copied.asset.mime_type == "image/jpeg"
    assert copied.asset.filesystem_path.suffix == ".jpg"
    assert copied.copied_icc is True
    assert copied.copied_exif is True
    with Image.open(copied.asset.filesystem_path) as image:
        assert image.format == "JPEG"
        assert image.info.get("icc_profile") == icc_profile
        assert image.getexif().get(270) == "pet-fusion-export-fixture"

    without_metadata = service.export_global_winner(
        ExportRequest(
            search_id="search-export",
            format="jpeg",
            jpeg_quality=82,
            copy_icc=False,
            copy_exif=False,
        )
    )
    assert without_metadata.export_key != copied.export_key
    assert without_metadata.asset != copied.asset
    assert without_metadata.copied_icc is False
    assert without_metadata.copied_exif is False
    with Image.open(without_metadata.asset.filesystem_path) as image:
        assert image.info.get("icc_profile") is None
        assert image.getexif().get(270) is None

    different_quality = service.export_global_winner(
        ExportRequest(search_id="search-export", format="jpeg", jpeg_quality=83)
    )
    assert different_quality.export_key != copied.export_key
    assert different_quality.asset.sha256 != copied.asset.sha256


def test_export_bakes_orientation_and_updates_exif_pixel_dimensions(
    settings, fake_generator
) -> None:
    source = Image.new("RGB", (12, 16), (20, 30, 40))
    exif = Image.Exif()
    exif[270] = "oriented-source"
    exif[274] = 6
    exif[40962] = 12
    exif[40963] = 16
    encoded = io.BytesIO()
    source.save(encoded, format="JPEG", quality=100, subsampling=0, exif=exif)
    container, _candidate = _accepted_winner(
        settings,
        fake_generator,
        background_bytes=encoded.getvalue(),
    )

    exported = container.export_service.export_global_winner(
        ExportRequest(search_id="search-export", format="jpeg", jpeg_quality=90)
    )
    with Image.open(exported.asset.filesystem_path) as image:
        delivered_exif = image.getexif()
        assert image.size == (16, 12)
        assert delivered_exif.get(274) is None
        assert delivered_exif.get(40962) == 16
        assert delivered_exif.get(40963) == 12
        assert delivered_exif.get(270) == "oriented-source"


def test_jpeg_export_flattens_transparent_source_pixels_to_a_white_matte(
    settings, fake_generator
) -> None:
    transparent = _png_bytes(Image.new("RGBA", (16, 12), (255, 0, 0, 0)))
    container, _candidate = _accepted_winner(
        settings,
        fake_generator,
        background_bytes=transparent,
    )

    exported = container.export_service.export_global_winner(
        ExportRequest(search_id="search-export", format="jpeg", jpeg_quality=100)
    )
    with Image.open(exported.asset.filesystem_path) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))
    assert red >= 250
    assert green >= 250
    assert blue >= 250


def test_incompatible_source_icc_is_not_attached_to_rgb_delivery(
    settings, fake_generator
) -> None:
    source = Image.new("CMYK", (16, 12), (0, 20, 40, 0))
    encoded = io.BytesIO()
    source.save(
        encoded,
        format="JPEG",
        icc_profile=b"fixture-cmyk-profile",
    )
    container, _candidate = _accepted_winner(
        settings,
        fake_generator,
        background_bytes=encoded.getvalue(),
    )

    exported = container.export_service.export_global_winner(
        ExportRequest(search_id="search-export", format="jpeg", jpeg_quality=90)
    )
    assert exported.copied_icc is False
    with Image.open(exported.asset.filesystem_path) as image:
        assert image.info.get("icc_profile") is None


async def test_non_full_provider_canvas_is_protected_replayed_and_exported(
    settings,
) -> None:
    provider = SmallCanvasGenerator()
    container = AppContainer.build(settings, image_generator=provider)  # type: ignore[arg-type]
    container.initialize()
    icc_profile = b"fixture-icc-profile"
    background = container.asset_store.put_image_bytes(
        _png_bytes(Image.new("RGBA", (16, 12), (12, 24, 36, 128)), icc_profile=icc_profile)
    )
    reference = container.asset_store.put_image_bytes(
        _png_bytes(Image.new("RGB", (4, 4), (80, 90, 100)))
    )
    from app.domain.assets import SourceManifest

    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="project-small-canvas", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    command = CreateSearchRequest(placement=_placement(), user_intent="same cat", candidate_count=1)
    search = container.app_store.create_search(
        search_id="search-small-canvas",
        thread_id="search-small-canvas",
        project=project,
        request=command,
    )
    request = GenerationRequest(
        search_id=search.search_id,
        source_manifest=manifest,
        placement=command.placement,
        prompt="same cat",
        prompt_hash="a" * 64,
        round_index=0,
        candidate_count=1,
        model="small-canvas-fixture",
        quality="medium",
        size="8x6",
    )

    first = await container.generator_service.generate_round(
        request, expected_manifest_hash=manifest.manifest_hash
    )
    second = await container.generator_service.generate_round(
        request, expected_manifest_hash=manifest.manifest_hash
    )
    candidate = first[0]
    assert second == first
    assert provider.call_count == 1
    assert candidate.raw_asset != candidate.protected_asset
    assert candidate.crop_mapping is not None
    assert candidate.composite is not None
    assert candidate.protected_asset.width == background.width
    assert candidate.protected_asset.height == background.height
    assert container.app_store.get_asset(candidate.composite.mask.asset.asset_id) == (
        candidate.composite.mask.asset
    )

    container.app_store.update_search(
        search.search_id,
        status=SearchStatus.WAITING_FOR_HUMAN,
        global_winner_id=candidate.candidate_id,
        global_winner_score=90.0,
    )
    assert container.app_store.accept_search(search.search_id)
    exported = ExportService(
        app_store=container.app_store, asset_store=container.asset_store
    ).export_global_winner(ExportRequest(search_id=search.search_id))
    assert exported.asset.width == background.width
    assert exported.asset.height == background.height
    assert exported.composite.crop_mapping == candidate.crop_mapping
    assert exported.composite.mask == candidate.composite.mask
    assert exported.copied_icc is True
    assert exported.copied_exif is False
    with Image.open(candidate.protected_asset.filesystem_path) as image:
        assert image.mode == "RGBA"
        assert image.info.get("icc_profile") == icc_profile
    with Image.open(exported.asset.filesystem_path) as image:
        assert image.mode == "RGBA"
        assert image.info.get("icc_profile") == icc_profile
