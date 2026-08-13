from __future__ import annotations

import hashlib

from app.domain.searches import PlacementIntent

CANONICAL_TEMPLATE_VERSION = "canonical-prompt/v1"


def compile_canonical_prompt(
    *, placement: PlacementIntent, user_intent: str, reference_count: int
) -> tuple[str, str]:
    contact = placement.contact_surface or "the visible local surface"
    prompt = "\n".join(
        [
            f"TEMPLATE VERSION: {CANONICAL_TEMPLATE_VERSION}",
            "ROLE OF INPUTS",
            "- Image 1 is the immutable original travel photograph and base scene.",
            f"- Images 2..{reference_count + 1} show the same cat.",
            "TASK",
            f"Add that exact cat inside normalized region x={placement.x:.4f}, "
            f"y={placement.y:.4f}, width={placement.width:.4f}, "
            f"height={placement.height:.4f}.",
            f"Pose: {placement.pose}. Facing: {placement.facing}. Contact: {contact}.",
            f"User intent: {user_intent}",
            "Preserve identity, perspective, local light, optics, physical contact, "
            "and all unrelated scene content.",
            "Return an authentic photograph from the same moment and camera system.",
        ]
    )
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()
