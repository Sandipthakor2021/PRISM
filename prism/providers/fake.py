"""In-process fake provider for automated tests (no network)."""
from .base import ProviderAdapter, StreamHandle, UpstreamError


class _FakeStream(StreamHandle):
    def __init__(self, lines):
        self._lines = lines

    async def lines(self):  # noqa: D401  (async generator)
        for line in self._lines:
            yield line

    async def aclose(self):
        pass


class _DyingStream(StreamHandle):
    """Yields a few chunks, then the connection 'breaks' (simulates a provider dying mid-stream)."""

    async def lines(self):
        yield 'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"partial "},"finish_reason":null}]}'
        yield ""
        raise RuntimeError("connection reset")

    async def aclose(self):
        pass


class FakeProvider(ProviderAdapter):
    """modes: ok | down | rate_limited | slow | flaky(fail first N calls)."""

    def __init__(self, name, mode="ok", fail_first=0):
        self.name, self.mode, self.fail_first = name, mode, fail_first
        self.calls = 0

    def _maybe_fail(self):
        self.calls += 1
        if self.mode == "down":
            raise UpstreamError("down", 503, retryable=True)
        if self.mode == "rate_limited":
            raise UpstreamError("429", 429, retryable=True)
        if self.mode == "slow":
            raise UpstreamError("timeout", timeout=True)
        if self.calls <= self.fail_first:
            raise UpstreamError("flaky", 500, retryable=True)

    def _reply(self, model, payload):
        last = next((m["content"] for m in reversed(payload["messages"]) if m["role"] == "user"), "")
        return f"[{self.name}:{model}] answer to: {last}"

    async def chat(self, model, payload):
        self._maybe_fail()
        text = self._reply(model, payload)
        return {"id": "chatcmpl-fake", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": len(text.split()), "total_tokens": 10 + len(text.split())}}

    async def open_stream(self, model, payload):
        if self.mode == "stream_dies":
            self.calls += 1
            return _DyingStream()
        self._maybe_fail()
        import json
        text = self._reply(model, payload)
        words = text.split(" ")

        def chunk(delta, finish=None, usage=None):
            d = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage:
                d["usage"] = usage
            return f"data: {json.dumps(d)}"

        lines = [chunk({"role": "assistant", "content": ""}), ""]
        for w in words:
            lines += [chunk({"content": w + " "}), ""]
        lines += [chunk({}, "stop", {"prompt_tokens": 10, "completion_tokens": len(words), "total_tokens": 10 + len(words)}),
                  "", "data: [DONE]", ""]
        return _FakeStream(lines)
