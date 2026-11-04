"""Open operator-only localhost access to Kubernetes and private Grafana."""

import argparse
import fcntl
import shlex
import socket
import subprocess
import time
from urllib.parse import urlsplit

import yaml
from cluster import FOLDER, Host, load_hosts
from common import PROFILES, environment, private_dir, tool, write_private
from inventory import verify_ca


def port(value):
    number = int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("Use an unprivileged local port (1024–65535)")
    return number


def free(number):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", number))
        except OSError as error:
            raise ValueError(f"Local port {number} is occupied; choose a different port") from error


def stop(process):
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def run(api_port=16444, grafana_port=13000, *, check=False):
    if api_port == grafana_port:
        raise ValueError("Kubernetes and Grafana require different local ports")
    private_dir(FOLDER)
    with (FOLDER / "user-access.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        free(api_port)
        free(grafana_port)
        cp = Host(load_hosts()["cp-01"])
        source = cp.run(["sudo", "cat", "/etc/kubernetes/admin.conf"], secret=True)
        config = yaml.safe_load(source)
        if len(config["clusters"]) != 1:
            raise ValueError("Expected the retained cluster's single operator endpoint")
        cluster = config["clusters"][0]["cluster"]
        endpoint = urlsplit(cluster["server"])
        expected = urlsplit(cp.fields["klip_node"]["api_endpoint"])
        if (
            (endpoint.scheme, endpoint.hostname, endpoint.port or 443)
            != (expected.scheme, expected.hostname, expected.port or 443)
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
        ):
            raise ValueError("Operator endpoint differs from the pinned cluster")
        cluster.update(
            server=f"https://127.0.0.1:{api_port}", **{"tls-server-name": endpoint.hostname}
        )
        kubeconfig = FOLDER / "user-access.conf"
        write_private(kubeconfig, yaml.safe_dump(config))
        api, grafana = None, None
        write_private(FOLDER / "user-access.log", "")
        with (FOLDER / "user-access.log").open("a") as log:
            try:
                ssh = cp.ssh_arguments(reuse=False)
                api = subprocess.Popen(
                    [
                        *ssh[:-1],
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-N",
                        "-L",
                        f"127.0.0.1:{api_port}:{endpoint.hostname}:{endpoint.port or 443}",
                        ssh[-1],
                    ],
                    env=environment(PROFILES["foundation"]),
                    stdout=log,
                    stderr=log,
                )
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if api.poll() is not None:
                        raise ValueError("AWS/SSH tunnel exited; check the private access log")
                    try:
                        verify_ca(FOLDER / "ca.crt", endpoint.geturl(), ("127.0.0.1", api_port))
                        break
                    except OSError:
                        time.sleep(0.5)
                else:
                    raise ValueError("Cluster TLS verification timed out")
                grafana = subprocess.Popen(
                    [
                        tool("kubectl"),
                        "--kubeconfig",
                        str(kubeconfig),
                        "-n",
                        "observability",
                        "port-forward",
                        "--address=127.0.0.1",
                        "service/klip-grafana",
                        f"{grafana_port}:80",
                    ],
                    stdout=log,
                    stderr=log,
                    env=environment(PROFILES["foundation"]),
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if grafana.poll() is not None:
                        raise ValueError("Grafana forward exited; check the private access log")
                    try:
                        with socket.create_connection(("127.0.0.1", grafana_port), timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.5)
                else:
                    raise ValueError("Grafana forward did not open")
                print(
                    "Operator access is ready. Leave this terminal running; Ctrl-C closes it.",
                    flush=True,
                )
                print("export KUBECONFIG=" + shlex.quote(str(kubeconfig)), flush=True)
                print(shlex.quote(tool("kubectl")) + " get nodes -o wide", flush=True)
                print(
                    f"Grafana: http://127.0.0.1:{grafana_port}/d/klip-a10-speculation", flush=True
                )
                print(
                    "Grafana username: admin; password remains in "
                    "the private operator credential file.",
                    flush=True,
                )
                if check:
                    result = subprocess.run(
                        [
                            tool("kubectl"),
                            "--kubeconfig",
                            str(kubeconfig),
                            "get",
                            "nodes",
                            "-o",
                            "name",
                        ],
                        text=True,
                        capture_output=True,
                        check=True,
                        timeout=30,
                    )
                    print(
                        "Verified cluster access: "
                        + str(len(result.stdout.splitlines()))
                        + " nodes",
                        flush=True,
                    )
                    return
                while api.poll() is None and grafana.poll() is None:
                    time.sleep(1)
                raise ValueError("An access tunnel exited; reconnect explicitly")
            finally:
                stop(grafana)
                stop(api)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=port, default=16444)
    parser.add_argument("--grafana-port", type=port, default=13000)
    parser.add_argument(
        "--check", action="store_true", help="verify access, then close both forwards"
    )
    args = parser.parse_args()
    try:
        run(args.api_port, args.grafana_port, check=args.check)
    except KeyboardInterrupt:
        print("Local access tunnels closed. Cluster workloads continue running.")
