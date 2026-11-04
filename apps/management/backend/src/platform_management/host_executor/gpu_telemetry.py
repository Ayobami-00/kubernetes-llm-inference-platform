"""Observe real DCGM samples from both existing Prometheus replicas.

The privileged CP transport uses an ephemeral loopback-only kubectl forward.
No metric ingestion rule is bypassed: this queries samples Prometheus scraped.
"""

import json
import math
import re
import time

from common import utc_now

from platform_management.host_executor.cleanup import kubectl

QUERY_PROGRAM = r"""
import http.client, json, socket, subprocess, sys, tempfile, time
from urllib.parse import urlencode
request = json.loads(sys.stdin.read())
with tempfile.TemporaryFile(mode='w+') as log:
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([
        'kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '-n', 'observability',
        'port-forward', 'pod/' + request['pod'], str(port) + ':9090', '--address', '127.0.0.1'
    ], stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError('Private Prometheus forwarding exited')
            log.seek(0)
            if ('Forwarding from 127.0.0.1:' + str(port)) in log.read():
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Private Prometheus forwarding timed out')
            time.sleep(.1)
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            connection.request('GET', '/api/v1/query?' + urlencode({'query': request['query']}))
            response = connection.getresponse()
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError('Prometheus response status or size differs')
            print(json.dumps(json.loads(body)))
        finally:
            connection.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
"""


def samples(data, *, now):
    if data.get("status") != "success" or data.get("data", {}).get("resultType") != "vector":
        raise ValueError("DCGM query did not return a successful instant vector")
    values = data["data"]["result"]
    if not values:
        return []
    result = []
    for value in values:
        stamp = float(value["value"][1])
        if not math.isfinite(stamp) or not 0 <= now - stamp <= 60:
            return []
        result.append(stamp)
    return result


def observe(cp, task, device, exporter, *, clock=time.time, sleep=time.sleep):
    if not re.fullmatch(r"GPU-[a-f0-9-]{36}", device["uuid"]):
        raise ValueError("Invalid GPU telemetry identity")
    # Require an actual FB_USED sample. An unsupported/missing counter leaves
    # readiness false; timestamp() reports its real scrape timestamp.
    if exporter.get("node") != task.node_name or not exporter.get("uid"):
        raise ValueError("DCGM exporter is not bound to the inspected node")
    query = (
        "timestamp(DCGM_FI_DEV_FB_USED{UUID="
        + json.dumps(device["uuid"])
        + ',namespace="gpu-system",pod='
        + json.dumps(exporter["name"])
        + "})"
    )
    pods = json.loads(
        kubectl(
            cp,
            "-n",
            "observability",
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=prometheus",
            "-o",
            "json",
        )
    )["items"]
    if len(pods) != 2 or any(
        not re.fullmatch(r"prometheus-klip-monitoring-prometheus-[01]", p["metadata"]["name"])
        or not p["status"].get("containerStatuses")
        or not all(c.get("ready") for c in p["status"]["containerStatuses"])
        for p in pods
    ):
        raise ValueError("Expected both Ready private Prometheus replicas")
    deadline = min(clock() + 90, task.deadline.timestamp() - 15)
    while clock() < deadline:
        receipts = []
        for pod in pods:
            response = json.loads(
                cp.run(
                    ["sudo", "python3", "-c", QUERY_PROGRAM],
                    data=json.dumps(
                        {
                            "pod": pod["metadata"]["name"],
                            "query": query,
                        }
                    ),
                )
            )
            stamps = samples(response, now=clock())
            if not stamps:
                break
            current = json.loads(
                kubectl(
                    cp, "-n", "observability", "get", "pod", pod["metadata"]["name"], "-o", "json"
                )
            )
            if current["metadata"]["uid"] != pod["metadata"]["uid"]:
                raise ValueError("Prometheus replica identity changed during GPU verification")
            receipts.append(
                {
                    "pod": pod["metadata"]["name"],
                    "uid": pod["metadata"]["uid"],
                    "sample_timestamps": stamps,
                }
            )
        if len(receipts) == 2:
            current = json.loads(
                kubectl(cp, "-n", "gpu-system", "get", "pod", exporter["name"], "-o", "json")
            )
            if (
                current["metadata"]["uid"] != exporter["uid"]
                or current["spec"]["nodeName"] != task.node_name
                or not current["status"].get("containerStatuses")
                or not all(s.get("ready") for s in current["status"]["containerStatuses"])
            ):
                raise ValueError("DCGM exporter identity changed during GPU verification")
            return {"observed_at": utc_now(), "query": query, "replicas": receipts}
        sleep(min(3, max(0, deadline - clock())))
    raise ValueError(
        "Fresh GPU-UUID-bound DCGM samples were not observed on both Prometheus replicas"
    )
