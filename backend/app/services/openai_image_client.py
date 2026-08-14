from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class OpenAIImageInput:
    """One in-memory image sent to the Image API for the lifetime of a request.

    ``png_bytes`` is retained as the field name for compatibility with the first
    provider adapter.  ``mime_type`` describes the actual payload and allows
    opaque proxy images to use a much smaller JPEG representation.
    """

    filename: str
    png_bytes: bytes
    mime_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class OpenAIImageEditResult:
    png_images: tuple[bytes, ...]
    request_id: str | None
    usage: Mapping[str, object]


class OpenAIImageEditsTransport(Protocol):
    async def edit(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[OpenAIImageInput],
        n: int,
        quality: str,
        size: str,
    ) -> OpenAIImageEditResult: ...


class OfficialOpenAIImageEditsTransport:
    """Thin, lazy adapter around the official OpenAI Python SDK.

    Importing this module never imports the SDK or constructs a network client, so
    the default fake-generator test path remains fully offline.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or None
        self._client_factory = client_factory
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(
                api_key=self._api_key,
                base_url=self._base_url,
            )
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - dependency is a runtime requirement
            raise ConfigurationError("The real image provider requires the openai package") from exc
        self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    async def edit(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[OpenAIImageInput],
        n: int,
        quality: str,
        size: str,
    ) -> OpenAIImageEditResult:
        client = self._get_client()
        try:
            return await self._edit_once(
                client=client,
                model=model,
                prompt=prompt,
                images=images,
                n=(n if n > 1 else None),
                quality=quality,
                size=size,
            )
        except Exception as exc:
            if n <= 1 or "tools[0].n" not in str(exc):
                raise
            # Some OpenAI-compatible relays translate Image API requests to the
            # Responses image tool, which rejects the Image API's multi-output
            # ``n`` field. Retry only this explicit compatibility error as
            # independent single-output requests so candidate_count semantics stay
            # intact for the caller.
            results = [
                await self._edit_once(
                    client=client,
                    model=model,
                    prompt=prompt,
                    images=images,
                    n=None,
                    quality=quality,
                    size=size,
                )
                for _ in range(n)
            ]
            return OpenAIImageEditResult(
                png_images=tuple(
                    image
                    for result in results
                    for image in result.png_images
                ),
                request_id=next(
                    (result.request_id for result in results if result.request_id),
                    None,
                ),
                usage=_merge_numeric_usage(results),
            )

    @staticmethod
    async def _edit_once(
        *,
        client: Any,
        model: str,
        prompt: str,
        images: Sequence[OpenAIImageInput],
        n: int | None,
        quality: str,
        size: str,
    ) -> OpenAIImageEditResult:
        kwargs: dict[str, object] = {
            "model": model,
            "prompt": prompt,
            "image": [(image.filename, image.png_bytes, image.mime_type) for image in images],
            "quality": quality,
            "size": size,
            "output_format": "png",
        }
        if n is not None:
            kwargs["n"] = n
        response = await client.images.edit(**kwargs)
        encoded_images = tuple(item.b64_json for item in response.data)
        if any(not isinstance(encoded, str) for encoded in encoded_images):
            raise RuntimeError("OpenAI Image API response did not include PNG base64 data")
        usage: object = getattr(response, "usage", None)
        model_dump = getattr(usage, "model_dump", None)
        if callable(model_dump):
            usage = model_dump(mode="json", exclude_none=True)
        return OpenAIImageEditResult(
            png_images=tuple(
                base64.b64decode(encoded, validate=True) for encoded in encoded_images
            ),
            request_id=getattr(response, "_request_id", None),
            usage=dict(usage) if isinstance(usage, Mapping) else {},
        )


def _merge_numeric_usage(results: Sequence[OpenAIImageEditResult]) -> dict[str, object]:
    merged: dict[str, int | float] = {}
    for result in results:
        for key, value in result.usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            merged[key] = merged.get(key, 0) + value
    return dict(merged)
