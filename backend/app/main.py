from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import assets, events, exports, fusions, local_fixes, projects, searches
from app.config import Settings, get_settings
from app.container import AppContainer
from app.domain.errors import DomainError
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import DeterministicCriticService
from app.services.generator_service import ImageGenerator
from app.services.local_fix_service import LocalFixProvider
from app.services.planner_service import FeedbackPlannerService, PlannerProvider
from app.services.stop_policy import DeterministicStopPolicy


def create_app(
    settings: Settings | None = None,
    *,
    image_generator: ImageGenerator | None = None,
    critic_service: DeterministicCriticService | None = None,
    candidate_ranker: DeterministicCandidateRanker | None = None,
    stop_policy: DeterministicStopPolicy | None = None,
    planner_provider: PlannerProvider | None = None,
    planner_service: FeedbackPlannerService | None = None,
    local_fix_provider: LocalFixProvider | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    container = AppContainer.build(
        resolved_settings,
        image_generator=image_generator,
        critic_service=critic_service,
        candidate_ranker=candidate_ranker,
        stop_policy=stop_policy,
        planner_provider=planner_provider,
        planner_service=planner_service,
        local_fix_provider=local_fix_provider,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container.initialize()
        app.state.container = container
        yield

    app = FastAPI(
        title="Pet Fusion API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.parsed_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"],
    )

    @app.exception_handler(DomainError)
    async def handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = []
        for item in exc.errors():
            details.append(
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
            )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Request validation failed",
                    "details": details,
                }
            },
        )

    @app.get("/health", include_in_schema=False)
    @app.get("/api/v1/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "generator": "fake" if resolved_settings.fake_generator else "openai",
        }

    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(searches.router, prefix="/api/v1")
    app.include_router(local_fixes.router, prefix="/api/v1")
    app.include_router(exports.router, prefix="/api/v1")
    app.include_router(fusions.router, prefix="/api/v1")
    app.include_router(events.router, prefix="/api/v1")
    app.include_router(assets.router, prefix="/api/v1")
    return app


app = create_app()
