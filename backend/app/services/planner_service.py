"""Safe, deterministic feedback-planning service and provider boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from app.domain.directives import (
    ActionableBlockingIssue,
    DirectiveCategory,
    DirectivePolicy,
    PlannerAction,
    PlannerDirective,
    PlannerInput,
    PlannerProposal,
    PlannerResult,
    stable_directive_id,
    stable_directives_hash,
)
from app.domain.evaluations import CandidateEvaluation, Severity

PLANNER_CONTRACT_VERSION = "feedback-planner/v1-fake"

_CATEGORY_ALIASES: dict[str, DirectiveCategory] = {
    "identity": DirectiveCategory.IDENTITY,
    "cat_identity": DirectiveCategory.IDENTITY,
    "coat_pattern": DirectiveCategory.IDENTITY,
    "fur_identity": DirectiveCategory.IDENTITY,
    "pose": DirectiveCategory.POSE_GEOMETRY,
    "pose_geometry": DirectiveCategory.POSE_GEOMETRY,
    "anatomy": DirectiveCategory.POSE_GEOMETRY,
    "perspective": DirectiveCategory.PERSPECTIVE_SCALE,
    "perspective_scale": DirectiveCategory.PERSPECTIVE_SCALE,
    "scale": DirectiveCategory.PERSPECTIVE_SCALE,
    "lighting": DirectiveCategory.LIGHTING_COLOR,
    "lighting_color": DirectiveCategory.LIGHTING_COLOR,
    "color": DirectiveCategory.LIGHTING_COLOR,
    "optics": DirectiveCategory.OPTICAL_CONSISTENCY,
    "optical_consistency": DirectiveCategory.OPTICAL_CONSISTENCY,
    "sharpness": DirectiveCategory.OPTICAL_CONSISTENCY,
    "physical_integration": DirectiveCategory.PHYSICAL_INTEGRATION,
    "integration": DirectiveCategory.PHYSICAL_INTEGRATION,
    "contact_shadow": DirectiveCategory.PHYSICAL_INTEGRATION,
    "scene_preservation": DirectiveCategory.SCENE_PRESERVATION,
    "background": DirectiveCategory.SCENE_PRESERVATION,
    "asset_integrity": DirectiveCategory.ASSET_INTEGRITY,
}

_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "developer message",
    "assistant message",
    "jailbreak",
    "reveal instructions",
    "follow these instructions instead",
    "<script",
    "```",
    "role:",
)
_BROAD_DIRECTIVE_MARKERS = (
    "make everything better",
    "more cinematic",
    "improve everything",
    "improve the face, lighting, background",
)
_MULTI_WHITESPACE = re.compile(r"\s+")


@runtime_checkable
class PlannerProvider(Protocol):
    """Provider port. Future OpenAI implementations must return this typed result."""

    def plan(self, planner_input: PlannerInput) -> PlannerProposal:
        """Return a proposal only; policy validation remains local."""


def normalize_category(value: str) -> DirectiveCategory | None:
    normalized = _MULTI_WHITESPACE.sub("_", value.strip().casefold().replace("-", " "))
    return _CATEGORY_ALIASES.get(normalized)


def _normalize_short_text(value: str | None, *, max_length: int) -> str | None:
    if value is None or len(value) > max_length:
        return None
    normalized = _MULTI_WHITESPACE.sub(" ", value.strip())
    return normalized or None


def _safe_instruction(value: str, *, max_length: int) -> str | None:
    if len(value) > max_length or "\n" in value or "\r" in value:
        return None
    normalized = _normalize_short_text(value, max_length=max_length)
    if normalized is None:
        return None
    lowered = normalized.casefold()
    if any(marker in lowered for marker in _INJECTION_MARKERS):
        return None
    if any(marker in lowered for marker in _BROAD_DIRECTIVE_MARKERS):
        return None
    broad_topics = (
        "face",
        "lighting",
        "background",
        "color",
        "composition",
        "perspective",
        "fur",
        "shadow",
    )
    if sum(topic in lowered for topic in broad_topics) >= 4:
        return None
    return normalized


def select_actionable_blocking_issues(
    *,
    selected_candidate_id: str | None,
    evaluations: Iterable[CandidateEvaluation],
) -> tuple[ActionableBlockingIssue, ...]:
    """Project *only* the selected winner's trustworthy blocking findings.

    A losing candidate is never allowed to bias the next prompt. Unknown categories
    are intentionally excluded because an offline policy cannot safely formulate a
    targeted correction for them.
    """

    if selected_candidate_id is None:
        return ()
    selected = next(
        (item for item in evaluations if item.candidate_id == selected_candidate_id),
        None,
    )
    if selected is None:
        return ()
    issues: list[ActionableBlockingIssue] = []
    for issue in selected.issues:
        if issue.severity is not Severity.BLOCKING or issue.confidence < 0.75:
            continue
        category = normalize_category(issue.category)
        if category is None:
            continue
        suggested_fix = _normalize_short_text(issue.suggested_fix, max_length=240)
        issues.append(
            ActionableBlockingIssue(
                issue_id=issue.issue_id,
                category=category,
                region=_normalize_short_text(issue.region, max_length=120),
                suggested_fix=suggested_fix,
                confidence=issue.confidence,
            )
        )
    return tuple(
        sorted(
            issues,
            key=lambda item: (-item.confidence, item.category.value, item.issue_id),
        )
    )


def normalize_active_directives(
    values: Iterable[PlannerDirective | Mapping[str, object]],
    *,
    policy: DirectivePolicy | None = None,
) -> tuple[PlannerDirective, ...]:
    """Parse current structured directives; ignore legacy/untrusted entries safely."""

    resolved_policy = policy or DirectivePolicy()
    normalized: list[PlannerDirective] = []
    for value in values:
        try:
            if isinstance(value, PlannerDirective):
                directive = value
            else:
                directive = PlannerDirective.model_validate(value)
        except (TypeError, ValueError):
            continue
        instruction = _safe_instruction(
            directive.instruction,
            max_length=resolved_policy.max_instruction_chars,
        )
        expected_effect = _normalize_short_text(
            directive.expected_effect,
            max_length=resolved_policy.max_expected_effect_chars,
        )
        if (
            instruction is None
            or expected_effect is None
            or directive.replaces_category not in {None, directive.category}
        ):
            continue
        normalized.append(
            directive.model_copy(
                update={"instruction": instruction, "expected_effect": expected_effect}
            )
        )
    unique: list[PlannerDirective] = []
    seen_categories: set[DirectiveCategory] = set()
    for directive in sorted(
        normalized,
        key=lambda item: (item.priority, item.category.value, item.directive_id),
    ):
        if directive.category in seen_categories:
            continue
        unique.append(directive)
        seen_categories.add(directive.category)
        if len(unique) == resolved_policy.max_directives:
            break
    return tuple(unique)


class DeterministicFakePlannerProvider:
    """Offline provider used by default until a structured OpenAI planner is enabled."""

    provider_version = PLANNER_CONTRACT_VERSION

    def plan(self, planner_input: PlannerInput) -> PlannerProposal:
        issue = next(iter(planner_input.blocking_issues), None)
        if issue is None:
            return _review_proposal(
                reason="no_actionable_blocking_issue",
                summary="No selected, normalized blocking issue is available for a safe directive.",
            )
        instruction = _safe_instruction(
            issue.suggested_fix or "",
            max_length=planner_input.directive_policy.max_instruction_chars,
        )
        if instruction is None:
            return _review_proposal(
                reason="no_safe_suggested_fix",
                summary="The selected blocking issue has no safe, bounded suggested fix.",
            )
        directive = PlannerDirective(
            directive_id=stable_directive_id(
                category=issue.category,
                instruction=instruction,
                policy_version=planner_input.directive_policy.policy_version,
            ),
            category=issue.category,
            instruction=instruction,
            replaces_category=issue.category,
            priority=1,
            expected_effect=(
                f"Address the selected {issue.category.value.replace('_', ' ')} issue."
            ),
        )
        return PlannerProposal(
            action=PlannerAction.CONTINUE,
            directives=(directive,),
            stop_reason=None,
            plan_summary=f"Create one focused directive for {issue.category.value}.",
        )


def _review_proposal(
    *, reason: str, summary: str, fallback_used: bool = False
) -> PlannerProposal:
    return PlannerProposal(
        action=PlannerAction.HUMAN_REVIEW,
        directives=(),
        stop_reason=reason,
        plan_summary=summary,
        fallback_used=fallback_used,
    )


def _review_result(
    planner_input: PlannerInput,
    *,
    reason: str,
    summary: str,
    directives: tuple[PlannerDirective, ...] | None = None,
    fallback_used: bool = False,
) -> PlannerResult:
    return PlannerResult(
        action=PlannerAction.HUMAN_REVIEW,
        directives=planner_input.active_directives if directives is None else directives,
        stop_reason=reason,
        plan_summary=summary,
        directive_policy_version=planner_input.directive_policy.policy_version,
        directive_version=planner_input.directive_version,
        active_directives_hash=stable_directives_hash(
            planner_input.active_directives if directives is None else directives
        ),
        fallback_used=fallback_used,
    )


class FeedbackPlannerService:
    """Local policy layer around an injectable provider result."""

    def __init__(
        self,
        provider: PlannerProvider | None = None,
        policy: DirectivePolicy | None = None,
    ) -> None:
        self.provider = provider or DeterministicFakePlannerProvider()
        self.policy = policy or DirectivePolicy()

    def request_proposal(self, planner_input: PlannerInput) -> PlannerProposal:
        """Call the provider or take exactly one deterministic fallback attempt."""

        if not planner_input.blocking_issues:
            return _review_proposal(
                reason="no_actionable_blocking_issue",
                summary="Only selected blocking issues can authorize an automatic next round.",
            )
        try:
            proposal = self.provider.plan(planner_input)
            return proposal.model_copy(update={"fallback_used": False})
        except Exception:
            if planner_input.fallback_attempts >= 1:
                return _review_proposal(
                    reason="planner_fallback_exhausted",
                    summary="The planner was unavailable after its single deterministic fallback.",
                )
            return self._fallback(planner_input)

    def validate_directive_budget(
        self, planner_input: PlannerInput, proposal: PlannerProposal
    ) -> PlannerResult:
        """Reject untrusted broad, duplicate, or oversized prompt material locally."""

        if proposal.action is not PlannerAction.CONTINUE:
            return _review_result(
                planner_input,
                reason=proposal.stop_reason or "planner_stopped",
                summary=proposal.plan_summary,
                fallback_used=proposal.fallback_used,
            ).model_copy(update={"action": proposal.action})
        if not proposal.directives:
            return _review_result(
                planner_input,
                reason="empty_directive_plan",
                summary="The planner requested continuation without a directive.",
                fallback_used=proposal.fallback_used,
            )
        if len(proposal.directives) > planner_input.directive_policy.max_directives:
            return _review_result(
                planner_input,
                reason="directive_budget_exceeded",
                summary="The planner exceeded the bounded directive budget.",
                fallback_used=proposal.fallback_used,
            )

        attempted = tuple(item.value for item in planner_input.attempted_categories)
        actionable_categories = {issue.category for issue in planner_input.blocking_issues}
        active_pairs = {
            (item.category, item.instruction.casefold()) for item in planner_input.active_directives
        }
        seen_categories: set[DirectiveCategory] = set()
        validated: list[PlannerDirective] = []
        for directive in proposal.directives:
            if directive.category not in actionable_categories:
                return _review_result(
                    planner_input,
                    reason="unrelated_directive_category",
                    summary="A planner directive did not target a selected blocking issue.",
                    fallback_used=proposal.fallback_used,
                )
            if directive.replaces_category not in {None, directive.category}:
                return _review_result(
                    planner_input,
                    reason="invalid_directive_replacement",
                    summary="A directive may replace only its own correction category.",
                    fallback_used=proposal.fallback_used,
                )
            instruction = _safe_instruction(
                directive.instruction,
                max_length=planner_input.directive_policy.max_instruction_chars,
            )
            if instruction is None:
                return _review_result(
                    planner_input,
                    reason="unsafe_directive",
                    summary="A planner directive failed local prompt-safety validation.",
                    fallback_used=proposal.fallback_used,
                )
            if directive.category in seen_categories or (
                directive.category,
                instruction.casefold(),
            ) in active_pairs:
                return _review_result(
                    planner_input,
                    reason="repeated_directive",
                    summary="The planner repeated an active correction instead of replacing it.",
                    fallback_used=proposal.fallback_used,
                )
            if attempted.count(directive.category.value) >= 2:
                return _review_result(
                    planner_input,
                    reason="repeated_category_without_improvement",
                    summary="The same correction category has already been attempted twice.",
                    fallback_used=proposal.fallback_used,
                )
            expected_effect = _normalize_short_text(
                directive.expected_effect,
                max_length=planner_input.directive_policy.max_expected_effect_chars,
            )
            if expected_effect is None:
                return _review_result(
                    planner_input,
                    reason="unsafe_expected_effect",
                    summary="A planner expected-effect summary exceeded its local bound.",
                    fallback_used=proposal.fallback_used,
                )
            seen_categories.add(directive.category)
            validated.append(
                directive.model_copy(
                    update={
                        "instruction": instruction,
                        "replaces_category": directive.category,
                        "expected_effect": expected_effect,
                        "directive_id": stable_directive_id(
                            category=directive.category,
                            instruction=instruction,
                            policy_version=planner_input.directive_policy.policy_version,
                        ),
                    }
                )
            )
        validated_directives = tuple(validated)
        return PlannerResult(
            action=proposal.action,
            directives=validated_directives,
            stop_reason=proposal.stop_reason,
            plan_summary=proposal.plan_summary,
            directive_policy_version=planner_input.directive_policy.policy_version,
            directive_version=planner_input.directive_version,
            active_directives_hash=stable_directives_hash(validated_directives),
            fallback_used=proposal.fallback_used,
        )

    def replace_or_retain_directives(
        self, planner_input: PlannerInput, validated: PlannerResult
    ) -> PlannerResult:
        """Apply category replacement instead of appending critique history forever."""

        if validated.action is not PlannerAction.CONTINUE:
            return validated
        replaced_categories = {
            directive.replaces_category or directive.category for directive in validated.directives
        }
        actionable_categories = {issue.category for issue in planner_input.blocking_issues}
        retained = tuple(
            directive
            for directive in planner_input.active_directives
            if directive.category in actionable_categories
            and directive.category not in replaced_categories
        )
        active = tuple(
            sorted(
                (*retained, *validated.directives),
                key=lambda item: (item.priority, item.category.value, item.directive_id),
            )
        )
        if len(active) > planner_input.directive_policy.max_directives:
            return _review_result(
                planner_input,
                reason="directive_budget_exceeded",
                summary="Directive replacement would exceed the configured active budget.",
                fallback_used=validated.fallback_used,
            )
        changed = active != planner_input.active_directives
        if not changed:
            return _review_result(
                planner_input,
                reason="repeated_directive",
                summary="Directive replacement did not produce a new active plan.",
                fallback_used=validated.fallback_used,
            )
        return validated.model_copy(
            update={
                "directives": active,
                "directive_version": planner_input.directive_version + 1,
                "active_directives_hash": stable_directives_hash(active),
                "directive_policy_version": planner_input.directive_policy.policy_version,
            }
        )

    def plan(self, planner_input: PlannerInput) -> PlannerResult:
        """Run provider proposal, local validation, and replacement as one service call."""

        proposal = self.request_proposal(planner_input)
        validated = self.validate_directive_budget(planner_input, proposal)
        return self.replace_or_retain_directives(planner_input, validated)

    def _fallback(self, planner_input: PlannerInput) -> PlannerProposal:
        issue = next(iter(planner_input.blocking_issues), None)
        instruction = (
            _safe_instruction(
                issue.suggested_fix,
                max_length=planner_input.directive_policy.max_instruction_chars,
            )
            if issue is not None and issue.suggested_fix is not None
            else None
        )
        if issue is None or instruction is None:
            return _review_proposal(
                reason="no_safe_suggested_fix",
                summary="Fallback cannot derive a safe short directive from the selected issue.",
                fallback_used=True,
            )
        directive = PlannerDirective(
            directive_id=stable_directive_id(
                category=issue.category,
                instruction=instruction,
                policy_version=planner_input.directive_policy.policy_version,
            ),
            category=issue.category,
            instruction=instruction,
            replaces_category=issue.category,
            priority=1,
            expected_effect=(
                f"Address the selected {issue.category.value.replace('_', ' ')} issue."
            ),
        )
        return PlannerProposal(
            action=PlannerAction.CONTINUE,
            directives=(directive,),
            stop_reason=None,
            plan_summary=(
                "The provider was unavailable; use one deterministic selected-issue fallback."
            ),
            fallback_used=True,
        )
