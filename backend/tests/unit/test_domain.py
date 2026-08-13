import pytest
from pydantic import ValidationError

from app.domain.assets import SourceManifest
from app.domain.searches import PlacementIntent
from app.services.asset_store import AssetStore
from tests.conftest import make_image_bytes


def test_source_manifest_hash_covers_ordered_immutable_sources(tmp_path) -> None:
    store = AssetStore(tmp_path / "assets", max_image_pixels=1_000_000)
    store.initialize()
    background = store.put_image_bytes(make_image_bytes((1, 2, 3)))
    first = store.put_image_bytes(make_image_bytes((4, 5, 6)))
    second = store.put_image_bytes(make_image_bytes((7, 8, 9)))

    manifest = SourceManifest.create(background=background, cat_references=[first, second])
    reordered = SourceManifest.create(background=background, cat_references=[second, first])

    manifest.assert_integrity()
    assert manifest.manifest_hash != reordered.manifest_hash


def test_placement_must_stay_inside_normalized_coordinate_space() -> None:
    with pytest.raises(ValidationError):
        PlacementIntent(
            x=0.9,
            y=0.5,
            width=0.2,
            height=0.2,
            pose="sitting",
            facing="left",
        )
