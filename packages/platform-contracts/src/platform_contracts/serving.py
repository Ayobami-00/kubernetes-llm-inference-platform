"""Reusable aggregated vLLM recipes, not upstream vLLM or llm-d manifests."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .common import (
    ContractModel,
    NonEmpty,
    PositiveInt,
    PositiveNumber,
    Slug,
    VersionedSpec,
    is_commit_pin,
    is_image_pin,
)


class ModelSource(ContractModel):
    alias: Slug
    repository: NonEmpty
    revision: NonEmpty | None = None
    tokenizer_revision: NonEmpty | None = None


class SpeculativeDecoding(ContractModel):
    method: Literal["ngram"]
    num_speculative_tokens: PositiveInt = 5
    prompt_lookup_min: PositiveInt = 2
    prompt_lookup_max: PositiveInt = 5

    @model_validator(mode="after")
    def check_lookup_range(self) -> Self:
        if self.prompt_lookup_min > self.prompt_lookup_max:
            raise ValueError("speculation lookup minimum exceeds maximum")
        return self


class EngineSettings(ContractModel):
    backend: Literal["vllm"] = "vllm"
    image: NonEmpty | None = None
    dtype: Literal["auto", "bfloat16", "float16"] = "bfloat16"
    max_model_len: PositiveInt
    max_num_seqs: PositiveInt = 8
    gpu_memory_utilization: Annotated[float, Field(strict=True, gt=0, lt=1)] = 0.8
    prefix_caching: bool = False
    chat_template_kwargs: dict[str, JsonValue] = Field(default_factory=dict)
    speculative_decoding: SpeculativeDecoding | None = None


class AggregatedTopology(ContractModel):
    kind: Literal["aggregated"] = "aggregated"
    replicas: PositiveInt = 1
    gpus_per_replica: PositiveInt = 1
    tensor_parallel_size: PositiveInt = 1
    pipeline_parallel_size: PositiveInt = 1

    @model_validator(mode="after")
    def check_parallelism(self) -> Self:
        if self.gpus_per_replica != self.tensor_parallel_size * self.pipeline_parallel_size:
            raise ValueError("GPUs per replica must match tensor × pipeline parallelism")
        return self


class GPURequirements(ContractModel):
    vendor: Literal["nvidia"] = "nvidia"
    min_vram_gib_per_gpu: PositiveNumber
    gpu_model: NonEmpty | None = None


class ServingProfile(VersionedSpec):
    draft: bool = True
    model: ModelSource
    engine: EngineSettings
    topology: AggregatedTopology = Field(default_factory=AggregatedTopology)
    hardware: GPURequirements
    worker_class: Slug
    routing_policy: Literal["load_aware", "cache_aware"] = "load_aware"

    @model_validator(mode="after")
    def require_pins_when_ready(self) -> Self:
        if not self.draft:
            if not is_image_pin(self.engine.image) or not is_commit_pin(self.model.revision):
                raise ValueError("non-draft serving profiles require image and model pins")
            if not is_commit_pin(self.model.tokenizer_revision):
                raise ValueError("non-draft serving profiles require a tokenizer pin")
        return self
