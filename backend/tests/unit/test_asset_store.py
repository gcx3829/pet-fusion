from pathlib import Path

import pytest
from PIL import Image

from app.domain.errors import SourceManifestMismatchError, UploadValidationError
from app.services.asset_store import AssetStore
from tests.conftest import make_image_bytes


def test_asset_store_normalizes_to_content_addressed_png(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets", max_image_pixels=1_000_000)
    store.initialize()
    jpeg = make_image_bytes((10, 20, 30), size=(30, 20), image_format="JPEG")

    first = store.put_image_bytes(jpeg)
    second = store.put_image_bytes(jpeg)

    assert first == second
    assert first.mime_type == "image/png"
    assert first.filesystem_path.suffix == ".png"
    assert first.filesystem_path.is_relative_to(store.root)
    with Image.open(first.path) as image:
        assert image.format == "PNG"
        assert image.size == (30, 20)


def test_asset_store_rejects_non_images_and_detects_tampering(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets", max_image_pixels=1_000_000)
    store.initialize()
    with pytest.raises(UploadValidationError):
        store.put_image_bytes(b"this is not an image")

    asset = store.put_image_bytes(make_image_bytes())
    asset.filesystem_path.write_bytes(b"tampered")
    with pytest.raises(SourceManifestMismatchError):
        store.assert_intact(asset)
