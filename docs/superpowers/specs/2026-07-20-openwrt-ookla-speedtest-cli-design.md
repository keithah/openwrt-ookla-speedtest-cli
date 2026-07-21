# OpenWrt Ookla Speedtest CLI Package Design

## Goal

Create a public, source-only GitHub repository named
`openwrt-ookla-speedtest-cli` that packages Ookla Speedtest CLI as an OpenWrt
`.ipk` for Linux ARM targets. The repository must not contain Ookla binaries or
release archives.

## Scope

The package supports exactly these official Ookla Linux archives:

| Vendor suffix | OpenWrt target | Initial version |
| --- | --- | --- |
| `linux-aarch64` | 64-bit ARM | `1.2.0` |
| `linux-armhf` | 32-bit ARM using the hard-float ABI | `1.2.0` |
| `linux-armel` | 32-bit ARM using the soft-float ABI | `1.2.0` |

FreeBSD, macOS, Windows, i386, x86_64, and other vendor artifacts are outside
the project scope. The repository maintains an OpenWrt package recipe; it does
not publish or commit vendor binaries, vendor archives, or prebuilt `.ipk`
files.

## Repository Layout

- `Makefile` defines the external-feed OpenWrt package.
- `README.md` documents supported targets, OpenWrt build/install steps,
  licensing behavior, and the update workflow.
- `LICENSE` licenses only this repository's packaging and automation code.
- `scripts/update_ookla.py` discovers and applies upstream version updates.
- `tests/` contains dependency-free updater and recipe tests with local
  fixtures.
- `.github/workflows/update-ookla.yml` checks for releases daily and supports
  manual dispatch.

## OpenWrt Package Mechanics

The package is named `ookla-speedtest-cli` and installs the command as
`/usr/bin/speedtest`. A single recipe maps the OpenWrt build target to one of
the three vendor suffixes. It distinguishes 32-bit ARM hard-float and
soft-float builds using the OpenWrt configuration's float-ABI setting.
Unsupported architectures are not selectable.

OpenWrt's standard download phase fetches the selected archive from
`https://install.speedtest.net/app/cli/`. The recipe supplies a distinct,
pinned SHA-256 checksum for every supported archive. The standard preparation
phase extracts the archive, and the install phase copies only the executable
into the package root. Source archives may exist in an OpenWrt builder's normal
download cache but never in this Git repository.

The three version 1.2.0 executables have been inspected and are statically
linked ELF binaries. They therefore do not depend on OpenWrt's musl runtime.
Each upstream archive has the same top-level layout: `speedtest`,
`speedtest.md`, and `speedtest.5`.

The recipe identifies the packaged application as proprietary and links to
Ookla's official CLI and EULA information. The repository's own license does
not relicense or claim ownership of Ookla's software. License acceptance stays
with the CLI's normal first-run behavior.

## Update Automation

The updater uses only the Python standard library. It fetches Ookla's official
Speedtest CLI page, extracts release URLs, and selects the highest semantic
version for which all three required Linux ARM archives are present. It does
not probe or track any non-Linux or non-ARM artifact.

When the discovered version is newer than `PKG_VERSION`, the updater:

1. Downloads all three release archives to temporary runner storage.
2. Verifies that each archive contains a `speedtest` executable.
3. Verifies the executable's ELF class and ARM machine type against the
   expected architecture.
4. Computes the three SHA-256 checksums.
5. Updates `PKG_VERSION`, resets `PKG_RELEASE` to `1`, and replaces the checksum
   table atomically.
6. Runs all repository tests.
7. Commits the text-only recipe change directly to `main` using the GitHub
   Actions bot identity.

The workflow runs daily and through `workflow_dispatch`, has `contents: write`
permission, and uses a concurrency group to prevent update races. An unchanged
version exits successfully without a commit.

The updater fails without changing the repository when the official page is
malformed, any required architecture is missing, versions disagree, a download
fails, archive or ELF validation fails, the recipe cannot be updated exactly
once, or tests fail. The workflow never commits a partial update.

## Validation

Dependency-free unit tests use local fixtures and cover:

- discovery of a newer complete three-architecture release;
- selection of the highest complete semantic version;
- same-version no-op behavior;
- rejection of incomplete architecture sets and malformed versions;
- calculation and insertion of architecture-specific checksums;
- resetting `PKG_RELEASE` when the upstream version changes;
- preservation of unrelated Makefile content; and
- expected architecture selection for aarch64, ARM hard-float, and ARM
  soft-float OpenWrt configurations.

The live update path performs the archive and ELF checks before modifying the
recipe. Repository verification runs on updater changes as well as immediately
before an automated version commit.

## Documentation and User Experience

The README explains how to clone the repository beneath an OpenWrt source
tree's `package/` directory, select `Utilities → ookla-speedtest-cli`, build the
package, install the generated `.ipk` with `opkg`, and run `speedtest`. It calls
out the first-run Ookla license prompt and clearly labels the project as an
unofficial packaging recipe.

## Success Criteria

- A supported OpenWrt ARM build downloads the correct official archive and
  rejects a checksum mismatch.
- The resulting `.ipk` contains `/usr/bin/speedtest` and no incorrect-ABI
  binary.
- The Git repository contains no Ookla executable, archive, or generated
  `.ipk`.
- A complete newer Ookla Linux ARM release updates the recipe and is committed
  directly to `main` after validation.
- Incomplete or invalid upstream state produces a visible failed workflow and
  no repository change.
