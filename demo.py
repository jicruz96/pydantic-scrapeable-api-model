from __future__ import annotations

import argparse
import asyncio
from typing import Any

from pydantic_cacheable_model import CacheKey

from pydantic_scrapeable_api_model import ScrapeableApiModel, ScrapeableField


class JSONPlaceholderAPIScraper(ScrapeableApiModel):
    """Intermediate base to demonstrate subclass-of-subclass discovery.

    Child classes inherit its BASE_URL while defining their own endpoints.
    """

    BASE_URL = "https://jsonplaceholder.typicode.com"


class Post(JSONPlaceholderAPIScraper):
    """Posts with an extra computed field fetched via a child endpoint."""

    list_endpoint = "/posts"

    id: CacheKey[int]
    userId: int
    title: str
    body: str
    comments_count: ScrapeableField[int]

    @property
    def detail_endpoint(self) -> str:
        return f"/posts/{self.id}"

    async def scrape_comments_count(self) -> None:
        resp = await self.request(
            id=f"post-{self.id}-comments",
            url=self._build_url(f"/posts/{self.id}/comments"),
            headers={"Accept": "application/json"},
        )
        if resp is None:
            # Mark as known-empty to avoid repeated attempts this run
            self.comments_count = 0
        else:
            data: list[dict[str, Any]] = resp.json()
            self.comments_count = len(data)


class Todo(JSONPlaceholderAPIScraper):
    """Todos with a locally computed derived field to show getter usage."""

    list_endpoint = "/todos"

    id: int
    userId: int
    title: str
    completed: bool
    title_length: ScrapeableField[int]

    @property
    def cache_key(self) -> str:
        return str(self.id)

    async def scrape_title_length(self) -> None:
        # Demonstrates a non-HTTP field getter
        self.title_length = len(self.title)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape demo using ScrapeableApiModel")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not hit APIs; use only cached data if present",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read/write cache; always fetch fresh (if not offline)",
    )
    args = parser.parse_args()

    check_api = not args.offline
    use_cache = not args.no_cache

    # Runs all discovered subclasses (including subclass-of-subclass) concurrently
    await JSONPlaceholderAPIScraper.run(use_cache=use_cache, check_api=check_api)

    posts = Post.load_all_cached()
    todos = Todo.load_all_cached()
    print(f"\nLoaded from cache: posts={len(posts)}, todos={len(todos)}")

    if posts:
        print(f"Example {posts[0]}")
    if todos:
        print(f"Example {todos[0]}")


if __name__ == "__main__":
    asyncio.run(main())
