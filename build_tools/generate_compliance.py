# -*- coding: utf-8 -*-
"""Generate dependency lock, CycloneDX SBOM and licence manifest.

The script is deliberately offline: it reads the active Python environment,
copies licence texts that are actually installed, and verifies checked-in
supplemental/manual declarations.  Missing evidence keeps ``complete=false``
and returns a non-zero status so a public release cannot proceed by accident.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "LICENSES"
DEFAULT_DISTRIBUTION_SCOPE = {
    "mode": "source-bootstrap",
    "description": (
        "默认 GitHub Release ZIP 分发项目 MIT 源码和自动安装器；"
        "首次运行由用户机器从上游来源安装 Python/PyPI 依赖和可选组件。"
    ),
    "included": [
        "MIT-licensed project source",
        "bootstrap installers and Premiere extension source",
        "documentation and project-maintained knowledge-base files",
    ],
    "excluded": [
        "智能剪辑助手.exe",
        "_internal/",
        ".venv/",
        "runtime/",
        "bin/",
        "models/",
    ],
    "offline_binary_release_gate": (
        "LICENSES/manifest.json complete=true is required before publishing "
        "an EXE/_internal or other bundled native-runtime release."
    ),
}
LICENCE_NAMES = re.compile(
    r"^(?:licen[cs]e|copying|notice|third[-_ ]?party|authors?)(?:[._-].*)?$",
    re.I,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-").lower()


def _canonical_package_name(value: str) -> str:
    """Normalize a distribution name using the PEP 503 comparison form."""
    return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()


def _installed_licences(distribution, name: str):
    copied = []
    destination = LICENSES / "python" / _safe_name(name)
    for relative in distribution.files or ():
        if not LICENCE_NAMES.match(Path(str(relative)).name):
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / source.name
        shutil.copy2(source, target)
        copied.append(target.relative_to(ROOT).as_posix())
    return sorted(set(copied))


def load_python_overrides(root: Path, licences: Path, missing: list[dict]):
    """Load version-pinned supplemental licence evidence.

    Overrides never fetch content.  Every checked-in licence file must remain
    inside ``root`` and match its declared SHA256; otherwise the row is
    rejected and an explicit missing-evidence record is appended.
    """
    result = {}
    root = Path(root).resolve()
    licences = Path(licences)
    data = _read_json(licences / "python_overrides.json", {})
    for row in data.get("components") or []:
        if not isinstance(row, dict):
            continue
        name = _canonical_package_name(row.get("name"))
        version = str(row.get("version") or "").strip()
        label = "%s==%s" % (name or "<unknown>", version or "<unknown>")
        source_hash = str(row.get("source_sha256") or "")
        if (not name or not version or not row.get("source_url")
                or not re.fullmatch(r"[0-9a-fA-F]{64}", source_hash)):
            missing.append({"name": label,
                            "reason": "override 缺少版本固定的来源或 source SHA256"})
            continue
        files = []
        for relative in row.get("license_files") or []:
            path = (root / str(relative)).resolve()
            expected = (row.get("file_sha256") or {}).get(relative)
            if (not path.is_relative_to(root) or not path.is_file()
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected or ""))):
                missing.append({"name": label,
                                "reason": "许可证文件缺失、越界或没有 SHA256：%s"
                                          % relative})
                files = []
                break
            if _sha256_bytes(path.read_bytes()) != expected:
                missing.append({"name": label,
                                "reason": "许可证文件 SHA256 不匹配：%s" % relative})
                files = []
                break
            files.append(str(relative))
        if not files:
            if not (row.get("license_files") or []):
                missing.append({"name": label,
                                "reason": "override 没有许可证正文"})
            continue
        result[(name, version)] = files
    return result


def load_manual_components(root: Path, licences: Path, missing: list[dict]):
    """Load native/manual components without hiding unresolved licence work."""
    root = Path(root).resolve()
    licences = Path(licences)
    data = _read_json(licences / "manual_components.json", {})
    components = []
    for row in data.get("components") or []:
        if not isinstance(row, dict):
            continue
        value = dict(row)
        files = list(value.get("license_files") or [])
        value["license_files"] = files
        hashes = value.get("file_sha256") or {}
        valid = bool(value.get("complete")) and bool(files)
        for relative in files:
            path = (root / str(relative)).resolve()
            expected = hashes.get(relative)
            if (not path.is_relative_to(root) or not path.is_file() or not expected
                    or _sha256_bytes(path.read_bytes()) != expected):
                valid = False
        value["complete"] = bool(valid)
        components.append(value)
        if not valid:
            missing.append({"name": value.get("name"),
                            "reason": value.get("reason") or "manual evidence incomplete"})
    return components


def _verified_overrides():
    missing = []
    return load_python_overrides(ROOT, LICENSES, missing)


def _manual_components():
    missing = []
    components = load_manual_components(ROOT, LICENSES, missing)
    return components, missing


def generate() -> bool:
    LICENSES.mkdir(exist_ok=True)
    overrides = _verified_overrides()
    components = []
    missing = []
    lock_lines = [
        "# Generated from the active build environment.",
        "# Exact installed versions; metadata hashes are provenance only.",
    ]
    for distribution in sorted(metadata.distributions(),
                               key=lambda item: (item.metadata.get("Name") or "").lower()):
        name = str(distribution.metadata.get("Name") or "").strip()
        if not name:
            continue
        version = str(distribution.version or "")
        metadata_text = distribution.read_text("METADATA") or ""
        metadata_hash = _sha256_bytes(metadata_text.encode("utf-8"))
        files = _installed_licences(distribution, name)
        if not files:
            files = list(overrides.get((name.lower(), version), ()))
        row = {
            "name": name,
            "version": version,
            "type": "python-distribution",
            "declared_license": str(distribution.metadata.get("License-Expression")
                                    or distribution.metadata.get("License") or ""),
            "license_files": files,
            "installed_metadata_sha256": metadata_hash,
        }
        components.append(row)
        lock_lines.append("%s==%s  # installed-metadata-sha256=%s" %
                          (name, version, metadata_hash))
        if not files:
            missing.append({"name": name, "version": version,
                            "reason": "installed distribution has no verified licence text"})

    manual, manual_missing = _manual_components()
    components.extend(manual)
    missing.extend(manual_missing)
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema": 1,
        "generated_at": generated_at,
        "generator": "build_tools/generate_compliance.py",
        "distribution_scope": DEFAULT_DISTRIBUTION_SCOPE,
        "complete": not missing,
        "components": components,
        "missing": missing,
    }
    sbom_components = []
    for row in components:
        name = str(row.get("name") or "")
        version = str(row.get("version") or "")
        item = {"type": "library", "name": name, "version": version}
        if row.get("type") == "python-distribution":
            normalized = _canonical_package_name(name)
            item["purl"] = "pkg:pypi/%s@%s" % (normalized, version)
            item["bom-ref"] = item["purl"]
        sbom_components.append(item)
    sbom = {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {"timestamp": generated_at},
        "components": sbom_components,
    }
    (ROOT / "requirements.lock").write_text(
        "\n".join(lock_lines) + "\n", encoding="utf-8", newline="\n")
    (LICENSES / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (ROOT / "SBOM.cdx.json").write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("components=%d missing=%d complete=%s" %
          (len(components), len(missing), not missing))
    return not missing


if __name__ == "__main__":
    raise SystemExit(0 if generate() else 2)
