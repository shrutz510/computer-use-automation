"""Artifacts and app packs on disk: versioned YAML files, reviewable in a pull request.

capabilities/<app>/<name>/v<N>.yaml — a registry/DB is the obvious next step, but files
diff cleanly and need no infrastructure.
"""

import json
from pathlib import Path

import yaml

from cua.artifact.schema import AppPack, CapabilityArtifact

CAPABILITY_ROOT = Path("capabilities")
APP_PACK_ROOT = Path("apps")


def _as_yaml(model: CapabilityArtifact | AppPack) -> str:
    return yaml.safe_dump(model.model_dump(mode="json", exclude_none=True), sort_keys=False, width=100)


def capability_dir(app: str, name: str, root: Path = CAPABILITY_ROOT) -> Path:
    return root / app / name


def next_version(app: str, name: str, root: Path = CAPABILITY_ROOT) -> int:
    existing = [int(p.stem[1:]) for p in capability_dir(app, name, root).glob("v*.yaml") if p.stem[1:].isdigit()]
    return max(existing, default=0) + 1


def save(artifact: CapabilityArtifact, root: Path = CAPABILITY_ROOT) -> Path:
    directory = capability_dir(artifact.capability.app.key, artifact.capability.name, root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"v{artifact.capability.version}.yaml"
    path.write_text(_as_yaml(artifact), encoding="utf-8")
    return path


def load(path: Path) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_latest(app: str, name: str, root: Path = CAPABILITY_ROOT) -> tuple[Path, CapabilityArtifact]:
    versions = sorted(capability_dir(app, name, root).glob("v*.yaml"), key=lambda p: int(p.stem[1:]))
    if not versions:
        raise FileNotFoundError(f"no artifact for {app}/{name} under {root}")
    return versions[-1], load(versions[-1])


def load_pack(app: str, root: Path = APP_PACK_ROOT) -> AppPack:
    return AppPack.model_validate(yaml.safe_load((root / f"{app}.yaml").read_text(encoding="utf-8")))


def export_json_schema(path: Path) -> Path:
    """The machine-readable contract: what a calling agent reads to understand artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(CapabilityArtifact.model_json_schema(), indent=2), encoding="utf-8")
    return path
