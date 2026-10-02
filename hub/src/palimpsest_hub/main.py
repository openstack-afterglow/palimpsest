from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from palimpsest_hub.api.builds import router as build_router
from palimpsest_hub.api.hub import configure_blocking_operations
from palimpsest_hub.api.hub import router as hub_router
from palimpsest_hub.api.packages import router as package_router
from palimpsest_hub.cache import close_redis
from palimpsest_hub.config import get_settings
from palimpsest_hub.database import close_db, init_db
from palimpsest_hub.logging import configure_logging
from palimpsest_hub.rate_limit import limiter

logger = logging.getLogger(__name__)
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


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
    configure_logging()
    settings = get_settings()
    configure_blocking_operations(settings.palimpsest_hub_max_blocking_operations)
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
app.add_middleware(SlowAPIMiddleware)
app.include_router(hub_router, prefix="/v1", tags=["hub"])
app.include_router(build_router, prefix="/v1", tags=["builds"])
app.include_router(package_router, prefix="/v1", tags=["packages"])


def _package_path(request: Request) -> bool:
    path = request.scope.get("path", "")
    return path == "/v1/auth/me" or path == "/v1/projects" or path.startswith("/v1/projects/")


def _package_error(status: int, code: str, message: str, headers=None, *, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id or uuid4().hex}},
        status_code=status,
        headers={**(headers or {}), "Cache-Control": "no-store"},
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    if not _package_path(request):
        return await http_exception_handler(request, exc)
    fallback = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 412: "TAG_CONFLICT", 413: "LIMIT_EXCEEDED", 422: "INVALID_REQUEST", 429: "RATE_LIMITED", 503: "IDENTITY_UNAVAILABLE"}
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", fallback.get(exc.status_code, "REQUEST_FAILED"))
        message = detail.get("message", "Package request failed")
    else:
        code, message = fallback.get(exc.status_code, "REQUEST_FAILED"), str(detail)
    return _package_error(exc.status_code, code, message, exc.headers, request_id=request.state.request_id)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    if not _package_path(request):
        return await request_validation_exception_handler(request, exc)
    # Validation inputs can contain credentials/provenance; never echo them.
    return _package_error(422, "INVALID_REQUEST", "Package request validation failed", request_id=request.state.request_id)


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit(request: Request, exc: RateLimitExceeded):
    response = _rate_limit_exceeded_handler(request, exc)
    if not _package_path(request):
        return response
    headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-type", "content-length"}}
    return _package_error(429, "RATE_LIMITED", "Package request rate limit exceeded", headers, request_id=request.state.request_id)


@app.middleware("http")
async def log_request(request: Request, call_next):
    request.state.request_id = uuid4().hex
    start = perf_counter()
    method = request.method if request.method in _METHODS else "OTHER"
    try:
        response = await call_next(request)
    except Exception:
        # Never expose exception text or traceback (SQL binds, paths or remote
        # credentials). Legacy routes keep plain text; package routes get an envelope.
        response = (
            _package_error(500, "INTERNAL_ERROR", "Package request failed", request_id=request.state.request_id)
            if _package_path(request)
            else PlainTextResponse("Internal Server Error", status_code=500)
        )
    if _package_path(request):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-Id"] = request.state.request_id
    route = request.scope.get("route")
    template = getattr(route, "path", "<unmatched>")
    logger.info(
        "hub request method=%s route=%s status=%d duration_ms=%.1f",
        method, template, response.status_code, (perf_counter() - start) * 1000,
    )
    if logger.isEnabledFor(logging.DEBUG):
        query = request.scope.get("query_string", b"")
        logger.debug("hub request query_present=%s query_bytes_bounded=%d", bool(query), min(len(query), 4096))
    return response

_APP_FILES = Path(__file__).parent / "static"
_APP_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@app.get("/app", include_in_schema=False)
async def hub_console() -> FileResponse:
    return FileResponse(_APP_FILES / "hub.html", media_type="text/html", headers=_APP_HEADERS)


@app.get("/app/{asset}", include_in_schema=False)
async def hub_console_asset(asset: str) -> FileResponse:
    if asset not in {"hub.css", "hub.js"}:
        raise HTTPException(status_code=404)
    return FileResponse(_APP_FILES / asset, headers=_APP_HEADERS)


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
    configure_logging()
    uvicorn.run("palimpsest_hub.main:app", host="0.0.0.0", port=8020, proxy_headers=True, access_log=False)
