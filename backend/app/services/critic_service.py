"""Deterministic Critic contract used until a vision provider is enabled."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.evaluations import (
    CandidateEvaluation,
    CriticIssue,
    DimensionScores,
    Severity,
)
from app.domain.searches import PlacementIntent

RUBRIC_VERSION = "critic-rubric/v1-fake"


@dataclass(frozen=True, slots=True)
class CriticInput:
    """References and structured intent only; image bytes never enter graph state."""

    candidate: CandidateRecord
    source_manifest: SourceManifest
    placement: PlacementIntent


class DeterministicCriticService:
    """A reproducible local fixture for Critic-shaped outputs.

    The service deliberately uses the candidate asset dimensions and a hash-derived
    fixture bias. It is useful for exercising reducers and policies without making
    claims about the quality of a future vision model.
    """

    rubric_version = RUBRIC_VERSION

    @staticmethod
    def _read_dimensions(path: Path) -> tuple[int, int] | None:
        try:
            with Image.open(path) as image:
                return image.size
        except (OSError, UnidentifiedImageError):
            return None

    def evaluate(self, request: CriticInput) -> CandidateEvaluation:
        candidate = request.candidate
        background = request.source_manifest.background
        candidate_size = self._read_dimensions(candidate.protected_asset.filesystem_path)
        expected_size = (background.width, background.height)
        digest = hashlib.sha256(
            f"{candidate.candidate_id}:{candidate.request_key}:{RUBRIC_VERSION}".encode()
        ).digest()
        fixture_bias = (digest[0] % 11) - 5

        scene_score = 96.0 if candidate_size == expected_size else 20.0
        identity_score = float(max(0, min(100, 92 + fixture_bias)))
        pose_score = float(max(0, min(100, 90 + (digest[1] % 9) - 4)))
        perspective_score = float(max(0, min(100, 91 + (digest[2] % 7) - 3)))
        lighting_score = float(max(0, min(100, 89 + (digest[3] % 9) - 4)))
        optical_score = float(max(0, min(100, 90 + (digest[4] % 7) - 3)))
        physical_score = float(max(0, min(100, 88 + (digest[5] % 9) - 4)))
        naturalness_score = float(max(0, min(100, 90 + (digest[6] % 7) - 3)))

        issues: list[CriticIssue] = []
        hard_failures: list[str] = []
        if candidate_size is None:
            issues.append(
                CriticIssue(
                    issue_id="asset_unreadable",
                    category="asset_integrity",
                    severity=Severity.BLOCKING,
                    evidence="The protected candidate asset cannot be decoded locally.",
                    suggested_fix="Regenerate the candidate as a valid PNG asset.",
                    confidence=1.0,
                )
            )
            hard_failures.append("asset_unreadable")
        elif candidate_size != expected_size:
            issues.append(
                CriticIssue(
                    issue_id="scene_dimensions_mismatch",
                    category="scene_preservation",
                    severity=Severity.BLOCKING,
                    evidence=(
                        f"Candidate dimensions {candidate_size} differ from source {expected_size}."
                    ),
                    suggested_fix="Regenerate at the immutable source background dimensions.",
                    confidence=1.0,
                )
            )
            hard_failures.append("scene_dimensions_mismatch")

        identity_match = identity_score >= 75 and not hard_failures
        if not identity_match:
            hard_failures.append("identity_mismatch")

        scores = DimensionScores(
            cat_identity=identity_score,
            pose_geometry=pose_score,
            perspective_scale=perspective_score,
            lighting_color=lighting_score,
            optical_consistency=optical_score,
            physical_integration=physical_score,
            scene_preservation=scene_score,
            overall_photographic_naturalness=naturalness_score,
        )
        blocking = any(issue.severity is Severity.BLOCKING for issue in issues)
        return CandidateEvaluation(
            rubric_version=RUBRIC_VERSION,
            candidate_id=candidate.candidate_id,
            round_index=candidate.round_index,
            source_manifest_hash=request.source_manifest.manifest_hash,
            scores=scores,
            issues=tuple(issues),
            no_meaningful_defect=not blocking,
            identity_match=identity_match,
            prompt_adherent=True,
            recommended_action="regenerate" if blocking else "review",
            summary=(
                "Local fixture found no blocking defect."
                if not blocking
                else "Local fixture found a blocking asset or scene defect."
            ),
            hard_constraint_failures=tuple(dict.fromkeys(hard_failures)),
        )
