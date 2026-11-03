"""Temporary, bounded pre-Kubernetes transport tests. Never run against a joined cluster."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from common import PROFILES, Aws, state_home, utc_now, write_private


def wait_until(operation: Callable, description: str, timeout: int = 300, interval: int = 5):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = operation()
            if result:
                return result
        except RuntimeError as error:
            last_error = str(error)
        time.sleep(interval)
    raise RuntimeError(
        f"Timed out waiting for {description}" + (f": {last_error}" if last_error else "")
    )


def ssm_command(aws: Aws, instance_id: str, script: str, timeout: int = 90) -> dict:
    result = aws.call(
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--timeout-seconds",
        "60",
        "--parameters",
        json.dumps({"commands": ["set -eu", script], "executionTimeout": [str(timeout)]}),
        "--comment",
        "klip foundation verification",
    )
    command_id = result["Command"]["CommandId"]

    def invocation():
        try:
            value = aws.call(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            )
        except RuntimeError as error:
            if "InvocationDoesNotExist" in str(error):
                return None
            raise
        if value["Status"] in {"Pending", "InProgress", "Delayed"}:
            return None
        return value

    value = wait_until(invocation, f"SSM command {command_id}", timeout=timeout + 90)
    if value["Status"] != "Success":
        raise ValueError(
            f"SSM command failed on {instance_id}: "
            f"{value.get('StandardErrorContent', '')} {value.get('StandardOutputContent', '')}"
        )
    return {
        "command_id": command_id,
        "observed_at": utc_now(),
        "stdout": value.get("StandardOutputContent", ""),
    }


def assert_prebootstrap(include_etcd: bool = False) -> str:
    script = """if [ -e /etc/kubernetes/admin.conf ] || \\
   [ -f /etc/kubernetes/manifests/kube-apiserver.yaml ]; then
  echo 'Refusing prebootstrap operation: Kubernetes configuration exists' >&2; exit 42
fi
if ss -ltn | awk '{print $4}' | grep -Eq '(^|:)6443$'; then
  echo 'Refusing prebootstrap operation: port 6443 is already in use' >&2; exit 42
fi
"""
    if include_etcd:
        # Do not mount or format data disks to inspect them. Empty acknowledgements
        # are accepted only while the attached dedicated disks have no signatures.
        script += """if [ -d /var/lib/etcd/member ]; then echo 'etcd data exists' >&2; exit 42; fi
python3 - <<'PY'
import json, subprocess
args=['lsblk','--json','--output','NAME,TYPE,FSTYPE,MOUNTPOINT,SIZE','--bytes']
devices=json.loads(subprocess.check_output(args))['blockdevices']
data=[item for item in devices if item['type']=='disk' and int(item.get('size',0))==20*1024**3]
if len(data)!=1 or data[0].get('fstype') or data[0].get('mountpoint') or data[0].get('children'):
    raise SystemExit('Cannot prove the etcd disk is empty; provide a verified backup')
PY
"""
    return script


def start_listener(node_id: str) -> str:
    server = """import http.server, json
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body=json.dumps({'node':NODE}).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass
http.server.HTTPServer(('0.0.0.0',6443),Handler).serve_forever()
""".replace("NODE", repr(node_id))
    return (
        "install -d -m 700 /run/klip-foundation-smoke\n"
        "cat > /run/klip-foundation-smoke/server.py <<'KLIP_SMOKE_PY'\n"
        + server
        + "KLIP_SMOKE_PY\n"
        "systemd-run --unit=klip-foundation-smoke --property=RuntimeMaxSec=1200 "
        "--property=WorkingDirectory=/run/klip-foundation-smoke "
        "/usr/bin/python3 /run/klip-foundation-smoke/server.py\n"
        "systemctl is-active --quiet klip-foundation-smoke\n"
    )


def probe_script(dns_name: str, count: int = 20) -> str:
    return f"""python3 - <<'PY'
import json, time, urllib.request
success=[]; errors=[]
url={json.dumps("http://" + dns_name + ":6443/")}
for _ in range({count}):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            success.append(json.load(response)['node'])
    except Exception as error:
        errors.append(str(error))
    time.sleep(0.2)
