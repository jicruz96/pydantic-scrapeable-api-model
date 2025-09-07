from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from glob import glob
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Literal,
    Sequence,
    TypeVar,
)
from urllib.parse import urljoin, urlparse

import httpx
from ji_async_http_utils.httpx import SetClient, lifespan, request
from pydantic import BaseModel, Field
from pydantic.main import IncEx
from pydantic_cacheable_model import CacheableModel, CacheKey

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

T = TypeVar("T")

__all__ = [
    "ScrapeableApiModel",
    "DetailField",
    "CustomScrapeField",
    "UnscrapedDetailFieldType",
    "CacheKey",
]


class UnscrapedDetailFieldType(BaseModel):
    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0


# Fields annotated with ``DetailField`` start as ``UnscrapedDetailFieldType``
# placeholders and are filled only after ``scrape_detail`` or a custom getter runs.
DetailField = Annotated[
    T | UnscrapedDetailFieldType, Field(default_factory=UnscrapedDetailFieldType)
]


def CustomScrapeField(method_name: str, **kwargs: Any) -> Any:
    """Field wrapper for values scraped by a custom instance method.

    Args:
        method_name: Name of the coroutine method on the instance that
            populates this field.
        **kwargs: Additional ``Field`` keyword arguments.

    Returns:
        A ``Field`` configured with an ``UnscrapedDetailFieldType`` placeholder and
        metadata pointing to the custom scraper method.
    """

    return Field(
        default_factory=UnscrapedDetailFieldType,
        json_schema_extra={"scrape_method": method_name},
        **kwargs,
    )

_error_log_lock: asyncio.Lock | None = None


