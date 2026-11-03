import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from platform_management.api.routers import actions, auth, specifications, topology
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
    responses={code: {"model": ErrorEnvelope} for code in (400, 401, 403, 404, 409, 422, 429)},
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(RequestLogging)
for router in (auth.router, specifications.router, actions.router, topology.router):
    app.include_router(router, prefix="/api/v1")


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
    with session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}