print(json.dumps({{'success':success,'errors':errors}}))
PY
"""


def smoke(manifest: dict) -> dict:
    aws = Aws(PROFILES["foundation"])
    aws.preflight("foundation")
    nodes = manifest["nodes"]
    if set(nodes) != {"cp-01", "cp-02", "cp-03"}:
        raise ValueError("Unexpected smoke-test node set")
    target = nodes["cp-03"]["instance_id"]
    group = manifest["api"]["target_group_arn"]
    report = {"started_at": utc_now(), "scope": "prebootstrap AWS TCP transport", "probes": {}}
    started, stop_attempted = [], False
    primary_error, cleanup_errors = None, []

    def target_health():
        values = aws.call("elbv2", "describe-target-health", "--target-group-arn", group)[
            "TargetHealthDescriptions"
        ]
        return {item["Target"]["Id"]: item["TargetHealth"]["State"] for item in values}

    try:
        # Guard every host before starting anything or scheduling an outage.
        for node in nodes.values():
            ssm_command(aws, node["instance_id"], assert_prebootstrap())
        for name, node in nodes.items():
            started.append(name)
            ssm_command(aws, node["instance_id"], start_listener(name))
        wait_until(
            lambda: (
                len(target_health()) == 3
                and all(value == "healthy" for value in target_health().values())
            ),
            "all three smoke targets to become healthy",
            timeout=300,
        )
        for name, node in nodes.items():
            result = json.loads(
                ssm_command(aws, node["instance_id"], probe_script(manifest["api"]["dns_name"]))[
                    "stdout"
                ]
            )
            if len(result["success"]) != 20 or result["errors"]:
                raise ValueError(f"NLB transport failed from {name}: {result}")
            report["probes"][name] = result
        stop_attempted = True
        aws.call("ec2", "stop-instances", "--instance-ids", target)
        during = json.loads(
            ssm_command(
                aws, nodes["cp-01"]["instance_id"], probe_script(manifest["api"]["dns_name"], 30)
            )["stdout"]
        )
        report["during_health_convergence"] = during
        failed_ip = nodes["cp-03"]["private_ip"]
        wait_until(
            lambda: target_health().get(failed_ip) == "unhealthy",
            "cp-03 target removal",
            timeout=300,
        )
        after = json.loads(
            ssm_command(
                aws, nodes["cp-01"]["instance_id"], probe_script(manifest["api"]["dns_name"])
            )["stdout"]
        )
        if len(after["success"]) != 20 or after["errors"] or "cp-03" in after["success"]:
            raise ValueError(f"NLB failover failed after health convergence: {after}")
        report["after_health_convergence"] = after
    except BaseException as error:
        primary_error = error
    finally:
        if stop_attempted:
            try:

                def instance_state():
                    return aws.call("ec2", "describe-instances", "--instance-ids", target)[
                        "Reservations"
                    ][0]["Instances"][0]["State"]["Name"]

                if instance_state() == "stopping":
                    wait_until(
                        lambda: instance_state() == "stopped",
                        "cp-03 to finish stopping",
                        timeout=300,
                    )
                restarted = instance_state() == "stopped"
                if restarted:
                    aws.call("ec2", "start-instances", "--instance-ids", target)
                wait_until(
                    lambda: ssm_online(aws, target),
                    "cp-03 SSM recovery",
                    timeout=600,
                )
                if restarted:
                    ssm_command(aws, target, assert_prebootstrap())
                    ssm_command(aws, target, start_listener("cp-03"))
                wait_until(
                    lambda: target_health().get(nodes["cp-03"]["private_ip"]) == "healthy",
                    "cp-03 smoke target recovery",
                    timeout=300,
                )
            except (RuntimeError, ValueError) as error:
                cleanup_errors.append(f"cp-03 recovery: {error}")
        for name in started:
            try:
                ssm_command(
                    aws,
                    nodes[name]["instance_id"],
                    "systemctl stop klip-foundation-smoke || true\n"
                    "systemctl reset-failed klip-foundation-smoke || true\n"
                    "rm -rf /run/klip-foundation-smoke\n"
                    "if ss -ltn | awk '{print $4}' | "
                    "grep -Eq '(^|:)6443$'; then exit 1; fi",
                )
            except (RuntimeError, ValueError) as error:
                cleanup_errors.append(f"listener cleanup on {name}: {error}")
        try:
            final_health = target_health()
        except (RuntimeError, ValueError) as error:
            final_health = {}
            cleanup_errors.append(f"Final target-health observation: {error}")
        report.update(
            {
                "finished_at": utc_now(),
                "passed": primary_error is None and not cleanup_errors,
                "error": str(primary_error) if primary_error else None,
                "cleanup_errors": cleanup_errors,
                "final_target_health": final_health,
            }
        )
        write_private(state_home() / "reports/smoke.json", report)
    if primary_error or cleanup_errors:
        if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
            raise primary_error
        raise RuntimeError(f"Smoke test failed: {primary_error}; cleanup: {cleanup_errors}")
    print(json.dumps(report, indent=2))
    return report


def ssm_online(aws: Aws, instance_id: str) -> bool:
    records = aws.call(
        "ssm",
        "describe-instance-information",
        "--filters",
        json.dumps([{"Key": "InstanceIds", "Values": [instance_id]}]),
    )["InstanceInformationList"]
    return bool(records) and records[0]["PingStatus"] == "Online"
