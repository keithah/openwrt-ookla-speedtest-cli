#!/usr/bin/env python3
"""Build a deterministic aarch64 OpenWrt IPK from the pinned Ookla archive."""

import gzip
import io
import os
from pathlib import Path
import re
import tarfile
import tempfile

from scripts.update_ookla import (
    UpdateError,
    _download,
    archive_sha256,
    validate_archive,
)


ARCHITECTURE = "aarch64_cortex-a53"
PACKAGE = "ookla-speedtest-cli"
SOURCE_URL = "https://install.speedtest.net/app/cli/"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_MEMBERS = 64
MAX_EXECUTABLE_BYTES = 16 * 1024 * 1024


def _assignment(recipe, name, value_pattern):
    assignments = re.findall(
        rf"(?m)^{re.escape(name)}[ \t]*:=[ \t]*([^\r\n]*)$",
        recipe,
    )
    if len(assignments) != 1:
        raise UpdateError(f"expected exactly one valid {name} assignment")
    match = re.fullmatch(rf"({value_pattern})[ \t]*", assignments[0])
    if match is None:
        raise UpdateError(f"expected exactly one valid {name} assignment")
    return match.group(1)


def _read_recipe(makefile):
    try:
        recipe = makefile.read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError(f"failed to read {makefile}: {error}") from error
    version = _assignment(recipe, "PKG_VERSION", r"\d+\.\d+\.\d+")
    release = _assignment(recipe, "PKG_RELEASE", r"\d+")
    checksum = _assignment(recipe, "OOKLA_HASH_aarch64", r"[0-9a-f]{64}")
    return version, release, checksum


def _check_archive_size(archive):
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise UpdateError("compressed archive exceeds maximum size")


def _preflight_source_archive(archive):
    _check_archive_size(archive)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            member_count = 0
            speedtest_count = 0
            for member in source:
                member_count += 1
                if member_count > MAX_SOURCE_MEMBERS:
                    raise UpdateError("source archive member count exceeds maximum")
                if member.pax_headers:
                    raise UpdateError("source archive contains pax members")
                if member.size > MAX_EXECUTABLE_BYTES:
                    if member.name == "speedtest":
                        raise UpdateError("source executable exceeds maximum size")
                    raise UpdateError("source archive member exceeds maximum size")
                if member.name == "speedtest":
                    speedtest_count += 1
                    if not member.isfile():
                        raise UpdateError(
                            "source archive speedtest member is not a regular file"
                        )
            if speedtest_count != 1:
                raise UpdateError(
                    "source archive must contain exactly one regular speedtest member"
                )
    except UpdateError:
        raise
    except (tarfile.TarError, EOFError, OSError) as error:
        raise UpdateError(f"invalid archive: {error}") from error


def _source_executable(archive):
    _preflight_source_archive(archive)
    validate_archive("aarch64", archive)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            member = next(
                candidate for candidate in source if candidate.name == "speedtest"
            )
            extracted = source.extractfile(member)
            if extracted is None:
                raise UpdateError("source archive speedtest member cannot be read")
            executable = extracted.read(MAX_EXECUTABLE_BYTES + 1)
            if len(executable) > MAX_EXECUTABLE_BYTES:
                raise UpdateError("source executable exceeds maximum size")
            return executable
    except UpdateError:
        raise
    except (StopIteration, tarfile.TarError, EOFError, OSError) as error:
        raise UpdateError(f"invalid archive: {error}") from error


def _gzip_ustar(members):
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as stream:
        with tarfile.open(
            fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
            for name, contents, mode in members:
                member = tarfile.TarInfo(name)
                member.size = len(contents)
                member.mode = mode
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                archive.addfile(member, io.BytesIO(contents))
    return compressed.getvalue()


def _render_ipk(version, release, executable):
    control = (
        f"Package: {PACKAGE}\n"
        f"Version: {version}-{release}\n"
        f"Architecture: {ARCHITECTURE}\n"
        "License: Proprietary\n"
    ).encode("utf-8")
    control_archive = _gzip_ustar((("./control", control, 0o644),))
    data_archive = _gzip_ustar((("./usr/bin/speedtest", executable, 0o755),))
    return _gzip_ustar(
        (
            ("./debian-binary", b"2.0\n", 0o644),
            ("./control.tar.gz", control_archive, 0o644),
            ("./data.tar.gz", data_archive, 0o644),
        )
    )


def _write_staged_ipk(path, contents):
    path.write_bytes(contents)


def build_ipk(makefile: Path, output_dir: Path, archive: bytes | None = None) -> Path:
    """Build and atomically publish the pinned aarch64 IPK."""
    makefile = Path(makefile)
    output_dir = Path(output_dir)
    version, release, checksum = _read_recipe(makefile)
    if archive is None:
        archive = _download(
            f"{SOURCE_URL}ookla-speedtest-{version}-linux-aarch64.tgz",
            max_bytes=MAX_ARCHIVE_BYTES,
        )
    _check_archive_size(archive)
    if archive_sha256(archive) != checksum:
        raise UpdateError("aarch64 source archive checksum mismatch")
    executable = _source_executable(archive)
    package = _render_ipk(version, release, executable)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / (
            f"{PACKAGE}_{version}-{release}_{ARCHITECTURE}.ipk"
        )
        with tempfile.TemporaryDirectory(dir=output_dir) as staging:
            temporary = Path(staging) / destination.name
            _write_staged_ipk(temporary, package)
            os.replace(temporary, destination)
        return destination
    except OSError as error:
        raise UpdateError(f"failed to write IPK: {error}") from error
