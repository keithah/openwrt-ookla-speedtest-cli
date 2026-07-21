import gzip
import hashlib
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_ipk as builder
from scripts.update_ookla import UpdateError


def aarch64_elf(*, machine=183, program_types=()):
    header = bytearray(64)
    header[:7] = b"\x7fELF" + bytes((2, 1, 1))
    header[18:20] = machine.to_bytes(2, "little")
    if program_types:
        header[32:40] = len(header).to_bytes(8, "little")
        header[54:56] = (56).to_bytes(2, "little")
        header[56:58] = len(program_types).to_bytes(2, "little")
    table = bytearray(56 * len(program_types))
    for index, program_type in enumerate(program_types):
        table[index * 56 : index * 56 + 4] = program_type.to_bytes(4, "little")
    return bytes(header + table)


def source_archive(
    payload=None,
    *,
    speedtest_type=tarfile.REGTYPE,
    speedtest_name="speedtest",
    tar_format=tarfile.USTAR_FORMAT,
    extra_members=True,
    filler_members=0,
):
    if payload is None:
        payload = aarch64_elf(program_types=(2,))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz", format=tar_format) as archive:
        member = tarfile.TarInfo(speedtest_name)
        member.mode = 0o700
        member.type = speedtest_type
        if tar_format == tarfile.PAX_FORMAT:
            member.pax_headers = {"comment": "pax fixture"}
        if member.isreg():
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        else:
            member.linkname = "elsewhere"
            archive.addfile(member)
        if extra_members:
            for name in ("speedtest.md", "speedtest.5"):
                contents = name.encode("ascii")
                extra = tarfile.TarInfo(name)
                extra.size = len(contents)
                archive.addfile(extra, io.BytesIO(contents))
        for index in range(filler_members):
            filler = tarfile.TarInfo(f"extra-{index}")
            archive.addfile(filler, io.BytesIO())
    return raw.getvalue()


def makefile_for(archive):
    return "\n".join(
        (
            "PKG_VERSION:=1.2.0",
            "PKG_RELEASE:=1",
            f"OOKLA_HASH_aarch64:={hashlib.sha256(archive).hexdigest()}",
            "",
        )
    )


def tar_members(gzip_data):
    if gzip_data[:2] != b"\x1f\x8b":
        raise AssertionError("not a gzip stream")
    if gzip_data[4:8] != b"\0\0\0\0":
        raise AssertionError("gzip mtime is not zero")
    raw = gzip.decompress(gzip_data)
    if raw[257:263] != b"ustar\0" or raw[263:265] != b"00":
        raise AssertionError("not a ustar archive")
    with tarfile.open(fileobj=io.BytesIO(gzip_data), mode="r:gz") as archive:
        return archive.getmembers(), {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }


