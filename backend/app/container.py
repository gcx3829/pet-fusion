from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.domain.errors import ConfigurationError
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.generator_service import (
    DeterministicFakeImageGenerator,
    GeneratorService,
    ImageGenerator,
)
from app.services.search_runner import SearchRunner


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
    ) -> AppContainer:
        if image_generator is None:
            if not settings.fake_generator:
                raise ConfigurationError(
                    "The first vertical slice only enables the deterministic fake generator"
                )
            image_generator = DeterministicFakeImageGenerator()
        app_store = AppStore(settings.resolved_app_db_path)
        asset_store = AssetStore(settings.asset_dir, max_image_pixels=settings.max_image_pixels)
        generator_service = GeneratorService(
            provider=image_generator,
            asset_store=asset_store,
            app_store=app_store,
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
            ),
        )

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.asset_store.initialize()
        self.app_store.initialize()
