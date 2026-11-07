"""Provider adapter interface. The gateway core only ever talks to this, so mock providers,
real providers or in-process fakes (tests) can be swapped without touching core logic."""
from abc import ABC, abstractmethod


class UpstreamError(Exception):
    """A failed upstream call. `retryable` => worth retrying the same provider (429/5xx/connection).
    `timeout` => the provider was too slow; do not retry, fail over immediately."""

    def __init__(self, message, status=None, retryable=False, timeout=False):
        super().__init__(message)
        self.status, self.retryable, self.timeout = status, retryable, timeout


class StreamHandle(ABC):
    """An open upstream SSE stream. `lines()` yields raw SSE lines as they arrive."""

    @abstractmethod
    def lines(self):  # async iterator of str
        ...

    @abstractmethod
    async def aclose(self):
        ...


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    async def chat(self, model, payload):
        """Non-streaming completion -> OpenAI-shaped dict. Raises UpstreamError."""

    @abstractmethod
    async def open_stream(self, model, payload):
        """Open a streaming completion -> StreamHandle. Raises UpstreamError before the first byte."""

    async def aclose(self):
        pass
