"""Offline YAML loading and a small, typed registry of authored configurations."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .common import ContractModel, VersionedSpec
from .customers import CustomerSpec
from .datasets import DatasetManifest
from .scenarios import ScenarioSpec
from .serving import ServingProfile
from .worker_groups import WorkerGroupSpec

CONFIG_MODELS: dict[str, type[VersionedSpec]] = {
    "customer": CustomerSpec,
    "scenario": ScenarioSpec,
    "worker-group": WorkerGroupSpec,
    "serving-profile": ServingProfile,
    "dataset-manifest": DatasetManifest,
}
CONFIG_FOLDERS = {
    "customer": "configs/customers",
    "scenario": "configs/scenarios",
    "worker-group": "configs/worker-groups",
    "serving-profile": "configs/serving-profiles",
    "dataset-manifest": "datasets/manifests",
}


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Avoid silently accepting a misspelled/repeated YAML setting."""


def _unique_mapping(loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("YAML mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeySafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_document[ModelT: ContractModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        document = yaml.load(path.read_text(), Loader=UniqueKeySafeLoader)
        return model.model_validate(document)
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


@dataclass
class ContractCatalog:
    records: dict[str, dict[str, VersionedSpec]] = field(default_factory=dict)

    def add(self, kind: str, spec: VersionedSpec) -> None:
        if kind not in CONFIG_MODELS or not isinstance(spec, CONFIG_MODELS[kind]):
            raise ValueError(f"incorrect contract type for {kind}")
        bucket = self.records.setdefault(kind, {})
        if spec.ref in bucket:
            raise ValueError(f"duplicate {kind} reference: {spec.ref}")
        bucket[spec.ref] = spec

    def get(self, kind: str, ref: str):
        try:
            return self.records[kind][ref]
        except KeyError as exc:
            raise ValueError(f"missing {kind} reference: {ref}") from exc


def load_catalog(root: Path) -> ContractCatalog:
    catalog = ContractCatalog()
    for kind, folder in CONFIG_FOLDERS.items():
        paths = sorted((root / folder).glob("*.yaml"))
        if not paths:
            raise ValueError(f"missing configuration files: {root / folder}")
        for path in paths:
            catalog.add(kind, load_document(path, CONFIG_MODELS[kind]))
    from .validation import validate_catalog

    validate_catalog(catalog)
    return catalog
