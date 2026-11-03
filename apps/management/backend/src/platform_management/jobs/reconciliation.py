from datetime import timedelta

import httpx
from sqlalchemy import select, text

from platform_management.api.schemas import Capacity
from platform_management.db.models import Observation, Resource
from platform_management.db.session import session
from platform_management.jobs.queue import submit
from platform_management.providers.lambda_cloud import LambdaCloud
from platform_management.services.common import lock_key, now
from platform_management.settings import get_settings


def refresh_capacity(force=False):
    # A session-level lock coordinates API refresh and every worker, without an open transaction.
    from platform_management.db.session import engine

    with engine().connect().execution_options(isolation_level="AUTOCOMMIT") as guard:
        key = lock_key("lambda-observations")
        if not guard.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}):
            return
        try:
            with session() as db:
                old = db.get(Observation, "lambda")
                if old and old.next_refresh > now() and not force:
                    return
                previous = old.payload if old else {}
            stamp = now()
            api_key = get_settings().secret(get_settings().lambda_api_key_file)
            if not api_key:
                payload = Capacity().model_dump(mode="json")
            else:
                try:
                    values = LambdaCloud(api_key).observe()
                    payload = Capacity(
                        status="fresh", observed_at=stamp, attempted_at=stamp, **values
                    ).model_dump(mode="json")
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    # Persist safe error metadata only; omit bodies and authentication headers.
                    payload = {
                        **Capacity().model_dump(mode="json"),
                        **previous,
                        "status": "stale" if previous.get("observed_at") else "error",
                        "attempted_at": stamp.isoformat(),
                        "error": f"Lambda refresh failed: {code or type(exc).__name__}",
                    }
            with session() as db, db.begin():
                db.merge(
                    Observation(
                        id="lambda", payload=payload, next_refresh=stamp + timedelta(seconds=30)
                    )
                )
        finally:
            guard.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


def reconcile_expiry():
    with session() as db, db.begin():
        for row in db.scalars(
            select(Resource)
            .where(Resource.kind == "worker_group")
            .with_for_update(skip_locked=True)
        ):
            if row.payload.get("ownership") == "external":
                continue  # Expiry blocks readiness; it never implies provider termination.
            group = row.payload["group"]
            status = group["status"]
            if status["phase"] in {"expired", "deleted"} or row.active_operation_id:
                continue
            expiry = status.get("expires_at")
            from datetime import datetime

            if expiry and datetime.fromisoformat(expiry) <= now():
                key = f"expiry:{row.id}:{expiry}"

                def mutate(op_id, resource=row):
                    resource.active_operation_id = op_id

                submit(db, key, "delete_worker_group", row.id, {"expiry": True}, mutate)
