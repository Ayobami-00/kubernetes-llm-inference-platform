"""Pinned WriterAssist releases for the retained A10; no provider lifecycle actions."""

import argparse
import hashlib
import json
from pathlib import Path

import yaml
from common import ROOT, tool, utc_now, write_private
from platform_contracts import ServingProfile
from platform_management.services.common import digest
from retained_model_review import assess

NODE = "writer-a10-retained"
INSTANCE = "8f2e29b7279b42148ffaaed4a4dcdd8e"
DEPLOYMENT = "writer-assist-a10"


def chart_digest():
    root = ROOT / "charts/klip-model"
    return digest(
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file()
        }
    )


def profile(variant):
    if variant not in {"baseline", "ngram"}:
        raise ValueError("Only paired WriterAssist variants are allowed")
    return ServingProfile.model_validate(
        yaml.safe_load(
            (ROOT / f"deploy/environments/lab/experiments/a10/{variant}.yaml").read_text()
        )
    )


def values(variant, node_uid):
    baseline, spec = profile("baseline"), profile(variant)
    left, right = (p.model_dump(mode="json") for p in (baseline, spec))
    for item in (left, right):
        for key in ("revision", "notes"):
            item.pop(key, None)
        item["engine"].pop("speculative_decoding", None)
    if left != right or spec.model.repository != "Qwen/Qwen3-4B" or spec.model.alias != "gec-small":
        raise ValueError("Paired experiment settings differ beyond n-gram decoding")
    value = yaml.safe_load((ROOT / "charts/klip-model/values.yaml").read_text())
    value.update(
        profileRef=spec.ref,
        profileSHA256=digest(spec.model_dump(mode="json")),
        deploymentId=DEPLOYMENT,
        nodeUID=node_uid,
        workerClass="latency",
    )
    value["model"].update(
        repository=spec.model.repository,
        revision=spec.model.revision,
        tokenizerRevision=spec.model.tokenizer_revision,
        alias=spec.model.alias,
    )
    engine = spec.engine
    value["engine"].update(
        image=engine.image,
        dtype=engine.dtype,
        maxModelLen=engine.max_model_len,
        maxNumSeqs=engine.max_num_seqs,
        gpuMemoryUtilization=engine.gpu_memory_utilization,
        chatTemplateKwargs=engine.chat_template_kwargs,
        speculativeDecoding=engine.speculative_decoding.model_dump()
        if engine.speculative_decoding
        else None,
    )
    return value


def checked_node(evidence):
    from cluster import kubectl

    node = json.loads(kubectl("get", "node", NODE, "-o", "json"))
    original = json.loads((evidence / "candidate.json").read_text())["uid"]
    boot = json.loads((evidence / "runtime-inspection.json").read_text())["boot_id"]
    labels = node["metadata"]["labels"]
    if (
        node["metadata"]["uid"] != original
        or node["spec"].get("providerID") != "lambda:///" + INSTANCE
        or node["status"]["nodeInfo"]["bootID"] != boot
        or labels.get("klip.dev.node-restriction.kubernetes.io/gpu-ready") != "true"
        or labels.get("klip.dev.node-restriction.kubernetes.io/gpu-node-uid") != original
        or node["status"].get("allocatable", {}).get("nvidia.com/gpu") != "1"
        or any(t["key"] == "klip.dev/bootstrap" for t in node["spec"].get("taints", []))
        or not any(
            c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]
        )
    ):
        raise ValueError("Retained A10 identity, boot or GPU readiness differs")
    return {"uid": original, "boot_id": boot, "provider_id": node["spec"]["providerID"]}


