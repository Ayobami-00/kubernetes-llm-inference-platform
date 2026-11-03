"""Manifests describe public sources and future preparation; they contain no corpus data."""

from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator

from .common import (
    ArtifactURI,
    ContractModel,
    NonEmpty,
    PositiveInt,
    Sha256,
    VersionedSpec,
)


class DatasetSource(ContractModel):
    url: HttpUrl
    revision: NonEmpty | None = None
    subset: NonEmpty
    license: NonEmpty | None = None
    filters: dict[str, str] = Field(default_factory=dict)


class PreparedDataset(ContractModel):
    uri: ArtifactURI
    sha256: Sha256
    record_count: PositiveInt
    tokenizer_revision: NonEmpty
    prompt_template_sha256: Sha256
    max_rendered_input_tokens: PositiveInt


class DatasetManifest(VersionedSpec):
    source: DatasetSource
    format: Literal["custom_single_turn", "custom_multi_turn", "exgentic_snapshot"]
    interaction: Literal["independent_requests", "session"]
    references: Literal[
        "multiple_corrections",
        "dialogue_annotations",
        "clause_spans",
        "answers_and_supporting_facts",
        "not_available",
    ]
    timing_provenance: Literal["experimental_assumption", "recorded_trace", "not_provided"]
    preparation: tuple[NonEmpty, ...]
    limitations: tuple[NonEmpty, ...]
    prepared: PreparedDataset | None = None

    @model_validator(mode="after")
    def compatible_format(self) -> Self:
        expected = "independent_requests" if self.format == "custom_single_turn" else "session"
        if self.interaction != expected:
            raise ValueError("dataset format and interaction disagree")
        return self
