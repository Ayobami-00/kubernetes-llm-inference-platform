import os
import sys

from platform_management.db.models import WorkerHeartbeat
from platform_management.db.session import session
from platform_management.services.common import now


def main():
    try:
        with session() as db:
            row = db.get(WorkerHeartbeat, os.environ.get("HOSTNAME", "local-worker"))
            healthy = row and (now() - row.observed_at).total_seconds() < 30
    except Exception:
        healthy = False
    sys.exit(0 if healthy else 1)
