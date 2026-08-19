from __future__ import annotations

import io
import os
from collections.abc import Iterator
from pathlib import Path

# Keep every test under ``backend/tests`` deterministic. The shell wrapper provides
# the same fence, but contributors often run one test from a shell configured for a
# live provider. A future paid smoke suite must live outside this default test tree
# and implement its own explicit opt-in instead of weakening this global boundary.
os.environ["FAKE_GENERATOR"] = "1"
os.environ["PET_FUSION_FAKE_GENERATOR"] = "1"
os.environ["FAKE_CRITIC"] = "1"
os.environ["PET_FUSION_FAKE_CRITIC"] = "1"
os.environ["FAKE_PROMPT_REFINER"] = "1"
os.environ["PET_FUSION_FAKE_PROMPT_REFINER"] = "1"
os.environ["OPENAI_API_KEY"] = ""
os.environ["PET_FUSION_OPENAI_API_KEY"] = ""
os.environ["OPENAI_BASE_URL"] = ""
os.environ["PET_FUSION_OPENAI_BASE_URL"] = ""

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.services.generator_service import DeterministicFakeImageGenerator


def make_image_bytes(
    color: tuple[int, int, int] = (70, 100, 130),
    *,
    size: tuple[int, int] = (96, 64),
    image_format: str = "PNG",
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format)
    return output.getvalue()


@pytest.fixture
def fake_generator() -> DeterministicFakeImageGenerator:
    return DeterministicFakeImageGenerator()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        run_inline=True,
        fake_generator=True,
        max_upload_bytes=2 * 1024 * 1024,
        max_image_pixels=1_000_000,
    )


@pytest.fixture
def client(
    settings: Settings, fake_generator: DeterministicFakeImageGenerator
) -> Iterator[TestClient]:
    app = create_app(settings, image_generator=fake_generator)
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


@pytest.fixture
def project_payload() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("background", ("travel.jpg", make_image_bytes(image_format="JPEG"), "image/jpeg")),
        (
            "cat_references",
            ("cat-front.png", make_image_bytes((190, 120, 60)), "image/png"),
        ),
        (
            "cat_references",
            ("cat-side.webp", make_image_bytes((170, 100, 55), image_format="WEBP"), "image/webp"),
        ),
    ]


@pytest.fixture
def search_payload() -> dict[str, object]:
    return {
        "placement": {
            "x": 0.58,
            "y": 0.48,
            "width": 0.18,
            "height": 0.29,
            "coordinate_space": "normalized",
            "pose": "sitting",
            "facing": "slightly_left",
            "contact_surface": "stone pavement",
        },
        "user_intent": "Place the same cat naturally in the travel photograph.",
        "candidate_count": 3,
        "max_rounds": 1,
        "review_each_round": False,
    }
