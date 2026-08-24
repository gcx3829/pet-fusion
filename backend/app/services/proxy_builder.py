"""Build bounded, checkpoint-safe image references for Critic invocations."""

from __future__ import annotations

import io
import math
from typing import Literal

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.searches import PlacementIntent
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore

CRITIC_PROXY_SCHEMA_VERSION: Literal["critic-proxy/v1"] = "critic-proxy/v1"
DEFAULT_CRITIC_PROXY_MAX_SIDE = 1536
DEFAULT_CRITIC_REFERENCE_LIMIT = 3


class CriticProxyBundle(BaseModel):
    """Asset-only inputs for a single, independent candidate Critic call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["critic-proxy/v1"] = CRITIC_PROXY_SCHEMA_VERSION
    candidate_id: str = Field(min_length=1, max_length=120)
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    background_proxy: AssetRef
    placement_overlay_proxy: AssetRef
    reference_proxies: tuple[AssetRef, ...] = Field(min_length=1, max_length=3)
    # Raw is the only candidate image used by Search/Critic/user review.  The
    # protected field remains optional so checkpoints written by the previous
    # protected-first implementation can still be read.
    raw_candidate_proxy: AssetRef
    protected_candidate_proxy: AssetRef | None = None
    scene_comparison_proxy: AssetRef | None = None

    @model_validator(mode="before")
    @classmethod
    def hydrate_legacy_proxy(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "raw_candidate_proxy" not in payload and "protected_candidate_proxy" in payload:
            payload["raw_candidate_proxy"] = payload["protected_candidate_proxy"]
        return payload

    @property
    def candidate_proxy(self) -> AssetRef:
        """The image Critic must see; legacy protected proxies are never preferred."""

        return self.raw_candidate_proxy

    @property
    def is_checkpoint_safe(self) -> bool:
        """The bundle contains only references to local PNG assets, never pixels."""

        return True


class CriticProxyBuilder:
    """Create reproducible bounded proxies without changing source or candidates.

    These derivatives are content-addressed assets. Replaying a graph node therefore
    returns the same references and does not mutate immutable source assets or the
    candidate's raw lineage.
    """

    def __init__(
        self,
        *,
        asset_store: AssetStore,
        app_store: AppStore,
        max_side: int = DEFAULT_CRITIC_PROXY_MAX_SIDE,
        reference_limit: int = DEFAULT_CRITIC_REFERENCE_LIMIT,
    ) -> None:
        if max_side <= 0:
            raise ValueError("Critic proxy max_side must be positive")
        if not 1 <= reference_limit <= 3:
            raise ValueError("Critic proxy reference_limit must be between 1 and 3")
        self.asset_store = asset_store
        self.app_store = app_store
        self.max_side = max_side
        self.reference_limit = reference_limit

    @staticmethod
    def _png_bytes(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="PNG", compress_level=9, optimize=False)
        return output.getvalue()

    def _store_proxy(self, image: Image.Image) -> AssetRef:
        asset = self.asset_store.put_image_bytes(self._png_bytes(image))
        self.app_store.register_asset(asset)
        return asset

    def _proxy_asset(self, asset: AssetRef) -> AssetRef:
        self.asset_store.assert_intact(asset)
        with Image.open(asset.filesystem_path) as opened:
            mode: Literal["RGB", "RGBA"] = "RGBA" if "A" in opened.getbands() else "RGB"
            image = opened.convert(mode)
        longest_side = max(image.size)
        if longest_side > self.max_side:
            scale = self.max_side / longest_side
            target_size = (
                max(1, math.floor(image.width * scale)),
                max(1, math.floor(image.height * scale)),
            )
            image = image.resize(target_size, Image.Resampling.LANCZOS)
        return self._store_proxy(image)

    def _placement_overlay(
        self, background_proxy: AssetRef, placement: PlacementIntent
    ) -> AssetRef:
        self.asset_store.assert_intact(background_proxy)
        with Image.open(background_proxy.filesystem_path) as opened:
            image = opened.convert("RGBA")
        left = max(0, min(image.width - 1, math.floor(placement.x * image.width)))
        top = max(0, min(image.height - 1, math.floor(placement.y * image.height)))
        right = max(
            left,
            min(image.width - 1, math.ceil((placement.x + placement.width) * image.width) - 1),
        )
        bottom = max(
            top,
            min(
                image.height - 1,
                math.ceil((placement.y + placement.height) * image.height) - 1,
            ),
        )
        line_width = max(1, round(min(image.size) / 256))
        draw = ImageDraw.Draw(image)
        draw.rectangle((left, top, right, bottom), outline=(255, 196, 72, 255), width=line_width)
        return self._store_proxy(image.convert("RGB"))

    def _scene_comparison(
        self, background_proxy: AssetRef, candidate_proxy: AssetRef
    ) -> AssetRef:
        """Place source and raw candidate on one bounded, equal-size review sheet."""

        self.asset_store.assert_intact(background_proxy)
        self.asset_store.assert_intact(candidate_proxy)
        with Image.open(background_proxy.filesystem_path) as opened:
            background = ImageOps.exif_transpose(opened).convert("RGB")
        with Image.open(candidate_proxy.filesystem_path) as opened:
            candidate = ImageOps.exif_transpose(opened).convert("RGB")

        gap = max(1, round(self.max_side / 384))
        panel_width = max(1, (self.max_side - gap) // 2)
        panel_height = min(
            self.max_side,
            max(
                1,
                round(
                    max(
                        background.height / background.width,
                        candidate.height / candidate.width,
                    )
                    * panel_width
                ),
            ),
        )
        background = ImageOps.contain(
            background, (panel_width, panel_height), Image.Resampling.LANCZOS
        )
        candidate = ImageOps.contain(
            candidate, (panel_width, panel_height), Image.Resampling.LANCZOS
        )
        sheet = Image.new(
            "RGB", (panel_width * 2 + gap, panel_height), color=(14, 16, 19)
        )
        sheet.paste(
            background,
            ((panel_width - background.width) // 2, (panel_height - background.height) // 2),
        )
        sheet.paste(
            candidate,
            (
                panel_width + gap + (panel_width - candidate.width) // 2,
                (panel_height - candidate.height) // 2,
            ),
        )
        return self._store_proxy(sheet)

    def build(
        self,
        *,
        source_manifest: SourceManifest,
        candidate: CandidateRecord,
        placement: PlacementIntent,
    ) -> CriticProxyBundle:
        """Create the fixed-proxy view for the raw candidate under review."""

        source_manifest.assert_integrity()
        if candidate.source_manifest_hash != source_manifest.manifest_hash:
            raise ValueError("Candidate source manifest does not match Critic source manifest")
        background_proxy = self._proxy_asset(source_manifest.background)
        references = tuple(
            self._proxy_asset(reference)
            for reference in source_manifest.cat_references[: self.reference_limit]
        )
        candidate_proxy = self._proxy_asset(candidate.raw_authoritative_asset)
        overlay_proxy = self._placement_overlay(background_proxy, placement)
        comparison_proxy = self._scene_comparison(background_proxy, candidate_proxy)
        return CriticProxyBundle(
            candidate_id=candidate.candidate_id,
            source_manifest_hash=source_manifest.manifest_hash,
            background_proxy=background_proxy,
            placement_overlay_proxy=overlay_proxy,
            reference_proxies=references,
            raw_candidate_proxy=candidate_proxy,
            # Byte-identical legacy alias for checkpoint/replay consumers.  New
            # providers use ``candidate_proxy`` so protected data cannot become
            # the review authority by accident.
            protected_candidate_proxy=candidate_proxy,
            scene_comparison_proxy=comparison_proxy,
        )
