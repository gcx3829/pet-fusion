from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import ContainerDependency

router = APIRouter(prefix="/searches", tags=["search-events"])


@router.get("/{search_id}/events", response_class=StreamingResponse)
async def stream_search_events(
    search_id: str,
    request: Request,
    container: ContainerDependency,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    cursor = after
    if last_event_id is not None:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            cursor = after
    container.app_store.get_search(search_id)

    async def event_stream() -> AsyncIterator[str]:
        nonlocal cursor
        while True:
            events = container.app_store.list_events(search_id, after_id=cursor)
            for event in events:
                cursor = event.id
                yield event.to_sse()
            search = container.app_store.get_search(search_id)
            if search.status.is_stream_terminal and not events:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
