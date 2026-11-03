"""Append project profiles without replacing existing credentials or unrelated settings."""

import shutil
from configparser import RawConfigParser
from pathlib import Path

from common import CONFIG


def configure_profiles() -> None:
    executable = shutil.which("aws") or "/opt/homebrew/bin/aws"
    profile_path = Path.home() / ".aws/config"
    config = RawConfigParser()
    config.read(profile_path)
    common = {"region": CONFIG["region"], "output": "json"}
    account, project = CONFIG["account_id"], CONFIG["project"]
    values = {
        "kubernetes-llm-console": {
            **common,
            "login_session": f"arn:aws:iam::{account}:user/{CONFIG['source_user']}",
        },
        "kubernetes-llm-bootstrap": {
            **common,
            "credential_process": (
                f"{executable} configure export-credentials "
                "--profile kubernetes-llm-console --format process"
            ),
        },
    }
    for label, role, session in (
        ("foundation", "kubernetes-llm-foundation-deployer", "klip-foundation"),
        ("dns", "kubernetes-llm-inference-dns-deployer", "klip-dns"),
    ):
        values[f"kubernetes-llm-{label}-session"] = {
            **common,
            "source_profile": "kubernetes-llm-console",
            "role_arn": f"arn:aws:iam::{account}:role/{project}/{role}",
            "role_session_name": session,
            "duration_seconds": "3600",
        }
        values[f"kubernetes-llm-{label}"] = {
            **common,
            "credential_process": (
                f"{executable} configure export-credentials "
                f"--profile kubernetes-llm-{label}-session --format process"
            ),
        }
    for name, expected in values.items():
        section = f"profile {name}"
        if config.has_section(section) and any(
            config.get(section, key, fallback=None) != value for key, value in expected.items()
        ):
            raise ValueError(f"Existing profile differs; preserved without changes: {name}")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("a") as stream:
        for name, expected in values.items():
            if not config.has_section(f"profile {name}"):
                stream.write(f"\n[profile {name}]\n")
                for key, value in expected.items():
                    stream.write(f"{key} = {value}\n")
    print("Project profiles verified; existing credentials preserved")


if __name__ == "__main__":
    configure_profiles()
