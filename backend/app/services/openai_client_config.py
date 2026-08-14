from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from app.domain.errors import ConfigurationError


def normalize_openai_base_url(base_url: str | None) -> str | None:
    """Return an SDK-compatible OpenAI base URL without exposing its value.

    A bare relay origin is treated as an OpenAI-compatible root and receives the
    conventional ``/v1`` path.  Explicit relay path prefixes are preserved.
    Queries and fragments are rejected because HTTPX joins relative SDK endpoint
    paths into the query/fragment instead of beneath the configured path.
    """

    if base_url is None:
        return None
    value = base_url.strip()
    if not value:
        return None

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("OPENAI_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("OPENAI_BASE_URL must not include credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "OPENAI_BASE_URL must not include query parameters or a fragment"
        )

    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit(parsed._replace(path=path))
