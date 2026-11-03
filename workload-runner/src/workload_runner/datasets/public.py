"""Bounded public source readers. All payloads stay under ignored preparation storage."""

import io
import json
import re
import zipfile
from pathlib import Path

import httpx

from workload_runner.artifacts import digest

from .preparation import messages_for


class DownloadBudget:
    def __init__(self, directory: Path, limit: int, metadata: dict):
        self.directory = directory
        self.limit = limit
        self.metadata = metadata
        self.client = httpx.Client(follow_redirects=True, timeout=90)
        directory.mkdir(parents=True, exist_ok=True)

    def fetch(self, url: str) -> bytes:
        path = self.directory / digest(url.encode())
        if path.exists():
            return path.read_bytes()
        chunks = []
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                self.account(len(chunk))
                chunks.append(chunk)
        value = b"".join(chunks)
        path.write_bytes(value)
        return value

    def account(self, count):
        if self.metadata["bytes"] + count > self.limit:
            raise ValueError("dataset download bound exceeded; increase --byte-limit explicitly")
        self.metadata["bytes"] += count

    def github(self, repo, name):
        info = json.loads(self.fetch(f"https://api.github.com/repos/{repo}/commits/HEAD"))
        revision = info["sha"]
        self.metadata["revisions"][name] = revision
        self.metadata["urls"][name] = f"https://github.com/{repo}/tree/{revision}"
        self.metadata["licenses"][name] = None
        base = f"https://raw.githubusercontent.com/{repo}/{revision}"
        tree = json.loads(self.fetch(f"https://api.github.com/repos/{repo}/git/trees/{revision}"))
        readme = next(
            (item["path"] for item in tree["tree"] if item["path"].lower().startswith("readme")),
            None,
        )
        if readme:
            evidence_url = base + "/" + readme
            evidence = self.fetch(evidence_url)
            self.metadata.setdefault("license_evidence", {})[name] = (
                evidence_url + "#sha256=" + digest(evidence)
            )
            match = re.search(
                r"creativecommons.org/licenses/(by-nc-sa|by-sa|by)/4.0",
                evidence.decode(errors="replace"),
                re.IGNORECASE,
            )
            if match:
                self.metadata["licenses"][name] = "CC-" + match.group(1).upper() + "-4.0"
        return base


