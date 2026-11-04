import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from platform_management.api.routers import (
    actions,
    auth,
    bootstrap,
    experiments,
    imports,
    specifications,
    topology,
)
from platform_management.api.schemas import ErrorEnvelope
from platform_management.db.session import session
from platform_management.observability import RequestLogging
from platform_management.services.common import AppError


@asynccontextmanager
async def lifespan(application):
    logging.basicConfig(level=logging.INFO)
    auth.password_hash()  # Refuse to serve an application without its configured admin secret.
    yield


app = FastAPI(
    title="Inference Platform Management",
    lifespan=lifespan,
    responses={code: {"model": ErrorEnvelope} for code in (400, 401, 403, 404, 409, 413, 422, 429)},
    version="0.2.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(RequestLogging)
for router in (
    auth.router,
    specifications.router,
    actions.router,
    topology.router,
    imports.router,
    bootstrap.router,
    experiments.router,
):
    app.include_router(router, prefix="/api/v1")


@app.middleware("http")
async def bounded_import(request: Request, call_next):
    from platform_management.settings import get_settings

    is_bootstrap = request.url.path.startswith("/api/v1/bootstrap/")
    if request.method == "POST" and (
        request.url.path.startswith("/api/v1/imports/") or is_bootstrap
    ):
        limit = 4096 if is_bootstrap else get_settings().import_max_bytes
        try:
            advertised = int(request.headers.get("content-length", "0"))
        except ValueError:
            advertised = limit + 1
        if advertised > limit:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "bootstrap_too_large" if is_bootstrap else "import_too_large",
                        "message": "Payload exceeds the size limit",
                        "details": [],
                    }
                },
            )
        # Bound chunked bodies too; never accumulate an unbounded upload.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "bootstrap_too_large" if is_bootstrap else "import_too_large",
                            "message": "Payload exceeds the size limit",
                            "details": [],
                        }
                    },
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)
    return await call_next(request)


@app.exception_handler(AppError)
async def application_error(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
    )


@app.exception_handler(RequestValidationError)
async def request_validation(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": [{"path": list(e["loc"]), "message": e["msg"]} for e in exc.errors()],
            }
        },
    )


@app.exception_handler(ValidationError)
async def domain_validation(request: Request, exc: ValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Configuration validation failed",
                "details": [{"path": list(e["loc"]), "message": e["msg"]} for e in exc.errors()],
            }
        },
    )


@app.exception_handler(ValueError)
async def value_error(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=422, content={"error": {"code": "validation_error", "message": str(exc)}}
    )


@app.exception_handler(IntegrityError)
async def integrity_error(request: Request, exc: IntegrityError):
    logging.getLogger(__name__).warning("Conflicting database write")
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "write_conflict",
                "message": "Conflicting write; reload the resource and retry.",
            }
        },
    )


@app.get("/health/live", include_in_schema=False)
def live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def ready():
    try:
        with session() as db:
            version = db.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "0002":
            return JSONResponse(status_code=503, content={"status": "migration_required"})
    except Exception:
        return JSONResponse(status_code=503, content={"status": "database_unavailable"})
    return {"status": "ok"}