def prepare(evidence, scans, variant):
    node = checked_node(evidence)
    review = assess(scans)
    resolved = values(variant, node["uid"])
    if resolved["engine"]["image"] != review["image"]:
        raise ValueError("Model image differs from the exact experiment review")
    import subprocess

    rendered = subprocess.check_output(
        [
            tool("helm"),
            "template",
            DEPLOYMENT + "-model",
            str(ROOT / "charts/klip-model"),
            "-n",
            "llm-d",
            "--values",
            "-",
        ],
        input=yaml.safe_dump(resolved),
        text=True,
    )
    plan = {
        "created_at": utc_now(),
        "variant": variant,
        "node": node,
        "values": resolved,
        "review": review,
        "scans": str(scans.resolve()),
        "chart_sha256": chart_digest(),
        "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
    }
    approval = digest(plan)
    plan["approval_sha256"] = approval
    directory = evidence / "models" / approval
    from platform_management.integrations.job_runner import frozen_file

    frozen_file(directory / "plan.json", (json.dumps(plan, indent=2) + "\n").encode())
    frozen_file(directory / "rendered.yaml", rendered.encode())
    frozen_file(directory / "values.yaml", yaml.safe_dump(resolved).encode())
    return {"approval_sha256": approval, "variant": variant, "profile_ref": resolved["profileRef"]}


def deploy(evidence, approval):
    import re

    from cluster import FOLDER, checked, kubeconfigs, kubectl, tunnel

    if not re.fullmatch(r"[a-f0-9]{64}", approval):
        raise ValueError("Exact saved model plan required")
    root = evidence / "models" / approval
    plan = json.loads((root / "plan.json").read_text())
    unsigned = {k: v for k, v in plan.items() if k != "approval_sha256"}
    if (
        digest(unsigned) != approval
        or plan["chart_sha256"] != chart_digest()
        or plan["node"] != checked_node(evidence)
        or plan["review"] != assess(Path(plan["scans"]))
        or plan["values"] != values(plan["variant"], plan["node"]["uid"])
        or hashlib.sha256((root / "rendered.yaml").read_bytes()).hexdigest()
        != plan["rendered_sha256"]
    ):
        raise ValueError("Model release inputs changed after planning")
    if (root / "result.json").exists():
        raise ValueError("Observe the existing model release instead of restarting it")
    receipt = {"status": "deploying", "started_at": utc_now(), "approval_sha256": approval}
    write_private(root / "result.json", receipt)
    try:
        kubeconfigs()
        with tunnel():
            output = checked(
                [
                    tool("helm"),
                    "upgrade",
                    "--install",
                    DEPLOYMENT + "-model",
                    str(ROOT / "charts/klip-model"),
                    "-n",
                    "llm-d",
                    "--kubeconfig",
                    str(FOLDER / "operator.conf"),
                    "--values",
                    str(root / "values.yaml"),
                    "--wait",
                    "--timeout",
                    "15m",
                    "--history-max",
                    "5",
                ],
                timeout=930,
            )
            write_private(root / "helm.log", output)
        deployment = json.loads(
            kubectl("-n", "llm-d", "get", "deployment", DEPLOYMENT, "-o", "json")
        )
        if deployment.get("status", {}).get("readyReplicas") != 1:
            raise ValueError("Model is not ready")
        pods = json.loads(
            kubectl(
                "-n",
                "llm-d",
                "get",
                "pods",
                "-l",
                "klip.dev/serving-deployment=" + DEPLOYMENT,
                "-o",
                "json",
            )
        )["items"]
        if (
            len(pods) != 1
            or pods[0]["spec"]["nodeName"] != NODE
            or not pods[0]["status"]["containerStatuses"][0]["imageID"].endswith(
                plan["review"]["image"].split("@")[1]
            )
        ):
            raise ValueError("Model placement or actual image differs")
        receipt.update(
            status="ready",
            completed_at=utc_now(),
            deployment_uid=deployment["metadata"]["uid"],
            pod_uid=pods[0]["metadata"]["uid"],
            node=checked_node(evidence),
            profile_ref=plan["values"]["profileRef"],
        )
    except Exception as error:
        receipt.update(status="requires-observation", error_type=type(error).__name__)
        raise
    finally:
        write_private(root / "result.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "deploy"])
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--scans", type=Path)
    parser.add_argument("--variant", choices=["baseline", "ngram"], default="baseline")
    parser.add_argument("--approve-plan")
    args = parser.parse_args()
    result = (
        prepare(args.evidence.resolve(), args.scans, args.variant)
        if args.action == "prepare"
        else deploy(args.evidence.resolve(), args.approve_plan)
    )
    print(json.dumps(result, indent=2))
