from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.domain.errors import ConfigurationError
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import CriticProvider
from app.services.generator_service import (
    FAKE_IMAGE_MODEL,
    DeterministicFakeImageGenerator,
    GeneratorService,
    ImageGenerator,
    OpenAIImageGenerator,
)
from app.services.openai_critic_client import OfficialOpenAICriticProvider
from app.services.openai_image_client import OfficialOpenAIImageEditsTransport
from app.services.planner_service import FeedbackPlannerService, PlannerProvider
from app.services.proxy_builder import CriticProxyBuilder
from app.services.search_runner import SearchRunner
from app.services.stop_policy import DeterministicStopPolicy


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    app_store: AppStore
    asset_store: AssetStore
    image_generator: ImageGenerator
    generator_service: GeneratorService
    search_runner: SearchRunner

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        image_generator: ImageGenerator | None = None,
        critic_service: CriticProvider | None = None,
        critic_proxy_builder: CriticProxyBuilder | None = None,
        candidate_ranker: DeterministicCandidateRanker | None = None,
        stop_policy: DeterministicStopPolicy | None = None,
        planner_provider: PlannerProvider | None = None,
        planner_service: FeedbackPlannerService | None = None,
    ) -> AppContainer:
        if image_generator is None:
            if settings.fake_generator:
                image_generator = DeterministicFakeImageGenerator()
            elif settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
                raise ConfigurationError(
                    "OPENAI_API_KEY is required when FAKE_GENERATOR is disabled"
                )
            else:
                image_generator = OpenAIImageGenerator(
                    transport=OfficialOpenAIImageEditsTransport(
                        api_key=settings.openai_api_key.get_secret_value(),
                        base_url=settings.openai_base_url,
                    )
                )
        app_store = AppStore(settings.resolved_app_db_path)
        asset_store = AssetStore(settings.asset_dir, max_image_pixels=settings.max_image_pixels)
        resolved_critic_service = critic_service
        if resolved_critic_service is None and not settings.fake_critic:
            if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
                raise ConfigurationError(
                    "OPENAI_API_KEY is required when FAKE_CRITIC is disabled"
                )
            resolved_critic_service = OfficialOpenAICriticProvider(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                model=settings.openai_critic_model,
                asset_store=asset_store,
            )
        generator_service = GeneratorService(
            provider=image_generator,
            asset_store=asset_store,
            app_store=app_store,
            model=(FAKE_IMAGE_MODEL if settings.fake_generator else settings.openai_image_model),
            quality=settings.openai_image_quality,
            size=(None if settings.fake_generator else settings.openai_image_size),
        )
        return cls(
            settings=settings,
            app_store=app_store,
            asset_store=asset_store,
            image_generator=image_generator,
            generator_service=generator_service,
            search_runner=SearchRunner(
                app_store=app_store,
                generator_service=generator_service,
                checkpoint_path=settings.resolved_checkpoint_db_path,
                critic_service=resolved_critic_service,
                critic_proxy_builder=critic_proxy_builder,
                candidate_ranker=candidate_ranker,
                stop_policy=stop_policy,
                planner_service=(
                    planner_service
                    if planner_service is not None
                    else FeedbackPlannerService(provider=planner_provider)
                ),
            ),
        )

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.asset_store.initialize()
        self.app_store.initialize()
