"""Header applicability is exact component evidence, never blanket suppression."""

import copy
from datetime import UTC, datetime, timedelta

import gpu_runtime_security as security
import pytest


def report():
    return {
        "Results": [
            {
                "Packages": [
                    {
                        "Name": "linux-libc-dev",
                        "InstalledFiles": [
                            "/usr/include/linux/example.h",
                            "/usr/share/doc/linux-libc-dev/copyright",
                        ],
                    }
                ],
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-synthetic",
                        "PkgName": "linux-libc-dev",
                        "InstalledVersion": "synthetic-1",
                        "Severity": "CRITICAL",
                    }
                ],
            }
        ]
    }


def review(value):
    result = {}
    for key, rows in security.groups(value).items():
        result[key] = {
            "count": len(rows),
            "sha256": security.digest(rows),
            "rationale": "Synthetic test applicability evidence",
        }
    files = value["Results"][0]["Packages"][0]["InstalledFiles"]
    result["kernel_headers"].update(
        file_count=len(files), files_sha256=security.digest(sorted(files))
    )
    return result


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "runtime-file",
        "changed-header",
        "duplicate-package",
        "changed-finding",
        "missing-rationale",
    ],
)
def test_header_only_review_requires_complete_matching_inventory(mutation):
    native = report()
    reviewed = review(native)
    if mutation == "runtime-file":
        # Even an updated inventory checksum cannot waive a running implementation.
        native["Results"][0]["Packages"][0]["InstalledFiles"].append("/boot/vmlinuz")
        reviewed = review(native)
    elif mutation == "changed-header":
        native["Results"][0]["Packages"][0]["InstalledFiles"].append("/usr/include/new.h")
    elif mutation == "duplicate-package":
        native["Results"][0]["Packages"].append(copy.deepcopy(native["Results"][0]["Packages"][0]))
    elif mutation == "changed-finding":
        native["Results"][0]["Vulnerabilities"][0]["VulnerabilityID"] = "CVE-unreviewed"
    elif mutation == "missing-rationale":
        reviewed["kernel_headers"]["rationale"] = ""
    if mutation:
        with pytest.raises(ValueError):
            security.check_findings(native, reviewed)
    else:
        assert security.check_findings(native, reviewed) == {"kernel_headers": 1, "runtime": 0}


def test_critical_running_implementation_cannot_be_reviewed_away():
    native = report()
    native["Results"][0]["Vulnerabilities"][0]["PkgName"] = "runtime-implementation"
    with pytest.raises(ValueError, match="Critical runtime implementation"):
        security.check_findings(native, review(native))


@pytest.mark.parametrize("age", [timedelta(hours=25), timedelta(seconds=-1)])
def test_expired_and_future_native_evidence_are_rejected(age):
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="timestamp"):
        security.recent((now - age).isoformat(), now)
    security.recent((now - timedelta(hours=1)).isoformat(), now)


def test_native_report_checksum_cannot_be_substituted(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="identity"):
        security.native(path, "0" * 64, datetime.now(UTC))
