"""Request pipeline for POST /v1/chat/completions.

Order of checks (documented in the README), cheapest first, the expensive upstream call last:
  1. authenticate virtual key         -> 401 authentication_error
  2. validate body                    -> 400 invalid_request_error
  3. known model/alias?               -> 404 not_found_error
  4. model on the key's allowlist?    -> 403 model_not_allowed
  5. requests-per-minute (atomic)     -> 429 rate_limit_exceeded
  6. monthly budget remaining?        -> 402 budget_exceeded
  7. `auto` -> classify difficulty, pick fast/smart, record the reason
  8. semantic cache (per key)         -> hit: serve for $0
  9. upstream call with timeout, retries, failover -> 502 upstream_error if every provider fails
 10. meter cost from provider-reported usage, write the request log, fill the cache
Every request, including every rejection, writes one request-log row.
"""
import json
import time
import uuid

from fastapi.responses import JSONResponse, StreamingResponse

from . import classifier
from .config import provider_for_model
from .metering import compute_cost_nano, format_usd
from .upstream import AllProvidersFailed


def _err(status, etype, message, extra_headers=None):
    headers = {"x-prism-provider": "none", "x-prism-cache": "miss",
               "x-prism-fallback": "false", "x-prism-cost-usd": "0.000000000"}
    headers.update(extra_headers or {})
    return JSONResponse({"error": {"message": message, "type": etype, "code": etype}},
                        status_code=status, headers=headers)


def _fmt_budget(nano):
    usd = nano / 1e9
    return f"${usd:,.2f}" if usd >= 0.01 else f"${usd:.6f}"


def _estimate_tokens(text):
    return max(1, len(text.split()))


