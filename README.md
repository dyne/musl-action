
<div align="center">

# dyne/musl-action

</div>

<p align="center">
  <a href="https://dyne.org/musl">
    <img src="https://dyne.org/musl/musl-logo-infinity-optimized.svg" width="250">
  </a>
</p>

This action installs a verified [dyne-gcc-musl](https://github.com/dyne/musl)
release for static C/C++ cross-compilation. It runs on **x86_64 Linux** with
Python 3.12 or newer (for example, GitHub's `ubuntu-24.04` runner). The current
toolchains require an x86-64-v2 CPU. The selected architecture is the compiler's
target, not the runner's architecture.

## 🎮 Usage

```yaml
- uses: dyne/musl-action@main # Pin an audited commit SHA for reproducible builds.
  id: musl
  with:
    target_arch: x86_64
    version: v2.5.0
    # Optional archive digest pin, checked against the release's manifest too:
    # sha256: 5e2c22c32f4618c778d95f715e79f25c47bdac3589f9eba4717fb59d5697485e
- name: Build
  env:
    CC: ${{ steps.musl.outputs.CC }}
    CXX: ${{ steps.musl.outputs.CXX }}
  run: |
    "$CXX" -std=c++20 -static main.cpp -o app
```

`version` defaults to `latest`, which follows GitHub's latest published stable
release. An exact tag selects that release without fallback; historical bare
tags such as `2.2.0` are supported as written. Pin both the action commit and
toolchain release when reproducibility matters. The action does not need a code
update for every toolchain release while the archive contract stays compatible.

| Input | Default | Meaning |
| ----- | ------- | ------- |
| `target_arch` | `x86_64` | `x86_64`, `arm_64`, `arm_hf`, or `riscv_64` |
| `version` | `latest` | Exact dyne/musl release tag or `latest` |
| `sha256` | empty | Optional expected SHA-256 of the selected archive |
| `token` | `${{ github.token }}` | Token for release API requests; use empty for anonymous requests |

The action resolves the release once, then downloads the archive and
`SHA256SUMS.txt` from that same GitHub release. HTTP failures, missing assets,
ambiguous manifest entries and digest mismatches fail the installation. Tokens
are used only for release API requests, not public asset downloads. There is no
dependency on the moving files.dyne.org mirror.

Only the original compressed archive is cached. Every installation, including a
cache hit, verifies its SHA-256 before extracting to a fresh directory under
`RUNNER_TEMP`. The cache key includes installer schema, host, target, resolved
tag and digest. A corrupt cache fails clearly; delete that cache and retry.
Compilers are checked with `-dumpmachine` before publishing their paths.

Compiler binaries are added to `PATH`. `CC` and `CXX` are outputs, not global
environment variables. **Use those outputs instead of hardcoding `/opt/dyne`**:
installations now use isolated runner directories, without sudo or replacement
of another installation. For CMake, locate `gcc-musl/settings.cmake` relative
to the compiler and pass explicit `TOOLCHAIN_PREFIX` and `TARGET_PREFIX` for
current releases. [The consumer smoke test](tests/smoke.py) shows independent
compiler/sysroot/package-root configuration that also works with historical
archives whose toolchain file used a different interface. CMake/pkg-config are
host dependencies for consumers, not requirements of installation itself.

Older archives, including v2.5.0, contain build-machine paths in libcurl and
libssh2 package metadata and shell config helpers. After verifying/extracting the archive, the installer
converts those paths to relocatable paths. It preserves the
original cached archive, compiler binaries and libraries. This fixes static
curl consumers failing with `cannot find -lssh2`, `-lz` or `-lcrypto` after
relocation. Future toolchain packages normalize this metadata during preparation.

## 🎣 Outputs

The following outputs can be accessed via `${{ steps.<step-id>.outputs }}` from this action

| Name | Type | Description |
| ---- | ---- | ----------- |
| `CC` | String | Path to musl-gcc |
| `CXX` | String | Path to musl-g++ |
| `version` | String | Resolved dyne/musl release tag |
| `sha256` | String | Verified archive SHA-256 |
| `cache-hit` | String | `true` when the exact archive cache was restored |

## Maintenance and validation

```sh
python3 -m unittest discover -s tests -v
python3 tests/live.py --versions v2.5.0 latest
# Optional historical compatibility check:
python3 tests/live.py --versions 2.2.0 --targets x86_64
```

Offline tests cover release/target selection, checksum failures, cache reuse,
HTTP failures, archive extraction, compiler identity and metadata relocation.
Live checks build static C++20/thread/libcurl consumers using pkg-config and
CMake on all four targets, repeat from the verified cache, and move the installed
SDK again. x86_64 programs execute; foreign targets receive link/ELF checks.

CI checks pinned and latest releases on pull requests, pushes, manual dispatch
and a weekly schedule. dyne/musl also runs an action consumer check after
publication. Keep `scripts/relocate-metadata.py` synchronized with the same
helper in dyne/musl; changes must pass relocation tests in both repositories.