class BuildIpkTest(unittest.TestCase):
    def setUp(self):
        self.archive = source_archive()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.makefile = self.root / "Makefile"
        self.makefile.write_text(makefile_for(self.archive), encoding="utf-8")
        self.output = self.root / "output"

    def test_builds_deterministic_ipk_with_exact_metadata_and_payload(self):
        first = builder.build_ipk(self.makefile, self.output, self.archive)
        first_bytes = first.read_bytes()
        second_bytes = builder.build_ipk(
            self.makefile, self.output, source_archive()
        ).read_bytes()

        self.assertEqual(
            "ookla-speedtest-cli_1.2.0-1_aarch64_cortex-a53.ipk",
            first.name,
        )
        self.assertEqual(first_bytes, second_bytes)
        outer_members, outer_files = tar_members(first_bytes)
        self.assertEqual(
            ["./debian-binary", "./control.tar.gz", "./data.tar.gz"],
            [member.name for member in outer_members],
        )
        self.assertEqual(b"2.0\n", outer_files["./debian-binary"])

        listed = subprocess.run(
            ("tar", "-tzf", first),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            "./debian-binary\n./control.tar.gz\n./data.tar.gz\n",
            listed.stdout,
        )

        control_members, control_files = tar_members(
            outer_files["./control.tar.gz"]
        )
        self.assertEqual(["./control"], [member.name for member in control_members])
        self.assertEqual(
            "Package: ookla-speedtest-cli\n"
            "Version: 1.2.0-1\n"
            "Architecture: aarch64_cortex-a53\n"
            "License: Proprietary\n",
            control_files["./control"].decode("utf-8"),
        )

        data_members, data_files = tar_members(outer_files["./data.tar.gz"])
        self.assertEqual(
            ["./usr/bin/speedtest"], [member.name for member in data_members]
        )
        self.assertEqual(0o755, data_members[0].mode)
        self.assertEqual(
            aarch64_elf(program_types=(2,)), data_files["./usr/bin/speedtest"]
        )

    def test_downloads_the_identified_archive_only_when_not_injected(self):
        with mock.patch.object(
            builder, "_download", return_value=self.archive
        ) as download:
            builder.build_ipk(self.makefile, self.output)
        download.assert_called_once_with(
            "https://install.speedtest.net/app/cli/"
            "ookla-speedtest-1.2.0-linux-aarch64.tgz"
        )

        with mock.patch.object(builder, "_download") as download:
            builder.build_ipk(self.makefile, self.output, self.archive)
        download.assert_not_called()

    def test_rejects_checksum_mismatch(self):
        self.makefile.write_text(
            makefile_for(self.archive).replace(
                hashlib.sha256(self.archive).hexdigest(), "0" * 64
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(UpdateError, "checksum"):
            builder.build_ipk(self.makefile, self.output, self.archive)

    def test_declares_bounded_input_limits(self):
        self.assertEqual(
            16 * 1024 * 1024, getattr(builder, "MAX_ARCHIVE_BYTES", None)
        )
        self.assertEqual(64, getattr(builder, "MAX_SOURCE_MEMBERS", None))
        self.assertEqual(
            16 * 1024 * 1024, getattr(builder, "MAX_EXECUTABLE_BYTES", None)
        )

    def test_rejects_oversized_compressed_archive_before_validation(self):
        archive = b"x" * (16 * 1024 * 1024 + 1)
        self.makefile.write_text(makefile_for(archive), encoding="utf-8")
        with mock.patch.object(builder, "archive_sha256") as digest, mock.patch.object(
            builder, "validate_archive"
        ) as validator:
            with self.assertRaisesRegex(UpdateError, "compressed archive"):
                builder.build_ipk(self.makefile, self.output, archive)
        digest.assert_not_called()
        validator.assert_not_called()

    def test_rejects_too_many_source_members_before_validation(self):
        archive = source_archive(filler_members=62)
        self.makefile.write_text(makefile_for(archive), encoding="utf-8")
        with mock.patch.object(builder, "validate_archive") as validator:
            with self.assertRaisesRegex(UpdateError, "member count"):
                builder.build_ipk(self.makefile, self.output, archive)
        validator.assert_not_called()

    def test_rejects_oversized_executable_before_validation(self):
        archive = source_archive(payload=b"x" * (16 * 1024 * 1024 + 1))
        self.makefile.write_text(makefile_for(archive), encoding="utf-8")
        with mock.patch.object(builder, "validate_archive") as validator:
            with self.assertRaisesRegex(UpdateError, "executable"):
                builder.build_ipk(self.makefile, self.output, archive)
        validator.assert_not_called()

    def test_rejects_non_aarch64_elf(self):
        archive = source_archive(aarch64_elf(machine=40))
        self.makefile.write_text(makefile_for(archive), encoding="utf-8")
        with self.assertRaisesRegex(UpdateError, "machine"):
            builder.build_ipk(self.makefile, self.output, archive)

    def test_rejects_pt_interp(self):
        archive = source_archive(aarch64_elf(program_types=(2, 3)))
        self.makefile.write_text(makefile_for(archive), encoding="utf-8")
        with self.assertRaisesRegex(UpdateError, "interpreter"):
            builder.build_ipk(self.makefile, self.output, archive)

    def test_rejects_pax_source_archive(self):
        archive = source_archive(tar_format=tarfile.PAX_FORMAT)
        self.makefile.write_text(makefile_for(archive), encoding="utf-8")
        with self.assertRaisesRegex(UpdateError, "pax"):
            builder.build_ipk(self.makefile, self.output, archive)

    def test_rejects_missing_nonregular_or_wrong_speedtest_member(self):
        cases = (
            source_archive(speedtest_name="not-speedtest"),
            source_archive(speedtest_type=tarfile.SYMTYPE),
            source_archive(speedtest_name="./speedtest"),
        )
        for archive in cases:
            with self.subTest():
                self.makefile.write_text(makefile_for(archive), encoding="utf-8")
                with self.assertRaises(UpdateError):
                    builder.build_ipk(self.makefile, self.output, archive)

    def test_rejects_duplicate_or_missing_required_makefile_assignments(self):
        original = makefile_for(self.archive)
        for name in ("PKG_VERSION", "PKG_RELEASE", "OOKLA_HASH_aarch64"):
            assignment = next(
                line for line in original.splitlines() if line.startswith(name)
            )
            for malformed in (
                original.replace(assignment + "\n", ""),
                original + assignment + "\n",
                original + f"{name}:=malformed\n",
            ):
                with self.subTest(name=name):
                    self.makefile.write_text(malformed, encoding="utf-8")
                    with self.assertRaisesRegex(UpdateError, "exactly one"):
                        builder.build_ipk(self.makefile, self.output, self.archive)

    def test_staged_write_failure_preserves_existing_package_and_cleans_up(self):
        destination = self.output / (
            "ookla-speedtest-cli_1.2.0-1_aarch64_cortex-a53.ipk"
        )
        self.output.mkdir()
        destination.write_bytes(b"existing package")

        def fail_after_partial_write(path, contents):
            path.write_bytes(contents[:10])
            raise OSError("controlled staged write failure")

        with mock.patch.object(
            builder,
            "_write_staged_ipk",
            create=True,
            side_effect=fail_after_partial_write,
        ):
            with self.assertRaisesRegex(UpdateError, "controlled staged write failure"):
                builder.build_ipk(self.makefile, self.output, self.archive)

        self.assertEqual(b"existing package", destination.read_bytes())
        self.assertEqual([destination], list(self.output.iterdir()))


if __name__ == "__main__":
    unittest.main()
