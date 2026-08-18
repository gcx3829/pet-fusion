from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.domain.directives import PlannerDirective
from app.domain.searches import PlacementIntent

CANONICAL_TEMPLATE_VERSION = "canonical-prompt/v3"
ROUND_DIRECTIVES_TEMPLATE_VERSION = "round-directives/v1"


def compile_canonical_prompt(
    *,
    placement: PlacementIntent,
    user_intent: str,
    reference_count: int,
) -> tuple[str, str]:
    lines = [
        f"TEMPLATE VERSION: {CANONICAL_TEMPLATE_VERSION}",
        "ROLE OF INPUTS",
        "- Image 1 is the immutable original travel photograph and base scene.",
        f"- Images 2..{reference_count + 1} show the same cat.",
        "TASK",
        "Add that exact cat in the intended placement area indicated by the "
        "provided guidance mask.",
        "Follow the photographer's written direction for pose, facing, contact, "
        "and composition; do not infer a fixed pose from the placement metadata.",
        f"Photographer direction: {user_intent}",
        "Preserve identity, perspective, local light, optics, physical contact, "
        "and all unrelated scene content.",
        "Return an authentic photograph from the same moment and camera system.",
    ]
    prompt = "\n".join(lines)
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def compile_generation_prompt(
    *,
    canonical_prompt: str,
    active_directives: Sequence[PlannerDirective],
    human_feedback: str | None = None,
) -> tuple[str, str]:
    """Compose one round prompt without mutating the canonical user intent.

    The canonical prompt/hash remains stable for the lifetime of a search. The
    returned hash covers the exact provider prompt, while directive lineage is
    independently recorded through ``active_directives_hash``.
    """

    lines = [canonical_prompt]
    if active_directives:
        lines.extend(
            [
                "",
                f"ROUND-SPECIFIC CORRECTIONS ({ROUND_DIRECTIVES_TEMPLATE_VERSION})",
                *(
                    f"{index}. {directive.instruction}"
                    for index, directive in enumerate(active_directives, start=1)
                ),
            ]
        )
    feedback = human_feedback.strip() if human_feedback else ""
    if feedback:
        lines.extend(
            [
                "",
                "PHOTOGRAPHER FEEDBACK FOR THIS ROUND",
                f"- {feedback}",
            ]
        )
    prompt = "\n".join(lines)
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()
