import logging
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("management.requests")


class RequestLogging(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = uuid4().hex
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "%s %s status=%s duration_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - start) * 1000,
            request_id,
        )
        return response
