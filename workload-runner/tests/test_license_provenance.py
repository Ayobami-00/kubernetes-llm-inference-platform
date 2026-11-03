import pytest
from workload_runner.datasets.preparation import prepare_customer, synthetic_entries
from workload_runner.datasets.public import resolve_source_license

REPO = "Exgentic/agent-llm-traces-v2"


def test_project_confirmation_fills_missing_upstream_license(catalog):
    manifest = catalog.get("dataset-manifest", "exgentic-code@1")
    metadata = {"licenses": {}}
    resolve_source_license(
        {"sha": manifest.source.revision, "cardData": {}},
        "https://huggingface.co/api/pinned-metadata",
        metadata,
        REPO,
        manifest,
    )
    assert metadata["licenses"][REPO] == "CDLA-Permissive-2.0"
    evidence = metadata["license_evidence"][REPO]
    assert "project owner confirmed" in evidence.casefold()
    assert manifest.source.revision in evidence
    assert "license unset" in evidence


def test_missing_license_without_declaration_stays_unknown():
    metadata = {"licenses": {}}
    resolve_source_license(
        {"sha": "a" * 40, "cardData": {}},
        "https://huggingface.co/api/pinned-metadata",
        metadata,
        REPO,
    )
    assert metadata["licenses"][REPO] is None


@pytest.mark.parametrize("change", ["revision", "repository", "license"])
def test_confirmation_cannot_be_reused_for_incompatible_source(catalog, change):
    manifest = catalog.get("dataset-manifest", "exgentic-code@1")
    info = {"sha": manifest.source.revision, "cardData": {}}
    repo = REPO
    if change == "revision":
        info["sha"] = "b" * 40
    elif change == "repository":
        repo = "another/source"
    else:
        info["cardData"]["license"] = "different-upstream-license"
    with pytest.raises(ValueError, match="license"):
        resolve_source_license(
            info, "https://huggingface.co/api/pinned-metadata", {"licenses": {}}, repo, manifest
        )


def test_new_evidence_creates_a_new_preparation_without_rewriting_history(
    catalog, tmp_path, monkeypatch
):
    from workload_runner.datasets import public

    class Counter:
        id = "unit-test-tokenizer"
        revision = "c" * 40
        tokenizer = True

        def count(self, messages, tools=()):
            return 100

    declaration = ["Confirmation A"]

    def entries(customer, manifest, limit, cache, byte_limit, metadata):
        metadata.update(
            revisions={REPO: manifest.source.revision},
            urls={REPO: str(manifest.source.url)},
            licenses={REPO: "CDLA-Permissive-2.0"},
            license_evidence={REPO: declaration[0]},
        )
        yield from synthetic_entries(customer, limit)

    monkeypatch.setattr(public, "public_entries", entries)
    root = tmp_path / "prepared"
    first = prepare_customer(
        catalog, "code-agent@1", root, source="public", limit=3, tokenizer=Counter()
    )
    original = root / "public" / "code-agent"
    old_files = {p: p.read_bytes() for p in original.glob("*/manifest.json")}
    declaration[0] = "Confirmation B"
    second = prepare_customer(
        catalog, "code-agent@1", root, source="public", limit=3, tokenizer=Counter()
    )
    assert first.id != second.id
    assert first.artifact.sha256 == second.artifact.sha256
    assert all(path.read_bytes() == contents for path, contents in old_files.items())
