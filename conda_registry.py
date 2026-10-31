"""Approved Anaconda channel validation for automated requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


APPROVED_CHANNELS = ("bioconda", "conda-forge")
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")


class RegistryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CondaPackage:
    name: str
    version: str
    channel: str


def preferred_channels(conda_url: str | None) -> list[str]:
    if not conda_url:
        return list(APPROVED_CHANNELS)

    parsed = urlparse(conda_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.casefold() == "anaconda.org" and parts:
        channel = parts[0].casefold()
        if channel in APPROVED_CHANNELS:
            return [channel] + [item for item in APPROVED_CHANNELS if item != channel]
    return list(APPROVED_CHANNELS)


def resolve_conda_package(
    name: str,
    requested_version: str,
    conda_url: str | None = None,
    timeout: float = 15,
) -> CondaPackage | None:
    name = name.strip()
    requested_version = (requested_version or "latest").strip()
    if not PACKAGE_PATTERN.fullmatch(name):
        return None
    if (
        requested_version.casefold() != "latest"
        and not VERSION_PATTERN.fullmatch(requested_version)
    ):
        return None

    for channel in preferred_channels(conda_url):
        url = f"https://api.anaconda.org/package/{channel}/{name}"
        try:
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as error:
            raise RegistryUnavailable(str(error)) from error

        if response.status_code == 404:
            continue
        try:
            response.raise_for_status()
            package = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RegistryUnavailable(str(error)) from error

        versions = {str(value) for value in package.get("versions", [])}
        if requested_version.casefold() == "latest":
            resolved_version = str(package.get("latest_version") or "").strip()
            if not resolved_version:
                continue
        elif requested_version in versions:
            resolved_version = requested_version
        else:
            continue

        return CondaPackage(
            name=name,
            version=resolved_version,
            channel=channel,
        )

    return None
