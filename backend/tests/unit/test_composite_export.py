from __future__ import annotations

import io

import pytest
from PIL import Image

from app.container import AppContainer
from app.domain.candidates import CandidateRecord
from app.domain.compositing import CropMapping, CropPadding, FloatBox, PixelBox
from app.domain.errors import ConflictError
from app.domain.exports import ExportRequest
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, PlacementIntent, SearchStatus
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import utcnow
from app.services.export_service import ExportService
from app.services.generator_service import GeneratedImage, GenerationRequest
from app.services.image_pipeline import CompositeFloorService, normalized_placement_to_pixel_box


def _png_bytes(image: Image.Image, *, icc_profile: bytes | None = None) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", icc_profile=icc_profile)
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


def _accepted_winner(settings, fake_generator) -> tuple[AppContainer, CandidateRecord]:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    source_image = Image.new("RGB", (16, 12), (20, 30, 40))
    raw_image = Image.new("RGB", (16, 12), (220, 30, 60))
    background = container.asset_store.put_image_bytes(_png_bytes(source_image))
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
    assert first.export_key == service.export_global_winner(
        ExportRequest(search_id="search-export", candidate_id=candidate.candidate_id)
    ).export_key
    with pytest.raises(ConflictError, match="historical global winner"):
        service.export_global_winner(
            ExportRequest(search_id="search-export", candidate_id="candidate-loser")
        )

    mismatched = candidate.model_copy(update={"source_manifest_hash": "f" * 64})
    container.app_store.add_candidate("search-export", mismatched)
    with pytest.raises(ConflictError, match="lineage"):
        service.export_global_winner(command)


def test_export_rejects_searches_that_are_not_accepted(settings, fake_generator) -> None:
    container, _candidate = _accepted_winner(settings, fake_generator)
    container.app_store.update_search(
        "search-export", status=SearchStatus.WAITING_FOR_HUMAN
    )
    with pytest.raises(ConflictError, match="accepted"):
        ExportService(
            app_store=container.app_store, asset_store=container.asset_store
        ).export_global_winner(ExportRequest(search_id="search-export"))


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
