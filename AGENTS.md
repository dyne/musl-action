# Working on dyne/musl-action

This is the companion installer for dyne/musl, not the toolchain build repository.
Run Git commands here. Keep `target_arch`, `CC` and `CXX` compatible. All shipped
compilers run on x86_64 Linux, regardless of target. Python 3.12+ is required.

`action.yaml` wires the release resolver, archive cache and installer in
`scripts/install.py`. Pass action inputs through environment variables, never
interpolate them into shell source. Resolve `latest` once and download assets
from that release. Require the manifest SHA-256 match before extraction on
both cold and warm installs. Never fall back to an older release or the mirror
on errors. Preserve original archive bytes in the cache; each extraction goes
into a fresh runner directory. Export compiler paths only after validation.

Run `python3 -m unittest discover -s tests -v`. For installer, cache or metadata
changes also run `python3 tests/live.py --versions v2.5.0 latest`, and test the
historical bare `2.2.0` tag on x86_64 when changing tag/legacy behavior. Live
checks need network, CMake, pkg-config and an x86_64-v2 Linux host. Foreign
binaries are linked/inspected, not executed. CI must exercise the actual
composite action and its GitHub cache, not just Python unit tests.

Keep `scripts/relocate-metadata.py` in sync with dyne/musl's helper. It repairs
legacy package metadata after verified extraction; producer preparation must
also normalize new archives. Keep the C++ curl consumers in `tests/curl/` in
sync with dyne/musl's smoke tests. Record release tags/digests and action revision
when validating a release. Action releases are independent of toolchain releases
as long as the asset/manifest/layout contract stays compatible.
