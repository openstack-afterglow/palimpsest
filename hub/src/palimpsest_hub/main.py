from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from palimpsest_hub.api.hub import router as hub_router
from palimpsest_hub.cache import close_redis
from palimpsest_hub.config import get_settings
from palimpsest_hub.database import close_db, init_db
from palimpsest_hub.rate_limit import limiter


class VersionLink(BaseModel):
    href: str
    rel: str = "self"


class VersionDocument(BaseModel):
    id: str = "v1.0"
    status: str = "CURRENT"
    updated: str = "2026-08-01T00:00:00Z"
    links: list[VersionLink]


class RootDiscoveryResponse(BaseModel):
    versions: list[VersionDocument]


class VersionDiscoveryResponse(BaseModel):
    version: VersionDocument


class HealthResponse(BaseModel):
    status: str = "ok"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    init_db(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        connect_timeout=settings.database_connect_timeout,
        pool_timeout=settings.database_pool_timeout,
        unhealthy_seconds=settings.database_unhealthy_seconds,
    )
    try:
        yield
    finally:
        await close_redis()
        await close_db()


app = FastAPI(title="Palimpsest Hub", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.include_router(hub_router, prefix="/v1", tags=["hub"])


def _version_document(request: Request) -> VersionDocument:
    base_url = str(request.base_url).rstrip("/")
    return VersionDocument(
        id="v1.0",
        status="CURRENT",
        updated="2026-08-01T00:00:00Z",
        links=[VersionLink(href=f"{base_url}/v1/", rel="self")],
    )


@app.get("/", response_model=RootDiscoveryResponse, operation_id="get_root_discovery")
async def root_discovery(request: Request) -> RootDiscoveryResponse:
    return RootDiscoveryResponse(versions=[_version_document(request)])


@app.get("/v1/", response_model=VersionDiscoveryResponse, operation_id="get_version_discovery")
async def version_discovery(request: Request) -> VersionDiscoveryResponse:
    return VersionDiscoveryResponse(version=_version_document(request))


@app.get("/v1/health", response_model=HealthResponse, operation_id="get_v1_health")
async def health_v1() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/health", response_model=HealthResponse, include_in_schema=False)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


def run() -> None:
    uvicorn.run("palimpsest_hub.main:app", host="0.0.0.0", port=8020, proxy_headers=True)
