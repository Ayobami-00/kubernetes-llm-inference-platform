"""Stage declared uv workspace members from the filtered Docker build context."""

import shutil
import sys
import tomllib
from pathlib import Path

IGNORED = shutil.ignore_patterns(
    ".git",
    ".venv",
    ".cache",
    "__pycache__",
    "*.pyc",
    "*.egg-info",
    "node_modules",
    ".secrets",
    ".env",
    ".env.*",
    ".aws",
    ".terraform",
    "*.tfstate",
    "*.tfstate.*",
    "*.pem",
    "*.key",
    "artifacts",
)


def stage(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    workspace = tomllib.loads((source / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]
    exclusions = workspace.get("exclude", [])
    copied = []
    for pattern in workspace["members"]:
        matches = list(source.glob(pattern))
        if not matches:
            raise ValueError(f"Declared workspace member is missing: {pattern}")
        for member in matches:
            if not member.resolve().is_relative_to(source):
                raise ValueError("Workspace members must stay inside the build context")
            relative = member.relative_to(source)
            if any(relative.match(exclude) for exclude in exclusions):
                continue
            if not (member / "pyproject.toml").is_file():
                continue
            shutil.copytree(member, destination / relative, ignore=IGNORED, dirs_exist_ok=True)
            copied.append(str(relative))
    return copied


if __name__ == "__main__":
    stage(*sys.argv[1:3])
