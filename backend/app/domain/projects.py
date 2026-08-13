from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.assets import PublicSourceManifest, SourceManifest


class ProjectRecord(BaseModel):
    project_id: str
    cat_name: str | None = None
    cat_traits: str | None = None
    source_manifest: SourceManifest
    created_at: datetime


class ProjectResponse(BaseModel):
    project_id: str
    cat_name: str | None = None
    cat_traits: str | None = None
    source_manifest: PublicSourceManifest
    created_at: datetime

    @classmethod
    def from_record(cls, project: ProjectRecord) -> ProjectResponse:
        return cls(
            project_id=project.project_id,
            cat_name=project.cat_name,
            cat_traits=project.cat_traits,
            source_manifest=PublicSourceManifest.from_internal(project.source_manifest),
            created_at=project.created_at,
        )
