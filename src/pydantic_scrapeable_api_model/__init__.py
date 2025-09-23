from __future__ import annotations

import asyncio
import inspect
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
    get_args,
    get_type_hints,
    overload,
)
from urllib.parse import urljoin, urlparse

import aiohttp
import ji_async_http_utils.aiohttp
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo
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


def _get_scrape_method(field: FieldInfo) -> str | None:
    """Return the custom scrape method name for a field, if any."""
    extra = field.json_schema_extra
    if isinstance(extra, dict):
        method = extra.get("scrape_method")  # type: ignore
        if isinstance(method, str):
            return method
    return None


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

    @classmethod
    def get_custom_scrape_methods(
        cls,
    ) -> dict[str, Callable[..., Any]]:
        scrape_methods = dict[str, Callable[..., Any]]()
        for field_name, field in cls.model_fields.items():
            method_name = _get_scrape_method(field)
            if not method_name:
                continue
            method = getattr(cls, method_name, None)
            if method is None:
                raise AttributeError(
                    f"{cls.__name__}.{field_name} references unknown method '{method_name}'"
                )
            if not inspect.iscoroutinefunction(method):
                raise TypeError(
                    f"Custom scraper '{method_name}' for field '{field_name}' must be async"
                )

            # Validate method signature: allow (self) or (self, session: aiohttp.ClientSession)
            sig = inspect.signature(method)
            params = list(sig.parameters.values())
            if not params or params[0].name != "self":
                raise TypeError(
                    f"Custom scraper '{method_name}' for field '{field_name}' must be an instance method with first parameter 'self'"
                )
            extra_params = params[1:]
            if any(
                p.kind
                in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for p in extra_params
            ):
                raise TypeError(
                    f"Custom scraper '{method_name}' for field '{field_name}' must not use *args/**kwargs"
                )
            if len(extra_params) > 1:
                raise TypeError(
                    f"Custom scraper '{method_name}' for field '{field_name}' may have at most one extra parameter 'session'"
                )
            if extra_params:
                session_param = extra_params[0]
                if session_param.name != "session":
                    raise TypeError(
                        f"Custom scraper '{method_name}' for field '{field_name}' second parameter must be named 'session'"
                    )
                hints = get_type_hints(method)
                session_type = hints.get("session")
                if session_type is None:
                    raise TypeError(
                        f"Custom scraper '{method_name}' for field '{field_name}' must type-annotate 'session: aiohttp.ClientSession'"
                    )
                if session_type is not aiohttp.ClientSession:
                    raise TypeError(
                        f"Custom scraper '{method_name}' for field '{field_name}' 'session' parameter must be of type aiohttp.ClientSession"
                    )

            expected_type = field.annotation
            args = [
                a for a in get_args(expected_type) if a is not UnscrapedDetailFieldType
            ]
            allowed_types = tuple(args) if args else (expected_type,)

            return_type = get_type_hints(method).get("return")
            if return_type is None:
                raise TypeError(
                    f"Custom scraper '{method_name}' for field '{field_name}' must declare a return type"
                )
            if return_type not in allowed_types:
                raise TypeError(
                    f"Custom scraper '{method_name}' for field '{field_name}' returns {return_type}, expected one of {allowed_types}"
                )

            scrape_methods[field_name] = method
        return scrape_methods

    def __init_subclass__(cls, **kwargs: Any):
        """Validate subclass configuration when defined.

        Ensures a non-empty ``BASE_URL`` is set so relative endpoints can be
        joined correctly and validates any ``CustomScrapeField`` definitions.
        """
        assert bool(cls.BASE_URL)  # enforce existence of a url
        super().__init_subclass__(**kwargs)
        cls.get_custom_scrape_methods()

    def unscraped_fields(self) -> list[str]:
        """Return names of fields that are still placeholders (unscraped)."""
        return [
            field
            for field, value in self
            if isinstance(value, UnscrapedDetailFieldType)
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
        use_cache: bool,
        check_api: bool,
        scrape_details: bool = True,
    ) -> None:
        """Run all discovered subclass scrapers concurrently.

        Manages a single ``aiohttp.ClientSession`` lifespan, discovers all
        subclasses recursively, and executes ``scrape_list`` for each class
        that defines ``list_endpoint``.

        Args:
            use_cache: Whether to read/write cache during scraping.
            check_api: When ``False``, skip network and only use cached data.
            scrape_details: If ``True`` (default), also scrape the detail endpoint for
                each item returned by ``scrape_list``.
        """

        async def _run(session: aiohttp.ClientSession | None = None):
            tasks = [
                subclass.scrape_list(
                    check_api=check_api,
                    use_cache=use_cache,
                    scrape_details=scrape_details,
                    session=session,
                )
                for subclass in cls._iter_all_subclasses()
                if subclass.list_endpoint
            ]
            cls.logger.info(
                "Running %d scraper(s) discovered under %s", len(tasks), cls.__name__
            )
            if tasks:
                await asyncio.gather(*tasks)

        if check_api:
            async with aiohttp.ClientSession() as session:
                await _run(session)
        else:
            await _run()

    @classmethod
    async def process_list_response(
        cls, resp: aiohttp.ClientResponse
    ) -> Sequence[Self]:
        """Map an HTTP response to model instances.

        Override in subclasses to adapt to non-trivial response shapes
        (e.g., nested keys, pagination envelopes). The default implementation
        expects a JSON array of objects directly parseable into ``cls``.

        Args:
            resp: The successful ``aiohttp.ClientResponse`` object.

        Returns:
            A sequence of instantiated models.
        """
        return [cls(**i) for i in (await resp.json())]

    async def process_detail_response(self, resp: aiohttp.ClientResponse) -> None:
        """Populate this instance from a detail response.

        Override in subclasses to adapt to non-trivial response shapes
        (e.g., nested keys). The default implementation expects a JSON object
        and sets attributes from the top-level keys.

        Args:
            resp: The successful ``aiohttp.ClientResponse`` object.
        """
        for field, value in (await resp.json()).items():
            setattr(self, field, value)

    @overload
    @classmethod
    async def scrape_list(
        cls,
        check_api: Literal[False],
        *,
        use_cache: bool,
        scrape_details: bool = True,
        session: None = None,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> Sequence[Self]: ...

    @overload
    @classmethod
    async def scrape_list(
        cls,
        check_api: Literal[True],
        *,
        use_cache: bool,
        scrape_details: bool = True,
        session: aiohttp.ClientSession,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> Sequence[Self]: ...

    @overload
    @classmethod
    async def scrape_list(
        cls,
        check_api: bool,
        *,
        use_cache: bool,
        scrape_details: bool = True,
        session: aiohttp.ClientSession | None = None,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> Sequence[Self]: ...

    @overload
    @classmethod
    async def scrape_list(
        cls,
        check_api: str,
        *,
        use_cache: bool,
        scrape_details: bool = True,
        session: aiohttp.ClientSession,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> Sequence[Self]: ...

    @classmethod
    async def scrape_list(
        cls,
        check_api: bool | str,
        *,
        use_cache: bool,
        scrape_details: bool = True,
        session: aiohttp.ClientSession | None = None,
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
            session: The ``aiohttp.ClientSession`` to use for HTTP requests. Must be
                provided when ``check_api`` is ``True`` or a string override.
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
        if not session:
            raise ValueError(
                "session must be provided if check_api is True or a string"
            )
        url = cls._build_url(endpoint)
        cls.logger.info("Scraping %s from %s", cls.__name__, url)
        resp = await cls.request(
            id="fetch_all",
            url=url,
            headers={"Accept": "application/json"},
            raise_on_status_except_for=raise_on_status_except_for,
            session=session,
        )
        if resp is None:
            cls.logger.warning(
                "Fetch failed for %s list; returning %d cached item(s)",
                cls.__name__,
                len(existing),
            )
            return list(existing.values())

        models = [
            existing.get(item.cache_key, item)
            for item in await cls.process_list_response(resp)
        ]

        for model in models:
            model.cache()

        if scrape_details:
            await asyncio.gather(
                *(m.scrape_detail(use_cache=use_cache, session=session) for m in models)
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
            async with aiohttp.ClientSession() as session:
                await cls.scrape_list(check_api=True, use_cache=True, session=session)
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

    async def scrape_detail(
        self, *, use_cache: bool = True, session: aiohttp.ClientSession
    ) -> None:
        """Populate remaining fields via cache, field getters, and detail API.

        1) Merge fields from a cached instance when available and allowed.
        2) Invoke any custom scraper methods declared via ``CustomScrapeField``
           for fields still in ``UnscrapedDetailFieldType`` state.
        3) If fields remain and ``detail_endpoint`` is provided, perform a GET
           and update attributes from the JSON response body.
        4) Cache the result.

        Args:
            use_cache: Whether to read/write cache during processing.
            session: The ``aiohttp.ClientSession`` used for any detail request.
        """
        if use_cache and (
            existing := await self.get(cache_key=self.cache_key, not_found_ok=True)
        ):
            for field, val in existing:
                if field in self.unscraped_fields():
                    setattr(self, field, val)
        for field, getter in self.get_custom_scrape_methods().items():
            bound = getter.__get__(self, self.__class__)
            # Decide whether to pass session based on declared parameters
            sig = inspect.signature(getter)
            param_names = [p.name for p in sig.parameters.values()]
            if len(param_names) >= 2 and param_names[1] == "session":
                value = await bound(session)
            else:
                value = await bound()
            setattr(self, field, value)
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
                    session=session,
                )
            )
        ):
            await self.process_detail_response(response)
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
        session: aiohttp.ClientSession,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        raise_on_status_except_for: Sequence[int] | None = None,
    ) -> aiohttp.ClientResponse | None:
        """HTTP request wrapper using ``aiohttp`` with optional suppression.

        Delegates to ``ji_async_http_utils.aiohttp.request`` and returns the
        live ``aiohttp.ClientResponse`` on success (2xx). On non-2xx, if the
        status is in ``raise_on_status_except_for`` the error is recorded via
        ``log_scrape_error`` and ``None`` is returned; otherwise the response
        raises for status.

        Args:
            id: Identifier used in error logs for this request.
            url: Absolute URL to request.
            session: The ``aiohttp.ClientSession`` to issue the request with.
            headers: Optional headers to include.
            params: Optional query parameters to include.
            raise_on_status_except_for: Status codes that should not raise.

        Returns:
            The successful ``aiohttp.ClientResponse`` or ``None`` when suppressed.
        """
        resp = await ji_async_http_utils.aiohttp.request(
            url=url,
            headers=headers or {},
            params=params or {},
            session=session,
        )
        if not 200 <= resp.status <= 299:
            if raise_on_status_except_for and resp.status in raise_on_status_except_for:
                await cls.log_scrape_error(
                    id=id,
                    url=url,
                    status=resp.status,
                    message=(await resp.read()).decode()[:500],
                )
                return None
            resp.raise_for_status()
        return resp
