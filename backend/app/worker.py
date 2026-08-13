from __future__ import annotations

import asyncio
import logging
import socket
from uuid import uuid4

from app.config import get_settings
from app.container import AppContainer

logger = logging.getLogger("pet_fusion.worker")


async def run_worker() -> None:
    settings = get_settings()
    container = AppContainer.build(settings)
    container.initialize()
    worker_id = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    logger.info("worker started: %s", worker_id)
    while True:
        search_id = container.app_store.claim_next_search(
            worker_id=worker_id,
            lease_seconds=settings.worker_lease_seconds,
        )
        if search_id is None:
            await asyncio.sleep(settings.worker_poll_seconds)
            continue
        try:
            await container.search_runner.run_with_lease(
                search_id=search_id,
                worker_id=worker_id,
                lease_seconds=settings.worker_lease_seconds,
            )
        except Exception:
            logger.exception("search failed: %s", search_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
