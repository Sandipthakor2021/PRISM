"""Adapter for any OpenAI-compatible HTTP provider (the mock providers, Ollama, real vendors).
The provider api_key is attached here, server-side only; it never reaches callers or logs."""
import httpx

from .base import ProviderAdapter, StreamHandle, UpstreamError


def _classify_status(status):
    return status == 429 or status >= 500


class _HttpxStream(StreamHandle):
    def __init__(self, response):
        self._r = response

    def lines(self):
        return self._r.aiter_lines()  # yields each line as soon as it is received: no buffering

    async def aclose(self):
        await self._r.aclose()


class OpenAICompatProvider(ProviderAdapter):
    def __init__(self, name, base_url, api_key, timeout_s=2.5, connect_timeout_s=1.0):
        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # timeout applies to connect and to every wait for upstream bytes, so a dead provider cannot hang us
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s))

    @staticmethod
    def _wrap(exc):
        if isinstance(exc, httpx.TimeoutException):
            return UpstreamError("upstream timed out", timeout=True)
        return UpstreamError(f"upstream connection failed: {type(exc).__name__}", retryable=True)

    async def chat(self, model, payload):
        body = {**payload, "model": model, "stream": False}
        try:
            r = await self._client.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise self._wrap(exc)
        if r.status_code != 200:
            raise UpstreamError(f"upstream returned {r.status_code}", r.status_code, _classify_status(r.status_code))
        try:
            return r.json()
        except ValueError:
            raise UpstreamError("upstream returned invalid JSON", r.status_code, retryable=True)

    async def open_stream(self, model, payload):
        body = {**payload, "model": model, "stream": True, "stream_options": {"include_usage": True}}
        try:
            req = self._client.build_request("POST", self._url, json=body, headers=self._headers)
            r = await self._client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise self._wrap(exc)
        if r.status_code != 200:
            await r.aread()
            await r.aclose()
            raise UpstreamError(f"upstream returned {r.status_code}", r.status_code, _classify_status(r.status_code))
        return _HttpxStream(r)

    async def aclose(self):
        await self._client.aclose()
