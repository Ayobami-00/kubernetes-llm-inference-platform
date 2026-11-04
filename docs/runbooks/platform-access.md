# Private operator access

The Kubernetes API and Grafana remain private. On the operator laptop, the access
helper opens a strict SSH/SSM tunnel to the retained control plane, verifies the
cluster TLS CA/name, then forwards Grafana on loopback. It does not deploy,
restart or delete a workload or VM. Its API port is separate from the automation
operator's port, so it can stay open during an experiment.

From this checkout, leave the first terminal running:

```sh
.venv/bin/python scripts/infrastructure/platform_access.py
```

In another terminal:

```sh
export KUBECONFIG="$HOME/.local/state/kubernetes-llm-inference-platform/cluster/user-access.conf"
/Users/enfinity/dev/PERSONAL/PROJECTS/kubernetes-llm-inference-platform/artifacts/tools/bin/kubectl get nodes -o wide
```

The generated file reuses the existing operator/admin identity and is written with
private permissions. Do not commit or share it. `kubectl` does not require GPU or
provider credentials. The first terminal must stay open for these connections.
If AWS authentication has expired, sign in with
`aws login --profile kubernetes-llm-console`, then reconnect explicitly.

Open <http://127.0.0.1:13000/d/klip-a10-speculation> for the A10 view, or
<http://127.0.0.1:13000/dashboards> for the dashboard list. Grafana's user is `admin`.
On macOS, copy its retained password without putting the value in a command:

```sh
jq -rj '."admin-password"' \
  "$HOME/.local/state/kubernetes-llm-inference-platform/credentials/grafana.json" | pbcopy
```

There is no public Grafana DNS endpoint in this release. The localhost HTTP hop
stays on the laptop; the remote path uses the authenticated SSH/Kubernetes tunnel.
The Grafana dashboard was tested through this route in Chromium.

Ctrl-C closes both local forwards. Running cluster Jobs and the retained A10
continue independently. If a connection exits, the helper closes its companion
forward and asks for an explicit reconnect; it never restarts cluster workloads.
For occupied local ports use `--api-port 16445 --grafana-port 13001` and follow the
printed URL/configuration. `--check` verifies access, then closes both forwards.
Private tunnel diagnostics are in the operator state's `cluster/user-access.log`.
