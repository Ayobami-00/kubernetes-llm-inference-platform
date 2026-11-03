"""Create the IAM access bootstrap only; preserve matching resources and reject drift."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from aws_access import IAM_PATH, PROJECT, ROLE_NAME, build_documents


def aws(profile: str, region: str, *arguments: str, absent_ok: bool = False) -> dict | None:
    result = subprocess.run(
        [
            "aws",
            *arguments,
            "--profile",
            profile,
            "--region",
            region,
            "--no-cli-pager",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if absent_ok and "(NoSuchEntity)" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--source-user", default=PROJECT)
    parser.add_argument("--documents-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="explicitly permit IAM creation")
    args = parser.parse_args()
    documents = build_documents(args.account_id, args.region, args.source_user)
    paths = {}
    for name, expected in documents.items():
        path = (args.documents_dir / f"{name}.json").resolve()
        if json.loads(path.read_text()) != expected:
            raise ValueError(f"{path} differs from the rendered policy; refusing to proceed")
        paths[name] = f"file://{path}"
    identity = aws(args.profile, args.region, "sts", "get-caller-identity")
    source_arn = f"arn:aws:iam::{args.account_id}:user/{args.source_user}"
    if identity["Account"] != args.account_id or identity["Arn"] != source_arn:
        raise ValueError("AWS identity does not match the expected bootstrap account and user")
    if not args.apply:
        print("Identity and documents verified. No changes made; --apply permits IAM creation.")
        return

    owned_role = aws(
        args.profile, args.region, "iam", "get-role", "--role-name", ROLE_NAME, absent_ok=True
    )
    if owned_role and any(
        tag["Key"] == "ManagedBy" and tag["Value"] == "terraform-state-bootstrap"
        for tag in owned_role["Role"].get("Tags", [])
    ):
        raise ValueError("Access is Terraform-owned; use the state-bootstrap plan/apply workflow")

    tags = [f"Key=Project,Value={PROJECT}", "Key=ManagedBy,Value=access-bootstrap"]
    attachments = []
    for document in ("network", "compute", "api", "identity-state", "node-boundary"):
        name = (
            "kubernetes-llm-control-plane-boundary"
            if document == "node-boundary"
            else f"kubernetes-llm-foundation-{document}"
        )
        arn = f"arn:aws:iam::{args.account_id}:policy{IAM_PATH}{name}"
        current = aws(
            args.profile, args.region, "iam", "get-policy", "--policy-arn", arn, absent_ok=True
        )
        if current:
            value = aws(
                args.profile,
                args.region,
                "iam",
                "get-policy-version",
                "--policy-arn",
                arn,
                "--version-id",
                current["Policy"]["DefaultVersionId"],
            )["PolicyVersion"]["Document"]
            if value != documents[document]:
                raise ValueError(f"Existing policy differs: {arn}; review an explicit update")
        else:
            aws(
                args.profile,
                args.region,
                "iam",
                "create-policy",
                "--policy-name",
                name,
                "--path",
                IAM_PATH,
                "--policy-document",
                paths[document],
                "--tags",
                *tags,
            )
        if document != "node-boundary":
            attachments.append(arn)

    current = aws(
        args.profile,
        args.region,
        "iam",
        "get-role",
        "--role-name",
        ROLE_NAME,
        absent_ok=True,
    )
    if current:
        role = current["Role"]
        if role["Path"] != IAM_PATH or role["AssumeRolePolicyDocument"] != documents["trust"]:
            raise ValueError("Existing deployment role differs; review an explicit update")
    else:
        aws(
            args.profile,
            args.region,
            "iam",
            "create-role",
            "--role-name",
            ROLE_NAME,
            "--path",
            IAM_PATH,
            "--assume-role-policy-document",
            paths["trust"],
            "--description",
            "MFA-protected, project-scoped AWS foundation deployment",
            "--max-session-duration",
            "3600",
            "--tags",
            *tags,
        )
    current_attachments = aws(
        args.profile,
        args.region,
        "iam",
        "list-attached-role-policies",
        "--role-name",
        ROLE_NAME,
    )["AttachedPolicies"]
    attached = {item["PolicyArn"] for item in current_attachments}
    if attached - set(attachments):
        raise ValueError("Unexpected policies on deployment role; refusing to continue")
    if aws(args.profile, args.region, "iam", "list-role-policies", "--role-name", ROLE_NAME)[
        "PolicyNames"
    ]:
        raise ValueError("Unexpected inline policies on deployment role; refusing to continue")
    for arn in attachments:
        if arn not in attached:
            aws(
                args.profile,
                args.region,
                "iam",
                "attach-role-policy",
                "--role-name",
                ROLE_NAME,
                "--policy-arn",
                arn,
            )
    print(f"Verified deployment role: arn:aws:iam::{args.account_id}:role{IAM_PATH}{ROLE_NAME}")
    print("Four scoped policies attached; node boundary prepared; no infrastructure provisioned.")


if __name__ == "__main__":
    main()
