from app.domain.directives import DirectiveCategory, PlannerDirective
from app.domain.searches import PlacementIntent
from app.services.prompt_compiler import (
    compile_canonical_prompt,
    compile_generation_prompt,
)


def test_round_directives_do_not_mutate_canonical_prompt_lineage() -> None:
    placement = PlacementIntent(
        x=0.5,
        y=0.5,
        width=0.2,
        height=0.3,
        pose="sitting",
        facing="left",
        contact_surface="stone pavement",
    )
    canonical_prompt, canonical_hash = compile_canonical_prompt(
        placement=placement,
        user_intent="Place the same cat naturally in the travel photograph.",
        reference_count=2,
    )
    assert "provided guidance mask" in canonical_prompt
    assert "normalized region" not in canonical_prompt
    assert "x=0.5000" not in canonical_prompt
    assert "y=0.5000" not in canonical_prompt
    assert "width=0.2000" not in canonical_prompt
    assert "height=0.3000" not in canonical_prompt
    feedback_prompt, feedback_hash = compile_generation_prompt(
        canonical_prompt=canonical_prompt,
        active_directives=(),
        human_feedback="Make the cat slightly smaller and keep the current eye color.",
    )
    assert "PHOTOGRAPHER FEEDBACK FOR THIS ROUND" in feedback_prompt
    assert "Make the cat slightly smaller" in feedback_prompt
    assert feedback_hash != canonical_hash
    assert feedback_prompt.startswith(canonical_prompt)
    no_directives_prompt, no_directives_hash = compile_generation_prompt(
        canonical_prompt=canonical_prompt,
        active_directives=(),
    )
    directive = PlannerDirective(
        directive_id="physical-directive",
        category=DirectiveCategory.PHYSICAL_INTEGRATION,
        instruction="Add a subtle contact shadow at the lower paws.",
        replaces_category=DirectiveCategory.PHYSICAL_INTEGRATION,
        priority=1,
        expected_effect="Improve ground contact.",
    )
    directed_prompt, directed_hash = compile_generation_prompt(
        canonical_prompt=canonical_prompt,
        active_directives=(directive,),
    )

    assert no_directives_prompt == canonical_prompt
    assert no_directives_hash == canonical_hash
    assert directed_prompt.startswith(canonical_prompt)
    assert directive.instruction in directed_prompt
    assert directed_hash != canonical_hash
    assert compile_canonical_prompt(
        placement=placement,
        user_intent="Place the same cat naturally in the travel photograph.",
        reference_count=2,
    ) == (canonical_prompt, canonical_hash)
