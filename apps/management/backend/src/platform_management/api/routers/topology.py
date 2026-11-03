from fastapi import APIRouter, Depends

from platform_management.api.dependencies import Db, administrator
from platform_management.api.schemas import Topology
from platform_management.db.models import Preference
from platform_management.services.topology import topology

router = APIRouter(dependencies=[Depends(administrator)], tags=["Topology"])


@router.get("/topology", response_model=Topology, operation_id="getTopology")
def current(db: Db, include_history: bool = False):
    return topology(db, include_history=include_history)


@router.get("/runs/{identity}/topology", response_model=Topology, operation_id="getRunTopology")
def historical(identity: str, db: Db):
    return topology(db, run_id=identity)


@router.get("/preferences", response_model=dict, operation_id="getPreferences")
def preferences(db: Db):
    row = db.get(Preference, "admin")
    return row.payload if row else {}


@router.put("/preferences", response_model=dict, operation_id="savePreferences")
def save_preferences(body: dict, db: Db):
    db.merge(Preference(id="admin", payload=body))
    return body
