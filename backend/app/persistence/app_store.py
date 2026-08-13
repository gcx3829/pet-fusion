from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.assets import AssetRef
from app.domain.candidates import CandidateRecord
from app.domain.errors import ConflictError, NotFoundError
from app.domain.evaluations import CandidateEvaluation
from app.domain.projects import ProjectRecord
from app.domain.searches import (
    CreateSearchRequest,
    SearchEvent,
    SearchRunRecord,
    SearchStatus,
)
from app.persistence.migrations import MIGRATION_VERSION, SCHEMA_SQL


def utcnow() -> datetime:
    return datetime.now(UTC)


class AppStore:
    """Small repository layer over the app database; checkpoints live elsewhere."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._migration_lock = threading.Lock()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._migration_lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA_SQL)
            provider_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(provider_calls)")
            }
            if "lease_owner" not in provider_columns:
                connection.execute("ALTER TABLE provider_calls ADD COLUMN lease_owner TEXT")
            if "lease_until" not in provider_columns:
                connection.execute("ALTER TABLE provider_calls ADD COLUMN lease_until TEXT")
            search_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(search_runs)")
            }
            if "idempotency_key" not in search_columns:
                connection.execute("ALTER TABLE search_runs ADD COLUMN idempotency_key TEXT")
            if "idempotency_fingerprint" not in search_columns:
                connection.execute(
                    "ALTER TABLE search_runs ADD COLUMN idempotency_fingerprint TEXT"
                )
            for column, definition in (
                ("round_winner_id", "TEXT"),
                ("global_winner_score", "REAL"),
                ("round_history_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("interrupt_payload_json", "TEXT"),
            ):
                if column not in search_columns:
                    connection.execute(
                        f"ALTER TABLE search_runs ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_runs_idempotency "
                "ON search_runs(project_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (MIGRATION_VERSION, utcnow().isoformat()),
            )
            connection.commit()

    def register_asset(self, asset: AssetRef) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO assets(asset_id, sha256, path, mime_type, width, height, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    path = excluded.path,
                    mime_type = excluded.mime_type,
                    width = excluded.width,
                    height = excluded.height
                """,
                (
                    asset.asset_id,
                    asset.sha256,
                    asset.path,
                    asset.mime_type,
                    asset.width,
                    asset.height,
                    utcnow().isoformat(),
                ),
            )
            connection.commit()

    def get_asset(self, asset_id: str) -> AssetRef:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Asset {asset_id} was not found")
        return AssetRef(
            asset_id=row["asset_id"],
            sha256=row["sha256"],
            path=row["path"],
            mime_type=row["mime_type"],
            width=row["width"],
            height=row["height"],
        )

    def create_project(self, project: ProjectRecord) -> None:
        manifest = project.source_manifest
        manifest_json = manifest.model_dump_json()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for asset in (manifest.background, *manifest.cat_references):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO assets(
                        asset_id, sha256, path, mime_type, width, height, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset.asset_id,
                        asset.sha256,
                        asset.path,
                        asset.mime_type,
                        asset.width,
                        asset.height,
                        project.created_at.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, cat_name, cat_traits, source_manifest_json,
                    manifest_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project.project_id,
                    project.cat_name,
                    project.cat_traits,
                    manifest_json,
                    manifest.manifest_hash,
                    project.created_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO project_assets(project_id, asset_id, role, sort_index) "
                "VALUES (?, ?, 'background', 0)",
                (project.project_id, manifest.background.asset_id),
            )
            connection.executemany(
                "INSERT INTO project_assets(project_id, asset_id, role, sort_index) "
                "VALUES (?, ?, 'cat_reference', ?)",
                [
                    (project.project_id, reference.asset_id, index)
                    for index, reference in enumerate(manifest.cat_references)
                ],
            )
            connection.commit()

    def get_project(self, project_id: str) -> ProjectRecord:
        from app.domain.assets import SourceManifest

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Project {project_id} was not found")
        manifest = SourceManifest.model_validate_json(row["source_manifest_json"])
        manifest.assert_integrity()
        return ProjectRecord(
            project_id=row["project_id"],
            cat_name=row["cat_name"],
            cat_traits=row["cat_traits"],
            source_manifest=manifest,
            created_at=row["created_at"],
        )

    def create_search(
        self,
        *,
        search_id: str,
        thread_id: str,
        project: ProjectRecord,
        request: CreateSearchRequest,
        idempotency_key: str | None = None,
    ) -> SearchRunRecord:
        now = utcnow()
        request_fingerprint = hashlib.sha256(
            (
                project.project_id
                + "\0"
                + request.model_dump_json(exclude_none=False)
            ).encode("utf-8")
        ).hexdigest()
        resolved_search_id = search_id
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                existing = connection.execute(
                    """
                    SELECT search_id, idempotency_fingerprint FROM search_runs
                    WHERE project_id = ? AND idempotency_key = ?
                    """,
                    (project.project_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["idempotency_fingerprint"] != request_fingerprint:
                        connection.rollback()
                        raise ConflictError(
                            "Idempotency-Key was already used with a different search request"
                        )
                    resolved_search_id = str(existing["search_id"])
                    connection.commit()
                    return self.get_search(resolved_search_id)
            connection.execute(
                """
                INSERT INTO search_runs(
                    search_id, thread_id, project_id, status, source_manifest_hash,
                    placement_json, user_intent, candidate_count, max_rounds,
                    budget_usd, review_each_round, idempotency_key,
                    idempotency_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    search_id,
                    thread_id,
                    project.project_id,
                    SearchStatus.QUEUED.value,
                    project.source_manifest.manifest_hash,
                    request.placement.model_dump_json(),
                    request.user_intent,
                    request.candidate_count,
                    request.max_rounds,
                    request.budget_usd,
                    int(request.review_each_round),
                    idempotency_key,
                    request_fingerprint if idempotency_key is not None else None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
        return self.get_search(resolved_search_id)

    def get_search(self, search_id: str) -> SearchRunRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM search_runs WHERE search_id = ?", (search_id,)
            ).fetchone()
            candidate_rows = connection.execute(
                """
                SELECT record_json FROM candidates
                WHERE search_id = ? ORDER BY round_index, variant_index
                """,
                (search_id,),
            ).fetchall()
        if row is None:
            raise NotFoundError(f"Search {search_id} was not found")
        return self._search_from_row(row, candidate_rows)

    @staticmethod
    def _search_from_row(row: sqlite3.Row, candidate_rows: list[sqlite3.Row]) -> SearchRunRecord:
        from app.domain.searches import PlacementIntent

        return SearchRunRecord(
            search_id=row["search_id"],
            thread_id=row["thread_id"],
            project_id=row["project_id"],
            status=row["status"],
            source_manifest_hash=row["source_manifest_hash"],
            placement=PlacementIntent.model_validate_json(row["placement_json"]),
            user_intent=row["user_intent"],
            candidate_count=row["candidate_count"],
            max_rounds=row["max_rounds"],
            budget_usd=row["budget_usd"],
            review_each_round=bool(row["review_each_round"]),
            round_index=row["round_index"],
            round_winner_id=row["round_winner_id"],
            candidates=[
                CandidateRecord.model_validate_json(item["record_json"]) for item in candidate_rows
            ],
            global_winner_id=row["global_winner_id"],
            global_winner_score=row["global_winner_score"],
            round_history=json.loads(row["round_history_json"] or "[]"),
            active_directives=json.loads(row["active_directives_json"]),
            interrupt_payload=(
                json.loads(row["interrupt_payload_json"])
                if row["interrupt_payload_json"]
                else None
            ),
            stop_reason=row["stop_reason"],
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_search(
        self,
        search_id: str,
        *,
        status: SearchStatus | None = None,
        round_index: int | None = None,
        round_winner_id: str | None = None,
        global_winner_id: str | None = None,
        global_winner_score: float | None = None,
        round_history: Sequence[Mapping[str, object]] | None = None,
        active_directives: Sequence[Mapping[str, object]] | None = None,
        interrupt_payload: Mapping[str, object] | None = None,
        clear_interrupt_payload: bool = False,
        clear_round_winner: bool = False,
        clear_global_winner: bool = False,
        clear_stop_reason: bool = False,
        clear_state_summary: bool = False,
        clear_active_directives: bool = False,
        stop_reason: str | None = None,
        error: Mapping[str, object] | None = None,
        state_summary: Mapping[str, object] | None = None,
        clear_lease: bool = False,
        expected_statuses: Sequence[SearchStatus] | None = None,
        expected_lease_owner: str | None = None,
        events: Sequence[tuple[str, str, Mapping[str, object]]] = (),
    ) -> bool:
        """Conditionally update a search and append its resulting events atomically."""

        now = utcnow()
        assignments = ["updated_at = ?"]
        values: list[Any] = [now.isoformat()]
        optional = {
            "status": status.value if status else None,
            "round_index": round_index,
            "round_winner_id": round_winner_id,
            "global_winner_id": global_winner_id,
            "global_winner_score": global_winner_score,
            "round_history_json": (
                json.dumps(round_history, separators=(",", ":"))
                if round_history is not None
                else None
            ),
            "active_directives_json": (
                json.dumps(active_directives, separators=(",", ":"))
                if active_directives is not None
                else None
            ),
            "interrupt_payload_json": (
                json.dumps(interrupt_payload, separators=(",", ":"))
                if interrupt_payload is not None
                else None
            ),
            "stop_reason": stop_reason,
            "error_json": json.dumps(error, separators=(",", ":")) if error else None,
            "state_summary_json": (
                json.dumps(state_summary, separators=(",", ":")) if state_summary else None
            ),
        }
        for column, value in optional.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if clear_lease:
            assignments.extend(["lease_owner = NULL", "lease_until = NULL"])
        if clear_interrupt_payload:
            assignments.append("interrupt_payload_json = NULL")
        if clear_round_winner:
            assignments.append("round_winner_id = NULL")
        if clear_global_winner:
            assignments.extend(["global_winner_id = NULL", "global_winner_score = NULL"])
        if clear_stop_reason:
            assignments.append("stop_reason = NULL")
        if clear_state_summary:
            assignments.append("state_summary_json = NULL")
        if clear_active_directives:
            assignments.append("active_directives_json = '[]'")
        where = ["search_id = ?"]
        if expected_statuses is not None:
            if not expected_statuses:
                return False
            placeholders = ", ".join("?" for _ in expected_statuses)
            where.insert(0, f"status IN ({placeholders})")
            values.extend(item.value for item in expected_statuses)
            if expected_lease_owner is None:
                where.insert(1, "lease_owner IS NULL")
            else:
                where.insert(1, "lease_owner = ?")
                values.append(expected_lease_owner)
        values.append(search_id)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE search_runs SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
                values,
            )
            if cursor.rowcount == 1:
                for event_key, event_type, payload in events:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO search_events(
                            search_id, event_key, type, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            search_id,
                            event_key,
                            event_type,
                            json.dumps(
                                payload, separators=(",", ":"), sort_keys=True
                            ),
                            now.isoformat(),
                        ),
                    )
            connection.commit()
        if cursor.rowcount == 0:
            if expected_statuses is None:
                raise NotFoundError(f"Search {search_id} was not found")
            return False
        return True

    def get_search_lease_owner(self, search_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT lease_owner FROM search_runs WHERE search_id = ?",
                (search_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Search {search_id} was not found")
        return str(row["lease_owner"]) if row["lease_owner"] is not None else None

    def add_candidate(self, search_id: str, candidate: CandidateRecord) -> None:
        self.register_asset(candidate.raw_asset)
        self.register_asset(candidate.protected_asset)
        if candidate.composite is not None:
            self.register_asset(candidate.composite.mask.asset)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, search_id, round_index, variant_index,
                    request_key, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET record_json = excluded.record_json
                """,
                (
                    candidate.candidate_id,
                    search_id,
                    candidate.round_index,
                    candidate.variant_index,
                    candidate.request_key,
                    candidate.model_dump_json(),
                    utcnow().isoformat(),
                ),
            )
            connection.commit()

    def find_candidates_for_request(
        self, search_id: str, request_key: str
    ) -> list[CandidateRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT record_json FROM candidates
                WHERE search_id = ? AND request_key = ? ORDER BY variant_index
                """,
                (search_id, request_key),
            ).fetchall()
        return [CandidateRecord.model_validate_json(row["record_json"]) for row in rows]

    def emit_event(
        self,
        *,
        search_id: str,
        event_key: str,
        event_type: str,
        payload: dict[str, object],
    ) -> SearchEvent:
        now = utcnow()
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO search_events(
                    search_id, event_key, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (search_id, event_key, event_type, payload_json, now.isoformat()),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM search_events WHERE search_id = ? AND event_key = ?",
                (search_id, event_key),
            ).fetchone()
        assert row is not None
        return SearchEvent(
            id=row["id"],
            type=row["type"],
            search_id=row["search_id"],
            created_at=row["created_at"],
            payload=json.loads(row["payload_json"]),
        )

    def list_events(self, search_id: str, *, after_id: int = 0) -> list[SearchEvent]:
        self.get_search(search_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM search_events
                WHERE search_id = ? AND id > ? ORDER BY id
                """,
                (search_id, after_id),
            ).fetchall()
        return [
            SearchEvent(
                id=row["id"],
                type=row["type"],
                search_id=row["search_id"],
                created_at=row["created_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def claim_provider_call(
        self,
        *,
        request_key: str,
        operation: str,
        search_id: str,
        request_payload: dict[str, object],
        owner_id: str,
        lease_seconds: int,
        max_attempts: int | None = None,
    ) -> tuple[bool, str, dict[str, object] | None]:
        """Atomically reserve a provider side effect for exactly one caller."""

        if max_attempts is not None and max_attempts < 1:
            raise ValueError("provider max_attempts must be positive")

        now_value = utcnow()
        now = now_value.isoformat()
        lease_until = (now_value + timedelta(seconds=lease_seconds)).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO provider_calls(
                    request_key, operation, search_id, status, request_json,
                    attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, 'reserved', ?, 0, ?, ?)
                """,
                (
                    request_key,
                    operation,
                    search_id,
                    json.dumps(request_payload, separators=(",", ":"), sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT status, response_json, lease_until, attempt_count
                FROM provider_calls WHERE request_key = ?
                """,
                (request_key,),
            ).fetchone()
            assert row is not None
            status = str(row["status"])
            claimed = False
            stale_running = status == "running" and (
                row["lease_until"] is None or str(row["lease_until"]) < now
            )
            attempts_available = max_attempts is None or int(row["attempt_count"]) < max_attempts
            if attempts_available and (
                status in {"reserved", "failed_retryable"} or stale_running
            ):
                cursor = connection.execute(
                    """
                    UPDATE provider_calls
                    SET status = 'running', attempt_count = attempt_count + 1,
                        error_json = NULL, lease_owner = ?, lease_until = ?, updated_at = ?
                    WHERE request_key = ? AND status = ?
                      AND (? != 'running' OR lease_until IS NULL OR lease_until < ?)
                    """,
                    (
                        owner_id,
                        lease_until,
                        now,
                        request_key,
                        status,
                        status,
                        now,
                    ),
                )
                claimed = cursor.rowcount == 1
                if claimed:
                    status = "running"
            connection.commit()
        response = json.loads(row["response_json"]) if row["response_json"] else None
        return claimed, status, response

    def renew_provider_call_lease(
        self,
        *,
        request_key: str,
        owner_id: str,
        lease_seconds: int,
    ) -> bool:
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE provider_calls SET lease_until = ?, updated_at = ?
                WHERE request_key = ? AND lease_owner = ? AND status = 'running'
                """,
                (
                    lease_until.isoformat(),
                    now.isoformat(),
                    request_key,
                    owner_id,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def get_provider_call(
        self, request_key: str
    ) -> tuple[str, dict[str, object] | None] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status, response_json FROM provider_calls WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        if row is None:
            return None
        response = json.loads(row["response_json"]) if row["response_json"] else None
        return str(row["status"]), response

    def renew_search_lease(
        self, *, search_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE search_runs SET lease_until = ?, updated_at = ?
                WHERE search_id = ? AND lease_owner = ? AND status = 'running'
                """,
                (
                    lease_until.isoformat(),
                    now.isoformat(),
                    search_id,
                    worker_id,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def complete_provider_call(
        self,
        request_key: str,
        response: Mapping[str, object],
        *,
        owner_id: str,
    ) -> bool:
        parameters: list[object] = [
            json.dumps(response, separators=(",", ":"), sort_keys=True),
            utcnow().isoformat(),
            request_key,
            owner_id,
        ]
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE provider_calls SET status = 'completed', response_json = ?,
                    error_json = NULL, lease_owner = NULL, lease_until = NULL,
                    updated_at = ?
                WHERE request_key = ? AND lease_owner = ? AND status = 'running'
                """,
                parameters,
            )
            connection.commit()
        return cursor.rowcount == 1

    def fail_provider_call(
        self, request_key: str, message: str, *, owner_id: str
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE provider_calls SET status = 'failed_retryable', error_json = ?,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE request_key = ? AND lease_owner = ? AND status = 'running'
                """,
                (
                    json.dumps({"message": message}, separators=(",", ":")),
                    utcnow().isoformat(),
                    request_key,
                    owner_id,
                ),
            )
            connection.commit()

    def provider_attempt_count(self, request_key: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM provider_calls WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        return int(row["attempt_count"]) if row else 0

    def save_evaluation(
        self,
        search_id: str,
        evaluation: CandidateEvaluation,
        *,
        score: float | None = None,
    ) -> None:
        """Persist one structured evaluation idempotently; image bytes are never stored."""

        if score is not None and (not math.isfinite(score) or not 0 <= score <= 100):
            raise ValueError("evaluation score must be finite and between 0 and 100")
        if evaluation.source_manifest_hash is None:
            raise ConflictError("Persisted evaluations require a source manifest hash")

        evaluation_id = hashlib.sha256(
            f"{search_id}:{evaluation.candidate_id}:{evaluation.rubric_version}".encode()
        ).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate_row = connection.execute(
                """
                SELECT c.search_id, c.round_index, s.source_manifest_hash
                FROM candidates AS c
                JOIN search_runs AS s ON s.search_id = c.search_id
                WHERE c.candidate_id = ?
                """,
                (evaluation.candidate_id,),
            ).fetchone()
            if candidate_row is None:
                connection.rollback()
                raise NotFoundError(
                    f"Candidate {evaluation.candidate_id} was not found"
                )
            if str(candidate_row["search_id"]) != search_id:
                connection.rollback()
                raise ConflictError("Evaluation candidate does not belong to search")
            if int(candidate_row["round_index"]) != evaluation.round_index:
                connection.rollback()
                raise ConflictError("Evaluation round does not match candidate lineage")
            if (
                str(candidate_row["source_manifest_hash"])
                != evaluation.source_manifest_hash
            ):
                connection.rollback()
                raise ConflictError("Evaluation source manifest does not match search")
            connection.execute(
                """
                INSERT INTO candidate_evaluations(
                    evaluation_id, search_id, candidate_id, round_index,
                    rubric_version, evaluation_json, score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id, rubric_version) DO UPDATE SET
                    evaluation_json = excluded.evaluation_json,
                    score = excluded.score
                """,
                (
                    evaluation_id,
                    search_id,
                    evaluation.candidate_id,
                    evaluation.round_index,
                    evaluation.rubric_version,
                    evaluation.model_dump_json(),
                    score,
                    utcnow().isoformat(),
                ),
            )
            connection.commit()

    def list_evaluations(self, search_id: str) -> list[CandidateEvaluation]:
        self.get_search(search_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT evaluation_json FROM candidate_evaluations
                WHERE search_id = ? ORDER BY round_index, candidate_id
                """,
                (search_id,),
            ).fetchall()
        return [CandidateEvaluation.model_validate_json(row["evaluation_json"]) for row in rows]

    def claim_next_search(self, *, worker_id: str, lease_seconds: int) -> str | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT search_id FROM search_runs
                WHERE status = 'queued'
                   OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
                ORDER BY created_at LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE search_runs SET status = 'running', lease_owner = ?,
                    lease_until = ?, updated_at = ? WHERE search_id = ?
                """,
                (worker_id, lease_until.isoformat(), now.isoformat(), row["search_id"]),
            )
            connection.commit()
            return str(row["search_id"])

    def claim_search(
        self, *, search_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        now = utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE search_runs SET status = 'running', lease_owner = ?,
                    lease_until = ?, updated_at = ?
                WHERE search_id = ? AND (
                    status = 'queued'
                    OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?))
                )
                """,
                (
                    worker_id,
                    lease_until.isoformat(),
                    now.isoformat(),
                    search_id,
                    now.isoformat(),
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def queue_next_round(self, search_id: str) -> bool:
        """Atomically move one human-reviewed search to its next round."""

        now = utcnow().isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE search_runs
                SET status = 'queued', round_index = round_index + 1,
                    round_winner_id = NULL, stop_reason = NULL,
                    state_summary_json = NULL, interrupt_payload_json = NULL,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE search_id = ? AND status = 'waiting_for_human'
                  AND round_index + 1 < max_rounds
                """,
                (now, search_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def accept_search(self, search_id: str) -> bool:
        """Atomically accept only a waiting search with a persisted winner."""

        now = utcnow().isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE search_runs
                SET status = 'accepted', stop_reason = 'accepted_global_winner',
                    interrupt_payload_json = NULL, lease_owner = NULL,
                    lease_until = NULL, updated_at = ?
                WHERE search_id = ? AND status = 'waiting_for_human'
                  AND global_winner_id IS NOT NULL
                """,
                (now, search_id),
            )
            if cursor.rowcount == 1:
                winner = connection.execute(
                    """
                    SELECT global_winner_id, global_winner_score
                    FROM search_runs WHERE search_id = ?
                    """,
                    (search_id,),
                ).fetchone()
                assert winner is not None
                connection.execute(
                    """
                    INSERT OR IGNORE INTO search_events(
                        search_id, event_key, type, payload_json, created_at
                    ) VALUES (?, 'search:accepted', 'search.accepted', ?, ?)
                    """,
                    (
                        search_id,
                        json.dumps(
                            {
                                "global_winner_id": winner["global_winner_id"],
                                "global_winner_score": winner["global_winner_score"],
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
            connection.commit()
        return cursor.rowcount == 1

    def cancel_search(self, search_id: str) -> bool:
        """Atomically cancel any non-terminal search; terminal repeats are no-ops."""

        now = utcnow().isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE search_runs
                SET status = 'cancelled', stop_reason = 'cancelled_by_user',
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE search_id = ?
                  AND status IN ('queued', 'running', 'waiting_for_human')
                """,
                (now, search_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO search_events(
                        search_id, event_key, type, payload_json, created_at
                    ) VALUES (
                        ?, 'search:cancelled', 'search.cancelled',
                        '{"reason":"cancelled_by_user"}', ?
                    )
                    """,
                    (search_id, now),
                )
            connection.commit()
        return cursor.rowcount == 1
