"""One semantic-classifier call per sentence, shared by everyone who needs its verdict.

The live-lookup and conversation-close routers keep finished verdicts in a plain dict.  While a call is still
running, a second caller (the turn commit, the reply preparation) used to start an identical cloud call of its own.
``ClassifierCache`` also publishes the call in flight for each key, so a second caller joins it.

The first caller still awaits the resolver itself, in its own task, so cancellation, timeouts and a resolver that
swallows a cancellation behave exactly as before.  Later callers wait on a future the first caller resolves:

* a verdict the resolver returns is cached and handed to everyone who joined;
* an exception is handed to everyone who joined and is not cached, so the next caller asks again;
* if the first caller is cancelled, the joiners are not left with a cancelled call: the first of them asks again
  (and the rest join that call), which is what each of them would have done without sharing;
* a joiner that is cancelled leaves the call alone;
* a call whose caller has been cancelled is never joined, even when its resolver swallows the cancellation and
  keeps running: a newcomer asks for itself, as it did before sharing.

The verdict started at the ASR final has no caller yet, so it runs as a background task that the commit path
joins; ``aclose`` cancels such tasks when the runtime closes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Resolver = Callable[[str], Awaitable[bool]]


@dataclass(slots=True)
class _Flight:
    future: asyncio.Future[bool | None]  # the verdict, or None when the caller running the call was cancelled
    driver: asyncio.Task[bool] | None = None  # the task awaiting the resolver

    def joinable(self) -> bool:
        return self.driver is not None and self.driver.cancelling() == 0


class ClassifierCache(dict[str, bool]):
    """Finished verdicts by normalized text, plus the call in flight for each text."""

    def __init__(self) -> None:
        super().__init__()
        self.inflight: dict[str, _Flight] = {}
        self._background: set[asyncio.Task[bool]] = set()

    def _open(self, key: str, driver: asyncio.Task[bool] | None) -> asyncio.Future[bool | None]:
        future: asyncio.Future[bool | None] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_retrieve)  # a failure nobody joined must not log "never retrieved"
        self.inflight[key] = _Flight(future, driver)
        return future

    async def _drive(
        self,
        key: str,
        future: asyncio.Future[bool | None],
        resolver: Resolver,
        text: str,
    ) -> bool:
        try:
            needed = await resolver(text)
        except asyncio.CancelledError:
            future.set_result(None)  # whoever joined asks again
            raise
        except Exception as exc:
            future.set_exception(exc)
            raise
        else:
            self[key] = needed
            future.set_result(needed)
            return needed
        finally:
            flight = self.inflight.get(key)
            if flight is not None and flight.future is future:
                del self.inflight[key]

    async def run(self, key: str, resolver: Resolver, text: str) -> bool:
        """Publish a call for ``key`` and run it in the calling task."""

        driver = asyncio.current_task()
        assert driver is not None
        return await self._drive(key, self._open(key, driver), resolver, text)

    def start(self, key: str, resolver: Resolver, text: str) -> None:
        """Run the call for ``key`` in the background, unless one is already in flight."""

        flight = self.inflight.get(key)
        if flight is not None and flight.joinable():
            return
        future = self._open(key, None)
        task = asyncio.ensure_future(self._drive(key, future, resolver, text))
        self.inflight[key].driver = task
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_retrieve)

    async def aclose(self) -> None:
        """Cancel the background calls and wait for them, so nothing outlives the runtime."""

        tasks = tuple(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _retrieve[T](future: asyncio.Future[T]) -> None:
    if not future.cancelled():
        future.exception()


async def resolve_shared(
    cache: dict[str, bool],
    key: str,
    resolver: Resolver,
    text: str,
) -> bool:
    """The verdict for ``text``: join the call in flight for ``key``, or make it, caching what it returns."""

    if not isinstance(cache, ClassifierCache):
        needed = await resolver(text)
        cache[key] = needed
        return needed
    while (flight := cache.inflight.get(key)) is not None and flight.joinable():
        verdict = await asyncio.shield(flight.future)
        if verdict is not None:
            return verdict
    return await cache.run(key, resolver, text)


def start_shared(cache: dict[str, bool], key: str, resolver: Resolver, text: str) -> None:
    """Start ``resolver(text)`` in the background when the cache can share it; later callers join it."""

    if isinstance(cache, ClassifierCache):
        cache.start(key, resolver, text)
