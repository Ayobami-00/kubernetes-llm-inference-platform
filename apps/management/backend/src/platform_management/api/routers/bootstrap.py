"""VM callback authenticated by an operation-scoped launch credential, not a session."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from platform_contracts.common import ContractModel
from pydantic import Field, field_validator

from platform_management.api.dependencies import Db
from platform_management.services import bootstrap

router = APIRouter(prefix="/bootstrap", tags=["Host bootstrap"])
bootstrap_auth = HTTPBearer(auto_error=False, scheme_name="BootstrapCredential")


class HostAttestation(ContractModel):
    host_key: str = Field(max_length=256)

    @field_validator("host_key")
    @classmethod
    def public_key_only(cls, value):
        return bootstrap.public_host_key(value)


class HostAttestationAccepted(ContractModel):
    status: Literal["accepted"]
    fingerprint: str


class BootstrapFailure(ContractModel):
    code: Literal["configuration", "ssh_policy", "host_key", "callback"]


class BootstrapFailureRecorded(ContractModel):
    status: Literal["recorded"]


@router.post(
    "/{identity}/failure",
    response_model=BootstrapFailureRecorded,
    operation_id="reportBootstrapFailure",
)
def failure(
    identity: Annotated[str, Path(pattern=r"^op-[a-f0-9]{16,32}$")],
    body: BootstrapFailure,
    db: Db,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bootstrap_auth)],
):
    nonce = authorization.credentials if authorization else ""
    return bootstrap.report_failure(db, identity, nonce, body.code)


@router.post("/{identity}", response_model=HostAttestationAccepted, operation_id="attestHost")
def attest(
    identity: Annotated[str, Path(pattern=r"^op-[a-f0-9]{16,32}$")],
    body: HostAttestation,
    db: Db,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bootstrap_auth)],
):
    nonce = authorization.credentials if authorization else ""
    return bootstrap.accept(db, identity, nonce, body.host_key)
