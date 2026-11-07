"""FastAPI app: OpenAI-compatible data plane + admin API + ops console.

Run:  uvicorn prism.app:app --port 8080
"""
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .cache import SemanticCache
from .config import (Settings, load_gateway_config, load_pricing, load_seed_keys)
from .db import Database, current_month
from .gateway import Gateway
from .limits import RateLimiter
from .providers import OpenAICompatProvider
from .upstream import UpstreamExecutor

CONSOLE = Path(__file__).parent / "console" / "index.html"


def create_app(settings=None, providers=None):
    """`providers` (name -> ProviderAdapter) lets tests inject in-process fakes."""
    settings = settings or Settings()
    config = load_gateway_config(settings.data_dir)
    pricing = load_pricing(settings.data_dir)
    db = Database(settings.db_path)
    db.seed_keys(load_seed_keys(settings.data_dir))
    adapters = providers if providers is not None else {
        p.name: OpenAICompatProvider(p.name, p.base_url, p.api_key,
                                     settings.upstream_timeout_s, settings.upstream_connect_timeout_s)
        for p in config.providers}
    gateway = Gateway(settings, db, pricing, config, UpstreamExecutor(adapters, config.retry),
                      SemanticCache(db, settings.cache_scan_limit), RateLimiter())

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for a in adapters.values():
            await a.aclose()

    app = FastAPI(title="Prism LLM Gateway", lifespan=lifespan)
    app.state.gateway, app.state.db = gateway, db

    # ---------------- data plane ----------------
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await gateway.chat(request)

    @app.get("/v1/models")
    async def models():
        names = list(config.aliases) + ["auto"]
        return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "prism"} for n in names]}

    @app.get("/health")
    async def health():
        return {"status": "ok", "providers": sorted(adapters)}

    # ---------------- admin plane (demo token) ----------------
    def admin(x_admin_token, authorization):
        token = x_admin_token or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
        if token != settings.admin_token:
            raise HTTPException(status_code=401, detail="admin token required (X-Admin-Token header)")

    @app.get("/admin/usage")
    async def usage(key: str = None, date_from: str = Query(None, alias="from"), date_to: str = Query(None, alias="to"),
                    x_admin_token: str = Header(None), authorization: str = Header(None)):
        admin(x_admin_token, authorization)
        if key:
            s = db.usage_summary(key, date_from, date_to)
            row = next((k for k in db.list_keys() if k["virtual_key"] == key), None)
            if row:
                s["monthly_budget_usd"] = row["monthly_budget_nano"] / 1e9
                s["spent_this_month_usd"] = db.spent_nano(key) / 1e9
            return s
        out = []
        for k in db.list_keys():
            s = db.usage_summary(k["virtual_key"], date_from, date_to)
            s.update(team=k["team"], monthly_budget_usd=k["monthly_budget_nano"] / 1e9,
                     spent_this_month_usd=db.spent_nano(k["virtual_key"]) / 1e9, month=current_month())
            out.append(s)
        return {"keys": out}

    @app.get("/admin/logs")
    async def logs(key: str = None, limit: int = 50, x_admin_token: str = Header(None),
                   authorization: str = Header(None)):
        admin(x_admin_token, authorization)
        return {"logs": db.recent_logs(key, max(1, min(limit, 500)))}

    @app.get("/admin/cache/stats")
    async def cache_stats(x_admin_token: str = Header(None), authorization: str = Header(None)):
        admin(x_admin_token, authorization)
        return db.cache_stats()

    @app.get("/admin/routing/eval")
    async def routing_eval(x_admin_token: str = Header(None), authorization: str = Header(None)):
        admin(x_admin_token, authorization)
        sys.path.insert(0, str(settings.data_dir.parent / "scripts"))
        from routing_eval import run_eval  # noqa: WPS433
        return run_eval(settings.data_dir / "routing_eval.jsonl")

    # ---------------- console ----------------
    @app.get("/console")
    async def console():
        return FileResponse(CONSOLE, media_type="text/html")

    return app


app = create_app()