class ScrapeableApiModel(CacheableModel):
    logger: ClassVar[logging.Logger] = logging.getLogger(__name__)
    BASE_URL: ClassVar[str] = ""
    list_endpoint: ClassVar[str | None] = None

    @property
    def detail_endpoint(self) -> str | None:
        """Return a per-instance detail endpoint or ``None``.

        Subclasses may override to provide a relative path (e.g.,
        ``f"/items/{self.id}"``) that will be joined with ``BASE_URL``. Returning
        ``None`` disables the automatic detail fetch in ``scrape_detail``.
        """
        return None

    def __init_subclass__(cls, **kwargs: Any):
        """Validate subclass configuration when defined.

        Ensures a non-empty ``BASE_URL`` is set so relative endpoints can be
        joined correctly.
        """
        assert bool(cls.BASE_URL)  # enforce existence of a url
        super().__init_subclass__(**kwargs)

    def unscraped_fields(self) -> list[str]:
        """Return names of fields that are still placeholders (unscraped)."""
        return [
            field for field, value in self if isinstance(value, UnscrapedDetailFieldType)
        ]

    @classmethod
    def _iter_all_subclasses(cls) -> list[type["ScrapeableApiModel"]]:
        seen: set[type[ScrapeableApiModel]] = set()
        queue: list[type[ScrapeableApiModel]] = list(cls.__subclasses__())
        result: list[type[ScrapeableApiModel]] = []
        while queue:
            sub = queue.pop(0)
            if sub in seen:
                continue
            seen.add(sub)
            result.append(sub)
            queue.extend(sub.__subclasses__())
        return result

    @classmethod
    def _build_url(cls, endpoint: str) -> str:
        parsed = urlparse(endpoint)
        if parsed.scheme in {"http", "https"}:
            return endpoint
        base = cls.BASE_URL
        # Ensure trailing slash so urljoin appends rather than replaces last segment
        if not base.endswith("/"):
            base = base + "/"
        return urljoin(base, endpoint.lstrip("/"))

    @classmethod
    async def run(
        cls,
        *,
        set_client: SetClient | None = None,
        use_cache: bool,
        check_api: bool,
        scrape_details: bool = True,
    ) -> None:
        """Run all discovered subclass scrapers concurrently.

        Manages the HTTP client lifespan, discovers all subclasses recursively,
        and executes ``scrape_list`` for each class that defines ``list_endpoint``.

        Args:
            set_client: Optional factory to create a custom ``httpx.AsyncClient``.
            use_cache: Whether to read/write cache during scraping.
            check_api: When ``False``, skip network and only use cached data.
            scrape_details: If ``True`` (default), also scrape the detail endpoint for
                each item returned by ``scrape_list``.
        """
        async with lifespan(set_client=set_client):
            tasks = [
                subclass.scrape_list(
                    check_api=check_api,
                    use_cache=use_cache,
                    scrape_details=scrape_details,
                )
                for subclass in cls._iter_all_subclasses()
                if subclass.list_endpoint
            ]
            cls.logger.info(
                "Running %d scraper(s) discovered under %s", len(tasks), cls.__name__
            )
            if tasks:
                await asyncio.gather(*tasks)

    @classmethod
    def response_to_models(cls, resp: httpx.Response) -> Sequence[Self]:
        """Map an HTTP response to model instances.

        Override in subclasses to adapt to non-trivial response shapes
        (e.g., nested keys, pagination envelopes). The default implementation
        expects a JSON array of objects directly parseable into ``cls``.

        Args:
            resp: The successful ``httpx.Response`` object.

        Returns:
            A sequence of instantiated models.
        """
        return [cls(**i) for i in resp.json()]

    # Backwards-compat alias
    _response_to_models = response_to_models

    @classmethod
    async def scrape_list(
        cls,
        check_api: bool | str,
        *,
        use_cache: bool,
        scrape_details: bool = True,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> Sequence[Self]:
        """Return model instances from the list endpoint or cache.

        Optionally performs detail scraping for each model. Merges fresh list
        results with cached items by ``cache_key`` and removes stale cache
        entries when a class ``list_endpoint`` is defined.

        Args:
            check_api: ``True`` to use ``list_endpoint``; ``False`` to skip API;
                a string to override the endpoint.
            use_cache: ``True`` to read/write cache; ``False`` to ignore cache.
        scrape_details: When ``True`` (default), invoke ``scrape_detail`` on each
            model before returning.
            raise_on_status_except_for: Status codes that should not raise.

        Returns:
            List of model instances parsed from the response or cache.
        """
        existing = (
            {}
            if not use_cache
            else {item.cache_key: item for item in cls.load_all_cached()}
        )
        if not check_api:
            return list(existing.values())

        if not (
            endpoint := cls.list_endpoint if isinstance(check_api, bool) else check_api
        ):
            raise ValueError(
                f"Cannot check API for {cls.__name__} because list_endpoint is "
                "not set and check_api did not provide an override"
            )
        url = cls._build_url(endpoint)
        cls.logger.info("Scraping %s from %s", cls.__name__, url)
        resp = await cls.request(
            id="fetch_all",
            url=url,
            headers={"Accept": "application/json"},
            raise_on_status_except_for=raise_on_status_except_for,
        )
        if resp is None:
            cls.logger.warning(
                "Fetch failed for %s list; returning %d cached item(s)",
                cls.__name__,
                len(existing),
            )
            return list(existing.values())

        models = [
            existing.get(item.cache_key, item) for item in cls.response_to_models(resp)
        ]

        for model in models:
            model.cache()

        if scrape_details:
            await asyncio.gather(
                *(m.scrape_detail(use_cache=use_cache) for m in models)
            )

        if cls.list_endpoint:
            existing_cache_files = set(
                glob(os.path.join(cls.cache_dir_path(), "*.json"))
            )
            stale_data = existing_cache_files - {m.cache_path for m in models}
            for fpath in stale_data:
                os.remove(fpath)
            if stale_data:
                cls.logger.info(
                    "Removed %d stale cache file(s) for %s",
                    len(stale_data),
                    cls.__name__,
                )

        return models

    @classmethod
    async def get(
        cls, *, cache_key: str, not_found_ok: bool = False, check_api: bool = False
    ) -> Self | None:
        """Load a single instance by cache key.

        Attempts to load from cache first; if not found and ``check_api`` is
        true, refreshes the cache by scraping the list and retries.

        Args:
            cache_key: Identifier used to locate the cached object.
            not_found_ok: If true, return ``None`` when not found.
            check_api: If true and not found in cache, query the API.

        Returns:
            The instance or ``None`` if not found and allowed.
        """
        obj = cls.load(cache_key=cache_key)
        if not obj and check_api:
            await cls.scrape_list(check_api=True, use_cache=True)  # hack
            obj = cls.load(cache_key=cache_key)
        if not obj and not not_found_ok:
            raise ValueError(f"Could not find {cls.__name__}({cache_key})")
        return obj

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: IncEx | None = None,
        exclude: IncEx | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        fallback: Callable[[Any], Any] | None = None,
        serialize_as_any: bool = False,
    ) -> dict[str, Any]:
        return {
            k: v
            for k, v in super()
            .model_dump(
                mode=mode,
                include=include,
                exclude=exclude,
                context=context,
                by_alias=by_alias,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
                round_trip=round_trip,
                warnings=warnings,
                fallback=fallback,
                serialize_as_any=serialize_as_any,
            )
            .items()
            if k not in self.unscraped_fields()
        }

    async def scrape_detail(self, *, use_cache: bool = True) -> None:
        """Populate remaining fields via cache, field getters, and detail API.

        1) Merge fields from a cached instance when available and allowed.
        2) Invoke any custom scraper methods declared via ``CustomScrapeField``
           for fields still in ``UnscrapedDetailFieldType`` state.
        3) If fields remain and ``detail_endpoint`` is provided, perform a GET
           and update attributes from the JSON response body.
        4) Cache the result.

        Args:
            use_cache: Whether to read/write cache during processing.
        """
        if use_cache and (
            existing := await self.get(cache_key=self.cache_key, not_found_ok=True)
        ):
            for field, val in existing:
                if field in self.unscraped_fields():
                    setattr(self, field, val)
        for field in self.unscraped_fields():
            field_info = type(self).model_fields[field]
            scrape_method = (
                (field_info.json_schema_extra or {}).get("scrape_method")
            )
            if scrape_method and (getter := getattr(self, scrape_method, None)):
                await getter()
        self.cache()
        if (
            self.unscraped_fields()
            and self.detail_endpoint
            and (
                response := await self.request(
                    id=self.cache_key,
                    url=self._build_url(self.detail_endpoint),
                    headers={"Accept": "application/json"},
                    raise_on_status_except_for=[400, 404, 500],
                )
            )
        ):
            for field, value in response.json().items():
                setattr(self, field, value)
            self.cache()

    @classmethod
    async def log_scrape_error(cls, **data: str | int | None) -> None:
        """Append a structured error record to ``scrape-errors.jsonl``.

        Each row includes an ISO-8601 timestamp, the model name, and any
        keyword data provided (e.g., request id, URL, status, message).
        """
        row: dict[str, str | int | None] = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "model": cls.__name__,
            **data,
        }
        log_filepath = os.path.join(cls.CACHE_ROOT, "scrape-errors.jsonl")

        def _log_error() -> None:
            with open(log_filepath, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(row) + "\n")

        global _error_log_lock
        if _error_log_lock is None:
            _error_log_lock = asyncio.Lock()
        async with _error_log_lock:
            await asyncio.to_thread(_log_error)

    @classmethod
    async def request(
        cls,
        *,
        id: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> httpx.Response | None:
        """HTTP request wrapper that returns ``None`` on non-success.

        Delegates to the shared async client in ``ji_async_http_utils.httpx``.
        When ``resp.is_success`` is false and the status is not in
        ``raise_on_status_except_for``, the error is recorded via
        ``log_scrape_error`` and ``None`` is returned.

        Args:
            id: Identifier used in error logs for this request.
            url: Absolute URL to request.
            headers: Optional headers to include.
            params: Optional query parameters to include.
            raise_on_status_except_for: Status codes that should not raise.

        Returns:
            The successful ``httpx.Response`` or ``None`` on failure.
        """
        resp = await request(
            url,
            headers=headers or {},
            params=params or {},
            raise_on_status_except_for=raise_on_status_except_for,
        )
        if not resp.is_success:
            await cls.log_scrape_error(
                id=id,
                url=url,
                status=resp.status_code,
                message=resp.text[:500],
            )
            return None
        return resp
