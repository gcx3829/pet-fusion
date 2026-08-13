"""Deterministic reducers used by parallel LangGraph branches."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict


class CriticEvaluationBucket(TypedDict):
    """One round-scoped set of keyed Critic evaluations."""

    round_index: int
    items: dict[str, dict[str, object]]


def empty_evaluation_bucket(round_index: int) -> CriticEvaluationBucket:
    if isinstance(round_index, bool) or round_index < 0:
        raise ValueError("Critic evaluation round_index must be a non-negative integer")
    return {"round_index": round_index, "items": {}}


def _validated_bucket(value: CriticEvaluationBucket) -> CriticEvaluationBucket:
    round_index = value.get("round_index")
    items = value.get("items")
    if isinstance(round_index, bool) or not isinstance(round_index, int) or round_index < 0:
        raise ValueError("Critic evaluation bucket requires a non-negative round_index")
    if not isinstance(items, Mapping):
        raise TypeError("Critic evaluation bucket requires an items object")
    normalized: dict[str, dict[str, object]] = {}
    for candidate_id, payload in items.items():
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("Critic evaluation reducer requires a non-empty candidate ID")
        if not isinstance(payload, Mapping):
            raise TypeError("Critic evaluation reducer requires object payloads")
        payload_dict = dict(payload)
        if payload_dict.get("candidate_id") != candidate_id:
            raise ValueError("Critic evaluation payload candidate_id does not match reducer key")
        if payload_dict.get("round_index") != round_index:
            raise ValueError("Critic evaluation payload does not match reducer round")
        normalized[candidate_id] = payload_dict
    return {"round_index": round_index, "items": normalized}


def merge_evaluations_by_candidate(
    left: CriticEvaluationBucket,
    right: CriticEvaluationBucket,
) -> CriticEvaluationBucket:
    """Merge same-round fan-out and replace the bucket on a newer round.

    A checkpoint replay can re-run a fan-out branch, so candidate IDs overwrite
    idempotently. A later round must not inherit the prior round's branch results;
    its empty bucket therefore replaces the complete older bucket before fan-out.
    Delayed writes from an older round are ignored.
    """

    if not left:
        return _validated_bucket(right)
    if not right:
        return _validated_bucket(left)
    normalized_left = _validated_bucket(left)
    normalized_right = _validated_bucket(right)
    if normalized_right["round_index"] > normalized_left["round_index"]:
        return normalized_right
    if normalized_right["round_index"] < normalized_left["round_index"]:
        return normalized_left
    return {
        "round_index": normalized_left["round_index"],
        "items": {**normalized_left["items"], **normalized_right["items"]},
    }
