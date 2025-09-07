# pydantic-scrapeable-api-model

Minimal utilities to build scrapeable API models on top of Pydantic with simple on-disk caching and async HTTP.

Relies on [`pydantic-cacheable-model`](https://github.com/jicruz96/pydantic-cacheable-model) for JSON-based caching.

## Install

```bash
pip install pydantic-scrapeable-api-model
```

## Usage

### Quickstart

```python
from __future__ import annotations

import asyncio
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

    id: CacheKey[int]  # required, unique key (or create a custom `id` property)
    name: str
    # lazily-scraped field (filled by `scrape_detail` or a custom method)
    description: DetailField[str] = CustomScrapeField("fetch_description")

    @property
    def detail_endpoint(self) -> str | None:
        return f"/items/{self.id}"

    # Optional: fetch a field yourself instead of relying on detail_endpoint
    async def fetch_description(self) -> str:
        resp = await self.request(
            id=f"item-{self.id}-desc",
            url=self._build_url(f"/items/{self.id}/description"),
            headers={"Accept": "application/json"},
        )
        if not resp:
            return ""
        return resp.json()["description"]


async def main() -> None:
    # Scrape list + detail for all models of MyAPI
    await MyAPI.scrape_list(check_api=True, use_cache=True)

    # Work with cached data later
    items = list(MyAPI.load_all_cached())
    first = items[0]
    print(first.model_dump())


asyncio.run(main())
```

Fields annotated with `DetailField` begin as placeholders and are populated
only after `scrape_detail` runs (via `.scrape_list()` by default, `.scrape_detail()`, or a
custom getter). Pass `scrape_details=False` to `scrape_list` to skip detail scraping. Use
`CustomScrapeField("method_name")` to register an async method that returns the
field's value during `scrape_detail`. These methods are validated to exist and to
return the same type as the field they populate.

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


# Discover and run all children concurrently
asyncio.run(Base.run(use_cache=True, check_api=True))
```

### Absolute Endpoints (no BASE_URL)

```python
from pydantic_scrapeable_api_model import CacheKey, ScrapeableApiModel

class ExternalFeed(ScrapeableApiModel):
    BASE_URL = ""  # allowed when list_endpoint is absolute
    list_endpoint = "https://example.org/feed.json"
    id: CacheKey[int]
    title: str

# Works because the endpoint is absolute
asyncio.run(ExternalFeed.scrape_list(check_api=True, use_cache=True, scrape_details=False))
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

Override `response_to_models()` to adapt to non-trivial API shapes:

```python
from typing import Sequence
import httpx

class MyWrappedAPI(MyAPI):
    # Server wraps results like: {"data": {"items": [ ... ]}}
    @classmethod
    def response_to_models(cls, resp: httpx.Response) -> Sequence["MyWrappedAPI"]:
        payload = resp.json()
        items = payload.get("data", {}).get("items", [])
        return [cls(**row) for row in items]
```


## API Notes

```python
MyModel.scrape_list(check_api=True|False|"/override", use_cache=True|False, scrape_details=True|False) -> list[MyModel]
MyModel.run(use_cache=True, check_api=True) -> None  # runs all subclasses
MyModel.get(cache_key=..., check_api=False) -> Optional[MyModel]
MyModel.load_all_cached() -> Iterable[MyModel]
instance.scrape_detail(use_cache=True) -> None
instance.model_dump() -> dict (unscraped fields omitted)
MyModel.response_to_models(resp) -> Sequence[MyModel]  # customize parsing
```