class Gateway:
    def __init__(self, settings, db, pricing, config, executor, cache, limiter):
        self.settings, self.db, self.pricing, self.config = settings, db, pricing, config
        self.executor, self.cache, self.limiter = executor, cache, limiter

    # ---- model resolution ---------------------------------------------
    def is_known_model(self, name):
        return name == "auto" or name in self.config.aliases or name in self.pricing

    def targets_for(self, name):
        """alias -> ordered chain [(provider, model)]; raw model -> single target."""
        if name in self.config.aliases:
            a = self.config.aliases[name]
            models = [a.primary] + list(a.fallbacks)
        else:
            models = [name]
        return [(provider_for_model(m, self.config.providers), m) for m in models]

    # ---- helpers -------------------------------------------------------
    def _reject(self, request_id, key, requested, status_code, etype, log_status, message, t0, extra=None):
        self.db.log_request(request_id=request_id, virtual_key=key, requested_model=requested,
                            status=log_status, latency_ms=round((time.monotonic() - t0) * 1000), error=etype)
        return _err(status_code, etype, message, extra)

    # ---- main entry ------------------------------------------------------
    async def chat(self, request):
        t0 = time.monotonic()
        request_id = "req_" + uuid.uuid4().hex[:16]

        # 1. authenticate
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        tenant = self.db.get_key(key) if key else None
        if tenant is None:
            return self._reject(request_id, None, None, 401, "authentication_error", "rejected_auth",
                                "Missing or invalid virtual key", t0)
        vkey = tenant["virtual_key"]

        # 2. validate body
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return self._reject(request_id, vkey, None, 400, "invalid_request_error", "rejected_invalid",
                                "Request body must be a JSON object", t0)
        requested = body.get("model")
        messages = body.get("messages")
        if not isinstance(requested, str) or not requested:
            return self._reject(request_id, vkey, None, 400, "invalid_request_error", "rejected_invalid",
                                "'model' is required", t0)
        if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
            return self._reject(request_id, vkey, requested, 400, "invalid_request_error", "rejected_invalid",
                                "'messages' must be a non-empty list", t0)
        stream = bool(body.get("stream"))

        # 3. unknown model / alias
        if not self.is_known_model(requested):
            return self._reject(request_id, vkey, requested, 404, "not_found_error", "rejected_unknown_model",
                                f"Unknown model or alias '{requested}'", t0)
        # 4. allowlist
        if requested not in tenant["model_allowlist"]:
            return self._reject(request_id, vkey, requested, 403, "model_not_allowed", "rejected_allowlist",
                                f"Model '{requested}' is not allowed for this key. Allowed: "
                                f"{', '.join(tenant['model_allowlist'])}", t0)
        # 5. rate limit (atomic check-and-record)
        ok, retry_after = self.limiter.try_acquire(vkey, tenant["rpm"])
        if not ok:
            return self._reject(request_id, vkey, requested, 429, "rate_limit_exceeded", "rejected_rate_limit",
                                f"Rate limit of {tenant['rpm']} requests/minute exceeded", t0,
                                {"retry-after": str(retry_after)})
        # 6. budget
        if self.db.spent_nano(vkey) >= tenant["monthly_budget_nano"]:
            return self._reject(request_id, vkey, requested, 402, "budget_exceeded", "rejected_budget",
                                f"Monthly budget of {_fmt_budget(tenant['monthly_budget_nano'])} exhausted for this key", t0)

        # 7. auto routing
        route_reason, scope_model = None, requested
        last_user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
        if requested == "auto":
            tier, _score, why = classifier.classify(last_user[:8000])
            scope_model = self.config.auto_tiers["complex" if tier == "smart" else "simple"]
            route_reason = f"auto -> {scope_model}: {why}"

        base = dict(request_id=request_id, virtual_key=vkey, requested_model=requested,
                    route_reason=route_reason, stream=1 if stream else 0)

        # 8. semantic cache
        if tenant["cache_enabled"]:
            hit = self.cache.lookup(vkey, scope_model, messages, tenant["cache_threshold"] or 0.9)
            if hit:
                cached, similarity = hit
                self.db.add_usage(vkey, 0, 0, 0, cache_hit=True)
                self.db.log_request(**base, status="cache_hit", cache="hit", resolved_provider=None,
                                    resolved_model=None, latency_ms=round((time.monotonic() - t0) * 1000))
                headers = {"x-prism-provider": "cache", "x-prism-cache": "hit", "x-prism-fallback": "false",
                           "x-prism-cost-usd": "0.000000000", "x-prism-cache-similarity": f"{similarity:.3f}"}
                return self._serve_cached(cached, stream, headers)

        # 9. upstream
        targets = self.targets_for(scope_model)
        try:
            res = await self.executor.execute(targets, {k: v for k, v in body.items() if k != "model"}, stream=stream)
        except AllProvidersFailed as exc:
            self.db.log_request(**base, status="upstream_error", retries=exc.retries,
                                latency_ms=round((time.monotonic() - t0) * 1000), error=str(exc.last_error)[:300])
            return _err(502, "upstream_error", "All upstream providers failed")

        headers = {"x-prism-provider": f"{res.provider}/{res.model}", "x-prism-cache": "miss",
                   "x-prism-fallback": "true" if res.fallback else "false"}
        if stream:
            return StreamingResponse(self._relay_stream(res, tenant, scope_model, messages, base, t0),
                                     media_type="text/event-stream",
                                     headers={**headers, "cache-control": "no-cache", "x-accel-buffering": "no"})

        # 10. meter + log + fill cache (non-streaming)
        usage = res.body.get("usage") or {}
        pt = usage.get("prompt_tokens") or sum(_estimate_tokens(str(m.get("content", ""))) for m in messages)
        content = (res.body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        ct = usage.get("completion_tokens") or _estimate_tokens(content)
        cost = compute_cost_nano(self.pricing, res.model, pt, ct)
        self.db.add_usage(vkey, pt, ct, cost)
        self.db.log_request(**base, status="ok", resolved_provider=res.provider, resolved_model=res.model,
                            prompt_tokens=pt, completion_tokens=ct, cost_nano=cost, cache="miss",
                            fallback=1 if res.fallback else 0, retries=res.retries, upstream_ms=res.upstream_ms,
                            latency_ms=round((time.monotonic() - t0) * 1000))
        if tenant["cache_enabled"]:
            self.cache.store(vkey, scope_model, messages, res.body)
        headers["x-prism-cost-usd"] = format_usd(cost)
        return JSONResponse(res.body, headers=headers)

    # ---- cached response serving ------------------------------------------
    @staticmethod
    def _serve_cached(cached, stream, headers):
        body = {**cached, "id": "chatcmpl-cache-" + uuid.uuid4().hex[:12]}
        if not stream:
            return JSONResponse(body, headers=headers)
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")

        def chunk(delta, finish=None):
            return "data: " + json.dumps({"id": body["id"], "object": "chat.completion.chunk",
                                          "model": body.get("model"),
                                          "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

        async def replay():  # documented behaviour: a cache hit is replayed as a single content chunk
            yield chunk({"role": "assistant", "content": ""})
            yield chunk({"content": text})
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(replay(), media_type="text/event-stream", headers=headers)

    # ---- streaming pass-through --------------------------------------------
    async def _relay_stream(self, res, tenant, scope_model, messages, base, t0):
        """Forward every upstream SSE line the moment it arrives (never assemble the whole answer).
        We only *observe* lines to capture usage and content for metering/cache.
        Mid-stream failure policy: emit an SSE error event then [DONE]; never splice providers."""
        vkey = tenant["virtual_key"]
        pt = ct = 0
        parts, done, error = [], False, None
        try:
            async for line in res.stream.lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                    else:
                        try:
                            obj = json.loads(data)
                            if obj.get("usage"):
                                pt = obj["usage"].get("prompt_tokens", pt)
                                ct = obj["usage"].get("completion_tokens", ct)
                            for ch in obj.get("choices") or []:
                                piece = (ch.get("delta") or {}).get("content")
                                if piece:
                                    parts.append(piece)
                        except ValueError:
                            pass
                yield line + "\n"
            if not done:
                error = "upstream stream ended before [DONE]"
        except Exception as exc:  # upstream died mid-stream (timeout/reset)
            error = f"upstream stream failed: {type(exc).__name__}"
        finally:
            try:
                await res.stream.aclose()
            except Exception:
                pass
            text = "".join(parts)
            if not pt:
                pt = sum(_estimate_tokens(str(m.get("content", ""))) for m in messages)
            if not ct and text:
                ct = _estimate_tokens(text)
            cost = compute_cost_nano(self.pricing, res.model, pt, ct)
            status = "ok" if (done and not error) else "stream_error"
            self.db.add_usage(vkey, pt, ct, cost)
            self.db.log_request(**base, status=status, resolved_provider=res.provider, resolved_model=res.model,
                                prompt_tokens=pt, completion_tokens=ct, cost_nano=cost, cache="miss",
                                fallback=1 if res.fallback else 0, retries=res.retries, upstream_ms=res.upstream_ms,
                                latency_ms=round((time.monotonic() - t0) * 1000), error=error)
            if status == "ok" and tenant["cache_enabled"] and text:
                self.cache.store(vkey, scope_model, messages, {
                    "id": "chatcmpl-stream", "object": "chat.completion", "model": res.model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}})
        if error and not done:
            yield "data: " + json.dumps({"error": {"message": error, "type": "upstream_error", "code": "upstream_error"}}) + "\n\n"
            yield "data: [DONE]\n\n"
