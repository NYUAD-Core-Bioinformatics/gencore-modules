#!/share/apps/NYUAD5/miniconda/3-4.11.0/envs/python3.11/bin/python
"""Generate the GUI package catalog from the installed Gencore modules."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_OUTPUT = Path(
    "/scratch/gencore/GencoreModule_automate/catalog/packages.json"
)

MODULE_ROOTS = {
    "1": Path("/scratch/gencore/.local/easybuild/modules/all"),
    "2": Path("/scratch/gencore/.eb/2.0/modules/all"),
    "3": Path("/scratch/gencore/.eb/3.0/modules/all"),
}

IGNORED_FILENAMES = {"default", ".version", ".modulerc", ".modulerc.lua"}


def module_version(path: Path) -> str | None:
    """Return a version represented by a Tcl or Lua modulefile."""
    if path.name.startswith(".") or path.name in IGNORED_FILENAMES:
        return None
    if not (path.is_file() or path.is_symlink()):
        return None

    version = path.name[:-4] if path.name.endswith(".lua") else path.name
    return version or None


def scan_module_root(root: Path, generation: str) -> list[dict[str, str]]:
    if not root.is_dir():
        raise FileNotFoundError(
            f"Gencore/{generation} module root does not exist: {root}"
        )

    packages: list[dict[str, str]] = []
    for package_dir in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if package_dir.name.startswith(".") or not package_dir.is_dir():
            continue

        for modulefile in sorted(
            package_dir.iterdir(), key=lambda item: item.name.casefold()
        ):
            version = module_version(modulefile)
            if version is None:
                continue

            packages.append(
                {
                    "name": package_dir.name,
                    "version": version,
                    "full_version": f"{package_dir.name}/{version}",
                    "master_version": generation,
                }
            )

    return packages


def write_json_atomically(path: Path, value: object) -> None:
    """Write valid JSON and replace the destination in one filesystem operation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        with temporary_path.open(encoding="utf-8") as stream:
            json.load(stream)

        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate packages.json from the installed Gencore modules."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"catalog destination (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        packages: list[dict[str, str]] = []
        for generation, root in MODULE_ROOTS.items():
            packages.extend(scan_module_root(root, generation))

        packages.sort(
            key=lambda package: (
                package["name"].casefold(),
                package["master_version"],
                package["version"],
            )
        )
        write_json_atomically(arguments.output, packages)

        metadata = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "package_count": len(packages),
            "module_roots": {
                generation: str(root)
                for generation, root in MODULE_ROOTS.items()
            },
        }
        write_json_atomically(
            arguments.output.with_name("packages.meta.json"), metadata
        )
    except Exception as error:
        print(f"Catalog generation failed: {error}", file=sys.stderr)
        return 1

    print(f"Generated {len(packages)} records at {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
