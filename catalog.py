"""Read and safely replace the GUI package catalog."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from packaging.version import InvalidVersion, Version

CATALOG_PATH = Path(
    os.getenv(
        "CATALOG_PATH",
        str(Path(__file__).resolve().parent / "catalog" / "packages.json"),
    )
)
REQUIRED_FIELDS = {"name", "version", "full_version", "master_version"}


def validate_catalog(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("Package catalog must contain a JSON list")

    validated: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not REQUIRED_FIELDS.issubset(item):
            raise ValueError(f"Invalid package record at index {index}")
        record = {field: str(item[field]) for field in REQUIRED_FIELDS}
        if not all(record.values()):
            raise ValueError(f"Empty package field at index {index}")
        validated.append(record)
    return validated


def load_packages(path: Path | None = None) -> list[dict[str, str]]:
    catalog_path = path or CATALOG_PATH
    with catalog_path.open(encoding="utf-8") as stream:
        return validate_catalog(json.load(stream))


def find_installed_packages(name: str) -> list[dict[str, str]]:
    return [
        package
        for package in load_packages()
        if package["name"].casefold() == name.casefold()
    ]


def find_installed_package(name: str, version: str) -> dict[str, str] | None:
    # "latest" is an upstream-channel concept. packages.json only knows what
    # is installed, so it must never guess that the highest local version is
    # also the latest Bioconda/conda-forge version.
    if version.casefold() == "latest":
        return None

    matches = find_installed_packages(name)
    if not matches:
        return None

    return next(
        (package for package in matches if package["version"] == version),
        None,
    )


def _version_key(value: str) -> tuple[int, object]:
    try:
        return 1, Version(value)
    except InvalidVersion:
        return 0, value.casefold()


def find_highest_installed_package(name: str) -> dict[str, str] | None:
    matches = find_installed_packages(name)
    if not matches:
        return None
    return max(matches, key=lambda package: _version_key(package["version"]))


def publish_downloaded_catalog(
    downloaded_path: Path,
    expected_name: str,
    expected_version: str,
) -> int:
    with downloaded_path.open(encoding="utf-8") as stream:
        packages = validate_catalog(json.load(stream))

    installed = any(
        package["name"].casefold() == expected_name.casefold()
        and package["version"] == expected_version
        and package["master_version"] == "3"
        for package in packages
    )
    if not installed:
        raise ValueError(
            f"Downloaded catalog does not contain {expected_name}/{expected_version} "
            "in Gencore generation 3"
        )

    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=CATALOG_PATH.parent,
        prefix=".packages.json.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(packages, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, CATALOG_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)
        downloaded_path.unlink(missing_ok=True)

    return len(packages)
