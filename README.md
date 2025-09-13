# pydantic-scrapeable-api-model

Minimal utilities to build scrapeable API models on top of Pydantic with simple on-disk caching and async HTTP.

Relies on [`pydantic-cacheable-model`](https://github.com/jicruz96/pydantic-cacheable-model) for JSON-based caching.

## Install

```bash
pip install pydantic-scrapeable-api-model
```

## Usage

This library now uses `aiohttp` for HTTP and requires an `aiohttp.ClientSession` to be provided when making requests. The recommended entrypoint for multi-model scrapes is `run()`, which manages a single session for you. When calling `scrape_list()` or `scrape_detail()` directly, pass a session explicitly.

### Quickstart

```python
from __future__ import annotations

import asyncio
import aiohttp
from typing import Any

from pydantic_scrapeable_api_model import (
    CacheKey,
    ScrapeableApiModel,
    DetailField,
    CustomScrapeField,
)


class MyAPI(ScrapeableApiModel):
    BASE_URL = "https://api.example.com"
    list_endpoint = "/items"

    id: CacheKey[int]  # required, unique key (or create a custom `cache_key`)
    name: str
    # lazily-scraped field (filled by `scrape_detail` or a custom method)
    description: DetailField[str] = CustomScrapeField("fetch_description")

    @property
    def detail_endpoint(self) -> str | None:
        return f"/items/{self.id}"

    # Optional: fetch a field yourself instead of relying on detail_endpoint.
    # Custom getters do not receive a shared session; create a short-lived one here
    # if you need to make HTTP calls.
    async def fetch_description(self) -> str:
        async with aiohttp.ClientSession() as session:
            resp = await self.request(
                id=f"item-{self.id}-desc",
                url=self._build_url(f"/items/{self.id}/description"),
                headers={"Accept": "application/json"},
                session=session,
            )
            if resp is None:
                return ""
            data: dict[str, Any] = await resp.json()
        return data.get("description", "")


async def main() -> None:
    # Manual call (single model): pass a session explicitly
    async with aiohttp.ClientSession() as session:
        await MyAPI.scrape_list(check_api=True, use_cache=True, session=session)

    # Work with cached data later
    items = list(MyAPI.load_all_cached())
    first = items[0]
    print(first.model_dump())


asyncio.run(main())
```

Fields annotated with `DetailField` begin as placeholders and are populated only after `scrape_detail` runs (triggered by `.scrape_list(scrape_details=True, ...)` by default, or by calling `.scrape_detail(..., session=...)`, or via a custom getter). Pass `scrape_details=False` to `scrape_list` to skip detail scraping. Use `CustomScrapeField("method_name")` to register an async method that returns the field's value during `scrape_detail`. These methods are validated to exist and to return the same type as the field they populate.

To scrape several models at once and have the library manage one shared session for you, define a common base class and call `Base.run(...)` as shown below.

### Run All Subclasses

```python
from pydantic_scrapeable_api_model import CacheKey, ScrapeableApiModel

# Define a base that sets the host
class Base(ScrapeableApiModel):
    BASE_URL = "https://api.example.com"


class Users(Base):
    list_endpoint = "/users"
    id: CacheKey[int]
    username: str


class Posts(Base):
    list_endpoint = "/posts"
    id: CacheKey[int]
    title: str


# Discover and run all children concurrently; manages one ClientSession for you
asyncio.run(Base.run(use_cache=True, check_api=True))
```

### Absolute Endpoints

Absolute `list_endpoint` or `detail_endpoint` values are supported and used as-is. A non-empty `BASE_URL` is still required by the base class contract, but it is ignored when the endpoint is absolute.

```python
from pydantic_scrapeable_api_model import CacheKey, ScrapeableApiModel
import aiohttp

class ExternalFeed(ScrapeableApiModel):
    BASE_URL = "https://example.com"  # required, but not used for absolute endpoints
    list_endpoint = "https://example.org/feed.json"  # absolute
    id: CacheKey[int]
    title: str

async def main() -> None:
    async with aiohttp.ClientSession() as session:
        await ExternalFeed.scrape_list(
            check_api=True, use_cache=True, scrape_details=False, session=session
        )

asyncio.run(main())
```

### Cached Access Helpers

```python
# Load everything from cache
cached_items = list(MyAPI.load_all_cached())

# Fetch one by key (fallback to API when allowed)
item = asyncio.run(MyAPI.get(cache_key="123", check_api=True))
```

### Configure Logging

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

# Library logs under module name
logging.getLogger("pydantic_scrapeable_api_model").setLevel(logging.INFO)
```


### Custom Response Mapping

Override `process_list_response()` or `process_detail_response()` to adapt to non-trivial API shapes:

```python
from typing import Sequence
import aiohttp

class MyWrappedAPI(MyAPI):
    # Server wraps results like: {"data": {"items": [ ... ]}}
    @classmethod
    async def process_list_response(
        cls, resp: aiohttp.ClientResponse
    ) -> Sequence["MyWrappedAPI"]:
        payload = await resp.json()
        items = payload.get("data", {}).get("items", [])
        return [cls(**row) for row in items]

    # Or customize how a detail response populates fields
    async def process_detail_response(self, resp: aiohttp.ClientResponse) -> None:
        data = await resp.json()
        # Only set what you need; unknown keys are ignored
        self.title = data["title"]
```


## API Notes

```python
MyModel.scrape_list(
    check_api=True|False|"/override",
    use_cache=True|False,
    scrape_details=True|False,
    session=<aiohttp.ClientSession>,
) -> list[MyModel]

MyModel.run(use_cache=True, check_api=True) -> None  # runs all subclasses, manages session
MyModel.get(cache_key=..., check_api=False) -> Optional[MyModel]  # creates a session if needed
MyModel.load_all_cached() -> Iterable[MyModel]
instance.scrape_detail(use_cache=True, session=<aiohttp.ClientSession>) -> None
instance.model_dump() -> dict  # unscraped fields omitted
MyModel.process_list_response(resp: aiohttp.ClientResponse) -> Sequence[MyModel]
instance.process_detail_response(resp: aiohttp.ClientResponse) -> None
```
