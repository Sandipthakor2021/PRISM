"""Retries with exponential backoff and ordered provider failover.

For a request we walk a chain of targets [(provider, model), ...] (primary first, then fallbacks):
  * retryable errors (429, 5xx, connection errors) are retried on the SAME target with
    exponential backoff, up to retry.max_attempts attempts;
  * a timeout is NOT retried (retrying a slow provider multiplies the client's wait): fail over now;
  * a non-retryable error (e.g. upstream 4xx) skips straight to the next target;
  * if every target fails -> AllProvidersFailed (the gateway answers 502).
The result records how many retries happened and whether a fallback served the request.
"""
import asyncio
import time
from dataclasses import dataclass

from .providers.base import UpstreamError


class AllProvidersFailed(Exception):
    def __init__(self, retries, last_error):
        super().__init__(last_error)
        self.retries, self.last_error = retries, last_error


@dataclass
class UpstreamResult:
    provider: str
    model: str
    fallback: bool
    retries: int
    upstream_ms: int
    body: dict = None          # non-streaming
    stream: object = None      # StreamHandle when streaming


class UpstreamExecutor:
    def __init__(self, providers, retry, sleep=asyncio.sleep):
        self.providers = providers  # name -> ProviderAdapter
        self.retry = retry
        self._sleep = sleep

    async def execute(self, targets, payload, stream=False):
        retries, last = 0, "no targets"
        for idx, (provider_name, model) in enumerate(targets):
            adapter = self.providers.get(provider_name)
            if adapter is None:
                last = f"provider '{provider_name}' is not registered"
                continue
            for attempt in range(self.retry.max_attempts):
                started = time.monotonic()
                try:
                    if stream:
                        handle = await adapter.open_stream(model, payload)
                        return UpstreamResult(provider_name, model, idx > 0, retries,
                                              round((time.monotonic() - started) * 1000), stream=handle)
                    body = await adapter.chat(model, payload)
                    return UpstreamResult(provider_name, model, idx > 0, retries,
                                          round((time.monotonic() - started) * 1000), body=body)
                except UpstreamError as exc:
                    last = f"{provider_name}/{model}: {exc}"
                    if exc.timeout or not exc.retryable:
                        break  # fail over immediately
                    if attempt + 1 < self.retry.max_attempts:
                        retries += 1
                        await self._sleep(self.retry.initial_backoff_ms / 1000
                                          * self.retry.backoff_multiplier ** attempt)
        raise AllProvidersFailed(retries, last)