class RangeFile(io.RawIOBase):
    """Seekable HTTP range reader for bounded Parquet sampling without downloading a shard."""

    def __init__(self, url, budget, size):
        self.url, self.budget, self.size = url, budget, size
        self.position = 0
        self.blocks = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = (
            offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        )
        if self.position < 0:
            raise ValueError("negative seek")
        return self.position

    def read(self, size=-1):
        size = min(self.size - self.position, self.size if size < 0 else size)
        result = bytearray()
        while len(result) < size:
            block_size = 1024 * 1024
            start = (self.position // block_size) * block_size
            if start not in self.blocks:
                end = min(start + block_size, self.size) - 1
                cache = self.budget.directory / "ranges" / digest(self.url.encode()) / str(start)
                if cache.exists():
                    self.blocks[start] = cache.read_bytes()
                    continue
                with self.budget.client.stream(
                    "GET", self.url, headers={"Range": f"bytes={start}-{end}"}
                ) as response:
                    response.raise_for_status()
                    if response.status_code != 206 and self.size > block_size:
                        raise ValueError("source does not support bounded HTTP range reads")
                    if response.status_code == 206 and not response.headers.get(
                        "content-range", ""
                    ).startswith(f"bytes {start}-{end}/"):
                        raise ValueError("source returned an unexpected byte range")
                    chunks = []
                    received = 0
                    for chunk in response.iter_bytes():
                        self.budget.account(len(chunk))
                        received += len(chunk)
                        if received > block_size:
                            raise ValueError("range response exceeded requested block")
                        chunks.append(chunk)
                    self.blocks[start] = b"".join(chunks)
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    import os
                    from tempfile import NamedTemporaryFile

                    with NamedTemporaryFile(dir=cache.parent, delete=False) as stream:
                        stream.write(self.blocks[start])
                    os.replace(stream.name, cache)
            block = self.blocks[start]
            local = self.position - start
            piece = block[local : local + min(size - len(result), len(block) - local)]
            if not piece:
                break
            result.extend(piece)
            self.position += len(piece)
        return bytes(result)

    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[: len(value)] = value
        return len(value)


def resolve_source_license(info, info_url, metadata, repo, manifest=None):
    """Keep upstream evidence distinct from a declaration for this exact source pin."""
    license_name = info.get("cardData", {}).get("license")
    if isinstance(license_name, list):
        license_name = ", ".join(license_name)
    evidence = info_url + "#sha256=" + digest(canonical_metadata(info))
    if manifest is not None and manifest.source.license is not None:
        if str(manifest.source.url).rstrip("/") != f"https://huggingface.co/datasets/{repo}":
            raise ValueError("license declaration does not match the source repository")
        if manifest.source.revision != info["sha"]:
            raise ValueError("license declaration does not match the resolved source revision")
        if license_name is None:
            license_name = manifest.source.license
            evidence = (
                f"Project declaration in {manifest.ref}, source revision {info['sha']}: "
                f"{manifest.notes} Upstream metadata (license unset): {evidence}"
            )
        elif license_name.casefold() != manifest.source.license.casefold():
            raise ValueError("upstream license conflicts with the project source declaration")
    metadata["licenses"][repo] = license_name
    metadata.setdefault("license_evidence", {})[repo] = evidence


def parquet_rows(budget, repo, revision, pattern, *, matching=None, manifest=None):
    import pyarrow.parquet as pq

    info_url = f"https://huggingface.co/api/datasets/{repo}/revision/{revision}?blobs=true"
    info = json.loads(budget.fetch(info_url))
    resolved = info["sha"]
    budget.metadata["revisions"][repo] = resolved
    budget.metadata["urls"][repo] = f"https://huggingface.co/datasets/{repo}/tree/{resolved}"
    resolve_source_license(info, info_url, budget.metadata, repo, manifest)
    files = [file for file in info["siblings"] if re.search(pattern, file["rfilename"])]
    for file in files:
        name = file["rfilename"]
        url = f"https://huggingface.co/datasets/{repo}/resolve/{resolved}/{name}"
        size = file.get("size") or file.get("lfs", {}).get("size")
        if size is None:
            response = budget.client.head(url)
            response.raise_for_status()
            size = int(response.headers["content-length"])
        reader = RangeFile(url, budget, size)
        parquet = pq.ParquetFile(reader)
        for group in range(parquet.num_row_groups):
            if matching and matching[0] in parquet.schema_arrow.names:
                values = parquet.read_row_group(group, columns=[matching[0]]).column(0).to_pylist()
                if matching[1] not in values:
                    continue
            for batch in parquet.iter_batches(batch_size=1, row_groups=[group]):
                for row in batch.to_pylist():
                    if matching is None or row.get(matching[0]) == matching[1]:
                        yield row


def canonical_metadata(info):
    return json.dumps(info, sort_keys=True, separators=(",", ":")).encode()


def otel_messages(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    messages = []
    for item in raw:
        if "parts" not in item:
            messages.append(item)
            continue
        message = {"role": item["role"]}
        texts = []
        calls = []
        for part in item["parts"]:
            if part.get("type") == "text":
                texts.append(part.get("content", part.get("text", "")))
            elif part.get("type") in {"tool_call", "tool-call"}:
                arguments = part.get("arguments", {})
                calls.append(
                    {
                        "id": part.get("id", "recorded-call"),
                        "type": "function",
                        "function": {
                            "name": part.get("name", "tool"),
                            "arguments": arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments),
                        },
                    }
                )
            elif part.get("type") in {"tool_call_response", "tool_result", "tool-response"}:
                message["tool_call_id"] = part.get("id", part.get("tool_call_id", "recorded-call"))
                texts.append(str(part.get("response", part.get("content", ""))))
        if texts:
            message["content"] = "".join(texts)
        if calls:
            message["tool_calls"] = calls
        if "content" not in message and not calls:
            raise ValueError("unsupported recorded message parts")
        messages.append(message)
    return messages


def openai_tools(source):
    tools, changed = [], False
    for tool in source:
        if tool.get("type") != "function":
            raise ValueError("unsupported source tool type")
        if "function" in tool:
            if not tool["function"].get("name"):
                raise ValueError("source function tool has no name")
            tools.append(tool)
        elif tool.get("name"):
            tools.append(
                {
                    "type": "function",
                    "function": {key: value for key, value in tool.items() if key != "type"},
                }
            )
            changed = True
        else:
            raise ValueError("source function tool has no name")
    return tools, changed


def public_entries(customer, manifest, limit, cache, byte_limit, metadata):
    budget = DownloadBudget(cache, byte_limit, metadata)
    task = customer.workload.task
    try:
        if task == "sentence_correction":
            base = budget.github("keisks/jfleg", "jfleg")
            for split, bound in (("dev", max(1, min(100, limit // 5))), ("test", limit)):
                inputs = budget.fetch(f"{base}/{split}/{split}.src").decode().splitlines()
                refs = [
                    budget.fetch(f"{base}/{split}/{split}.ref{i}").decode().splitlines()
                    for i in range(4)
                ]
                for index, text in enumerate(inputs[:bound]):
                    yield {
                        "source_id": f"jfleg:{split}:{index}",
                        "warmup": split == "dev",
                        "turns": [
                            (
                                messages_for(customer, text=text),
                                {"corrections": [ref[index] for ref in refs]},
                                (),
                                {},
                            )
                        ],
                    }
        elif task == "tutoring":
            base = budget.github("eth-nlped/mathdial", "mathdial")
            lines = budget.fetch(base + "/data/test.jsonl").decode().splitlines()
            for index, line in enumerate(lines):
                row = json.loads(line)
                dialogue = row.get("conversation", row.get("dialogue", ""))
                turns = []
                if isinstance(dialogue, str):
                    parts = dialogue.split("|EOM|")
                    for part in parts:
                        if part.strip().startswith("Student:"):
                            turns.append(
                                (
                                    messages_for(customer, dialogue=part.strip())
                                    if not turns
                                    else [{"role": "user", "content": part.strip()}],
                                    {"recorded_dialogue": dialogue},
                                    (),
                                    {},
                                )
                            )
                elif isinstance(dialogue, list):
                    for turn in dialogue:
                        if str(turn.get("role", turn.get("speaker", ""))).lower() in {
                            "student",
                            "user",
                        }:
                            text = turn.get("content", turn.get("text", ""))
                            turns.append(
                                (
                                    messages_for(customer, dialogue=text)
                                    if not turns
                                    else [{"role": "user", "content": text}],
                                    {"recorded_dialogue": dialogue},
                                    (),
                                    {},
                                )
                            )
                if turns:
                    yield {
                        "source_id": f"mathdial:test:{row.get('id', index)}",
                        "turns": turns,
                        "context_mode": "live",
                        "adaptations": ["assumed_turn_timing"],
                        "delays": [0.0] + [100.0] * (len(turns) - 1),
                    }
        elif task == "extraction":
            base = budget.github("TheAtticusProject/cuad", "cuad")
            license_url = "https://www.atticusprojectai.org/cuad/"
            evidence = budget.fetch(license_url)
            if re.search(rb"CC\s+BY\s+4\.0", evidence):
                metadata["licenses"]["cuad"] = "CC-BY-4.0"
                metadata.setdefault("license_evidence", {})["cuad"] = (
                    license_url + "#sha256=" + digest(evidence)
                )
            archive = zipfile.ZipFile(io.BytesIO(budget.fetch(base + "/data.zip")))
            names = [name for name in archive.namelist() if name.lower().endswith("test.json")]
            if not names:
                names = [name for name in archive.namelist() if name.endswith("CUAD_v1.json")]
            if not names:
                raise ValueError("CUAD archive contains no supported held-out JSON")
            data = json.loads(archive.read(names[0]))
            for document in data["data"]:
                for paragraph in document["paragraphs"]:
                    for qa in paragraph["qas"]:
                        answers = qa["answers"]
                        anchor = answers[0]["answer_start"] if answers else 0
                        start = max(0, anchor - 1000)
                        context = paragraph["context"][start : start + 12000]
                        retained = [a["text"] for a in answers if a["text"] in context]
                        if answers and not retained:
                            continue
                        yield {
                            "source_id": "cuad:" + qa["id"],
                            "adaptations": ["evidence_preserving_chunk", "adapted_extraction_task"],
                            "turns": [
                                (
                                    messages_for(
                                        customer, context=context, clause_type=qa["question"]
                                    ),
                                    {
                                        "answers": retained or [""],
                                        "original_answer_count": len(answers),
                                        "retained_answer_count": len(retained),
                                        "document_title": document.get("title"),
                                        "chunk_start": start,
                                        "chunk_end": start + len(context),
                                    },
                                    (),
                                    {},
                                )
                            ],
                        }
        elif task == "research":
            for row in parquet_rows(
                budget, "hotpotqa/hotpot_qa", "main", r"distractor/validation.*parquet$"
            ):
                context = row["context"]
                if isinstance(context, dict):
                    pairs = zip(context["title"], context["sentences"], strict=True)
                else:
                    pairs = context
                evidence = "\n".join(
                    f"{title}: "
                    + " ".join(f"[{i}] {sentence}" for i, sentence in enumerate(sentences))
                    for title, sentences in pairs
                )
                facts = row["supporting_facts"]
                if isinstance(facts, dict):
                    facts = [
                        list(pair) for pair in zip(facts["title"], facts["sent_id"], strict=True)
                    ]
                yield {
                    "source_id": "hotpotqa:dev:" + str(row.get("id", row.get("_id"))),
                    "context_mode": "live",
                    "adaptations": ["prepared_qa_substeps", "assumed_turn_timing"],
                    "delays": [0.0, 100.0],
                    "turns": [
                        (
                            messages_for(customer, question=row["question"], context=evidence),
                            {},
                            (),
                            {},
                        ),
                        (
                            [
                                {
                                    "role": "user",
                                    "content": "Return JSON with answer and supporting_facts "
                                    "as [title, sentence index] pairs using the supplied evidence.",
                                }
                            ],
                            {"answer": row["answer"], "supporting_facts": facts},
                            (),
                            {},
                        ),
                    ],
                }
        else:
            for row in parquet_rows(
                budget,
                "Exgentic/agent-llm-traces-v2",
                manifest.source.revision,
                r"data/train/.*parquet$",
                matching=("benchmark", "swebench"),
                manifest=manifest,
            ):
                spans = []
                for span in row.get("spans", []):
                    attrs = span.get("attributes", {})
                    if attrs.get("gen_ai.input.messages"):
                        spans.append((span, attrs))
                spans.sort(key=lambda pair: pair[0].get("start_time", ""))
                turns, delays, source_controls, call_adaptations, source_tools = [], [], [], [], []
                previous_end = None
                for span, attrs in spans:
                    try:
                        messages = otel_messages(attrs["gen_ai.input.messages"])
                        tools = attrs.get("gen_ai.tool.definitions", [])
                        tools = json.loads(tools) if isinstance(tools, str) else tools
                        original_tools = tools or []
                        tools, changed_tools = openai_tools(original_tools)
                        controls = {
                            name: attrs[key]
                            for name, key in (
                                ("max_tokens", "gen_ai.request.max_tokens"),
                                ("temperature", "gen_ai.request.temperature"),
                                ("top_p", "gen_ai.request.top_p"),
                                ("stop", "gen_ai.request.stop_sequences"),
                            )
                            if attrs.get(key) is not None
                        }
                        original = {
                            k: v for k, v in attrs.items() if k.startswith("gen_ai.request.")
                        }
                        source_controls.append(original)
                        supported = {
                            "gen_ai.request." + name
                            for name in (
                                "max_tokens",
                                "temperature",
                                "top_p",
                                "stop_sequences",
                                "model",
                            )
                        }
                        call_adaptations.append(
                            [
                                "ignored_source_control:" + key
                                for key in sorted(set(original) - supported)
                            ]
                            + (
                                ["model_alias_override"]
                                if original.get("gen_ai.request.model")
                                else []
                            )
                            + (["openai_function_tool_envelope"] if changed_tools else [])
                        )
                        source_tools.append(original_tools)
                        turns.append(
                            (
                                messages,
                                {
                                    "source_call_id": span.get("span_id"),
                                    "source_call_index": len(turns),
                                },
                                tuple(tools or ()),
                                controls,
                            )
                        )
                        start = span.get("start_time_unix_nano", span.get("start_time", 0))
                        end = span.get("end_time_unix_nano", span.get("end_time", start))
                        from datetime import datetime

                        def milliseconds(value):
                            if isinstance(value, str) and "T" in value:
                                return (
                                    datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                                    * 1000
                                )
                            return float(value or 0) / 1e6

                        start, end = milliseconds(start), milliseconds(end)
                        delays.append(
                            max(0.0, start - previous_end) if previous_end is not None else 0.0
                        )
                        previous_end = end
                    except (ValueError, TypeError, KeyError):
                        # A contiguous prefix is allowed; skipping a middle call is not.
                        break
                if turns:
                    yield {
                        "source_id": "exgentic:" + str(row["session_id"]),
                        "turns": turns,
                        "delays": delays,
                        "context_mode": "recorded",
                        "source_controls": source_controls,
                        "source_tools": source_tools,
                        "call_adaptations": call_adaptations,
                        "adaptations": ["inference_snapshot_replay"]
                        + (["unsupported_call_prefix"] if len(turns) < len(spans) else []),
                    }
    finally:
        budget.client.close()
