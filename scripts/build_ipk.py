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


def _source_executable(archive):
    validate_archive("aarch64", archive)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            members = source.getmembers()
            if any(member.pax_headers for member in members):
                raise UpdateError("source archive contains pax members")
            matching = [member for member in members if member.name == "speedtest"]
            if len(matching) != 1 or not matching[0].isfile():
                raise UpdateError(
                    "source archive must contain exactly one regular speedtest member"
                )
            extracted = source.extractfile(matching[0])
            if extracted is None:
                raise UpdateError("source archive speedtest member cannot be read")
            return extracted.read()
    except UpdateError:
        raise
    except (tarfile.TarError, EOFError, OSError) as error:
        raise UpdateError(f"invalid archive: {error}") from error


def _tar_gzip(name, contents, mode):
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as stream:
        with tarfile.open(
            fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT
        ) as archive:
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


def _ar_member(name, contents, mode=0o100644):
    encoded_name = name.encode("ascii")
    if len(encoded_name) > 16:
        raise UpdateError(f"ar member name is too long: {name}")
    header = b"".join(
        (
            encoded_name.ljust(16),
            b"0".ljust(12),
            b"0".ljust(6),
            b"0".ljust(6),
            format(mode, "o").encode("ascii").ljust(8),
            str(len(contents)).encode("ascii").ljust(10),
            b"`\n",
        )
    )
    return header + contents + (b"\n" if len(contents) % 2 else b"")


def _render_ipk(version, release, executable):
    control = (
        f"Package: {PACKAGE}\n"
        f"Version: {version}-{release}\n"
        f"Architecture: {ARCHITECTURE}\n"
        "License: Proprietary\n"
    ).encode("utf-8")
    members = (
        ("./debian-binary", b"2.0\n"),
        ("./control.tar.gz", _tar_gzip("./control", control, 0o644)),
        ("./data.tar.gz", _tar_gzip("./usr/bin/speedtest", executable, 0o755)),
    )
    return b"!<arch>\n" + b"".join(_ar_member(name, data) for name, data in members)


def build_ipk(makefile: Path, output_dir: Path, archive: bytes | None = None) -> Path:
    """Build and atomically publish the pinned aarch64 IPK."""
    makefile = Path(makefile)
    output_dir = Path(output_dir)
    version, release, checksum = _read_recipe(makefile)
    if archive is None:
        archive = _download(
            f"{SOURCE_URL}ookla-speedtest-{version}-linux-aarch64.tgz"
        )
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
            temporary.write_bytes(package)
            os.replace(temporary, destination)
        return destination
    except OSError as error:
        raise UpdateError(f"failed to write IPK: {error}") from error
