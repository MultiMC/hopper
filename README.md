# hopper

MultiMC's build and release automation, which replaced the TeamCity jobs: build, package,
verify, deploy into a local copy of the files tree, publish to the live tree and a bucket;
also translations, the Patreon credits, the website and meta.

The toolchain and tool images it runs are built from the
[Toolchains](https://github.com/MultiMC/Toolchains) repo (`containers/`), which also holds
the raw captures of the old build machines (`snapshots/`). This repo has no large files.

## Layout

One directory per job, plus hopper itself and what they share. Every script is one
directory down and is run from the top of the checkout, e.g.
`launcher/release-build.py --config config/files2.env`.

| Directory | What |
|-----------|------|
| `daemon/` | What runs the jobs, and the views. `trigger.py` starts Laminar's jobs on new commits and schedules (`cron.py`: the schedules); `releases.py` lists and cleans up the releases in `RELEASES_DIR`; `overview.py` gathers what the setups have live and what Laminar is doing, as JSON, for `web/`; `builds.py` lists a setup's builds, and checks and queues moving tags, withdrawing and pruning; `runs.py` cancels queued runs |
| `web/` | The view of all this in the browser, with Laminar's logs, the actions on builds, and `/api/overview`: `hopper-web`, in Go with only its standard library |
| `launcher/` | The Launcher: `release-build.py` builds a release of all platforms (checkout, parallel builds, packaging, compatibility gate, manifest); `run-build.py` builds one platform in its toolchain container; `steps/` holds the build steps run inside the containers (`linux.sh`, `win32.sh` for both Windows platforms, `osx64.sh`, mounted at `/build`); `compat-check.py` is the compatibility gate, checking a build against its description in `expectations/`; `dev-run.py` and `dev/` run CMake and make in a toolchain container for an IDE |
| `translations/` | `build.py` builds a translations release (the Translations repo's `release.py`, in its image); `template.py` updates the translations template from the Launcher source and pushes it |
| `patrons/` | `update.py` makes the Patreon credits (`patrons.txt`) as a release |
| `website/` | `build.py` builds the website (multimc.org) from the Website repo with Hugo, as a release; `publish.py` publishes it to the test site, or promotes the one on test to production |
| `files/` | The files tree (files.multimc.org), shared by the Launcher, translations and Patreon releases: `deploy.py` deploys a release into the local tree; `publish.py` publishes what changed since the last publish to the live tree and the bucket and purges Cloudflare's cache; `pull.py` makes the local tree a copy of the live one; `snapshots.py` lists, takes, restores and removes the tree's snapshots |
| `updater/` | The new updater's tree (the Design repo's `UpdaterFormats.md`): `deploy.py` deploys a Launcher release into it (the new layout's publisher); `tag.py`, `remove.py` and `prune.py` move the Testing and Stable tags, remove a build and keep the tree under its size cap; `validate.py` checks a document or a whole tree against the format; `test_*.py` their tests |
| `keys/` | The new updater's signing keys, one script per step of the offline key ceremony: `make-key.py` makes a key pair, `write-keys.py` writes a keys document, `sign.py` signs a document, `verify.py` checks keys documents and signed documents; `test_keys.py` their tests |
| `laminar/` | Laminar, which runs the jobs: `cfg/`, its config (`jobs/<job>.run` and `.conf`, `contexts/`), linked as `~/laminar/cfg`; `build.sh` and `Containerfile` build its binaries, linked against nothing but glibc |
| `lib/` | `common.py`, shared by the scripts: settings, platforms, the container command, locking, the files tree; `updater_formats.py`, the new updater's formats: checking, signing, the keys chain; `updater_tree.py`, a setup's new updater tree: new documents signed, the tree checked as it would be, then written in the format's order |
| `config/` | The settings: `defaults.env` (every setting, with its default), `production.env` (none: the defaults as they are; there so every setup has a name, for Laminar's jobs `<stream>-<setup>`), `files2.env` (the files2 test system); `keys/<setup>/`, each setup's signed keys documents (public keys only) |
| `host/` | What is installed on other machines, by machine. `builder/`: the build server's systemd user units, `laminar.service` (Laminar), `trigger.service` (`daemon/trigger.py`, which starts Laminar's jobs) and `web.service` (`web/`). `fileserver/`: `hopper-ssh`, the only things hopper's SSH key may do on the files server |

### State

What the scripts keep between runs is outside the checkout, in `$STATE`:
`$XDG_STATE_HOME/hopper`, by default `~/.local/state/hopper` (`lib/common.py`). It holds the
repo mirrors, the incremental work trees, the releases, the build counters, the retired
daemon's records, and the local files trees (btrfs subvolumes). Settings may use `$STATE`;
the paths below written as `$STATE/...` are its defaults. Nothing in it is in git.

## Principles

No black boxes: every part runs on its own and its results can be inspected; the pipeline
fails closed (stops and lets nothing through to users); intermediate results are
versioned, so the real system can be debugged in place and any mess undone. How this
automation measures up:

- **Runnable parts:** each step is its own script (`launcher/run-build.py`, `launcher/compat-check.py`,
  `launcher/release-build.py`, `files/deploy.py`, `updater/deploy.py`, `files/publish.py`, `files/pull.py`);
  `updater/deploy.py`, `files/publish.py` and `files/pull.py` have `--dry-run`.
- **Inspectable results:** a release directory keeps the source, build trees, logs,
  packages, deploy archives, gate reports and `manifest.json`.
- **Fails closed:** a failed build or test stops the release; the gate refuses anything
  its description (`launcher/expectations/`) doesn't allow; `files/deploy.py` refuses releases that aren't
  releasable, were built for another system, or aren't newer; `updater/deploy.py` writes nothing
  unless the new updater's tree as it would be passes the validator; `files/publish.py` refuses when
  the live tree changed underneath it; settings files with unknown keys are refused.
- **Versioned locally:** the local files trees are btrfs subvolumes, snapshotted before
  every change (`files/deploy.py`, `updater/deploy.py`, `files/pull.py`); any earlier state can be inspected or
  restored with `files/snapshots.py`.
- **Not yet versioned:** `files/publish.py` overwrites the live indexes and downloads, and
  nothing undoes a publish (restoring a snapshot and publishing again only adds files).
  Releases are kept in `RELEASES_DIR/<build number>` (nothing prunes them yet).

The scripts on the host are Python (3.12 or newer, standard library only). The build steps
run inside the containers are plain bash: the Windows image has no Python.

## Settings

Every setting (repo, branch, platforms, images, clean/incremental, build type, jobs, macOS
deployment target, extra CMake arguments, the URLs compiled into the Launcher, the gate's descriptions,
the deployer image, the files trees, the bucket, the new updater's tree and keys) is listed with its default in
`config/defaults.env`. Every script takes `--config FILE`, a file of the same `KEY=value` form that
may only set settings from `defaults.env`; environment variables override both. Values may
be quoted and may use `$HOPPER` (the checkout), `$STATE` (see Layout), `$HOME` and settings defined before them.
`common.load_settings` reads them; every setting is also passed into the build containers.

**Our sites** (since 2026-11-03; the Design repo's `LauncherNetwork.md`): `FILES_MIRRORS` and
`META_MIRRORS` are each a site's base URLs, separated by spaces, the main server first and
then its copies (the bucket), in the order the Launcher tries them. They go into the
Launcher as `Launcher_FILES_MIRRORS` and `Launcher_META_MIRRORS`, which builds every URL on
our sites from a base URL and a path. `FILES_MIRRORS` also says where the files tree is
(`files/publish.py` purges on each of its base URLs) and is the new updater's mirror list.
Older Launchers take `UPDATER_BASE`, `NOTIFICATION_URL` and `TRANSLATIONS_BASE_URL` one by
one; `common.load_settings` derives them from the first files base URL (`update/`,
`notifications.json`, `translations/`), and the build steps pass both kinds, so older
commits still build (but fail the gate, below). They can't be set themselves. Of the other
URLs none take effect: the Launcher's `CMakeLists.txt` hard-codes the subreddit, Discord and
bug tracker URLs with plain `set()`.

**The updater in the Launchers** (since 2026-11-04): `UPDATER_VERSION` is the updater the
setup's Launchers are built with (`Launcher_UPDATER_VERSION`): 1, the current one
(production, until its root key ceremony), or 2, the new one (files2). With 2 they pin
`UPDATER_ROOT_KEYS` (`Launcher_UPDATER_ROOT_KEYS`) and compile in the newest keys document
from `UPDATER_KEYS` as the floor (`Launcher_UPDATER_KEYS_FLOOR`), and each platform gets its
variant ID from `PLATFORMS` in `lib/common.py` (`Launcher_UPDATER_VARIANT`).
`common.updater_floor` refuses a setup that is only half set up: root keys with updater 1;
updater 2 without root keys, without `UPDATER_ROOT=updater` (the Launchers look for the tree
at `updater/`), with a keys chain that doesn't verify or has expired, or with root keys that
aren't exactly the newest keys document's. `common.prepare_build_output` writes the floor
next to each build as `updater-keys-floor.json` (and removes one left by an earlier build
with updater 2), where the build steps pick it up. Every release's `manifest.json` records
the updater and root keys it was built with (`updater`), and `files/deploy.py` and
`updater/deploy.py` refuse a release built with another updater or other keys than the
setup's, so a build pinned to files2's test keys can't go anywhere else.

**Container engine:** `CONTAINER_ENGINE` is `docker` (default; containers run as the
calling user with `-u`) or `podman` (rootless, with `--userns=keep-id`, so the calling user
is the same user inside and owns what the containers write). Everything that starts a
container goes through `common.container_run`. The build server uses Podman: its automation user
has no Docker access, since the `docker` group is equivalent to root.

## Building one platform

    launcher/run-build.py [--config FILE] <lin64|lin32|win32|win32-5.15.2|osx64-5.15.2> <source dir> <output dir> [build number]

This builds the Launcher exactly like the releases, in the same toolchain image, and
needs nothing from the build server. To do it yourself you need Linux, Docker (or rootless
podman: `CONTAINER_ENGINE=podman`), git and Python 3.12 or newer.

1. **The images.** They are published on GitHub's container registry; pull the ones you
   need by digest and give them the names hopper uses. The digests and names are in the
   Toolchains repo's [README](https://github.com/MultiMC/Toolchains#published-images), e.g.
   for lin64:

       docker pull ghcr.io/multimc/toolchains/linux-x64@sha256:7edb202c67b44e33b455fd89ac61d233bde534c5f127c10e6459a2c6c8548d35
       docker tag ghcr.io/multimc/toolchains/linux-x64@sha256:7edb202c67b44e33b455fd89ac61d233bde534c5f127c10e6459a2c6c8548d35 multimc/toolchains/linux-x64:dev

   lin64 is 2.7 GB, lin32 2.6 GB, win32 7.7 GB; win32-5.15.2's image is win32's plus Qt 5.15.2
   and MinGW 8.1 (Toolchains `containers/windows-win32-qt5.15`, not published yet). The macOS image can't be published (it
   contains Apple's SDK); its README in the Toolchains repo says how to build it yourself.

2. **The source**, with submodules, on a branch:

       git clone --recurse-submodules https://github.com/MultiMC/Launcher.git
       git clone https://github.com/MultiMC/hopper.git

3. **Build**, e.g. lin64 into `out-lin64`:

       hopper/launcher/run-build.py lin64 Launcher out-lin64

   The Launcher is in `out-lin64/install/MultiMC`, the logs next to it. Running it again
   builds incrementally; `CLEAN=1` starts over. On a 32-core machine a clean lin64 build
   takes about half a minute, win32 (under Wine) a little over a minute.

4. **Check it** against what the releases must look like (the compatibility gate):

       hopper/launcher/compat-check.py check lin64 out-lin64/install/MultiMC hopper/launcher/expectations/lin64.toml

The build number defaults to 9999, higher than any release so far, so the build's updater
leaves it alone. The Linux builds run on the system's Qt 5, like the releases; the win32 build
runs on Windows (XP and newer) or under Wine, the win32-5.15.2 build on Windows 7 and newer.

### How it runs

The source must be a git checkout on its branch (not detached), with submodules, so the
commit and `refs/heads/<branch>` are embedded like in the TeamCity builds. The output
directory gets `build/` (the build tree; reused unless `CLEAN=1`), `install/` (the bundle:
`MultiMC/` or `MultiMC.app/`, always fresh) and `*.log`; the Windows builds also keep their Wine prefix
there.

Every platform runs the same way (`common.build_command`): its image (settings `IMAGE_*`,
built from the Toolchains repo's `containers/`), no network, as the calling user, source at `/src` read-only,
output at `/out`, `launcher/steps/` at `/build` read-only, all settings plus `PLATFORM` and
`BUILD_NUMBER` in the environment, command `bash /build/<script>`. The images contain only
toolchains.

## Working on the Launcher in an IDE (`launcher/dev-run.py`)

    launcher/dev-run.py [--config FILE] <lin64|lin32> <command> [args...]
    launcher/dev-run.py --qt-path <lin64|lin32>

Your IDE can configure and build in the Linux toolchain image, while it edits, runs and
debugs on the host. Then the code is compiled against Qt 5.6 and g++ 5 as you work, like
the releases, and anything newer than Qt 5.6 fails right away instead of at release.

`dev-run.py` runs a command in the platform's image (as you, no network, `HOME=/tmp`) in
the current directory, with your home directory mounted at the same path. Source and build
trees, compiler messages and the compile database therefore have the same paths inside and
outside. `CC`/`CXX` are the image's `gcc-5`/`g++-5`, and `LD_LIBRARY_PATH` has the image's
Qt, so the tests run in the container. The image's Qt (`/opt/qt56`) is copied to
`$STATE/dev/<platform>/qt56` on first use, and again whenever the image changes, so the IDE's
code model sees the same Qt 5.6 headers; `--qt-path` prints where it is.

`launcher/dev/lin64-cmake` and `launcher/dev/lin64-make` are what the IDE runs instead of
`cmake` and `make`. CMake also runs the make program itself (`CMAKE_MAKE_PROGRAM`), from
inside the container; the wrappers see `HOPPER_DEV_CONTAINER` there and run the real one.

Configure the build directory (inside your home directory) with these CMake arguments,
`<qt>` being what `--qt-path` prints:

    -DCMAKE_PREFIX_PATH=<qt> -DCMAKE_PLATFORM_IMPLICIT_LINK_DIRECTORIES=<qt>/lib -DLauncher_LAYOUT=lin-nodeps

The second one keeps the Qt copy out of the binaries' RPATH: run on the host, the Launcher
uses the host's Qt 5 (as the releases do), not the copy, which needs Ubuntu 14.04's
libraries.

In KDevelop (Project → Open Configuration):

- **CMake:** add a build directory with *CMake Executable* `<hopper>/launcher/dev/lin64-cmake`,
  *Runtime* Host System, *Generator* Unix Makefiles (the images have no Ninja), and the
  arguments above as *Extra Arguments*.
- **Make:** *Make Binary* `<hopper>/launcher/dev/lin64-make`. This is set for the whole
  project, not per build directory.

KDevelop's own Docker runtime isn't used: it reads the image's files through Docker's
`GraphDriver.MergedDir`, which Docker's containerd image store doesn't provide. With CMake
3.10 KDevelop imports the project from `compile_commands.json`.

## Releases

    launcher/release-build.py [--config FILE] [--incremental [--clean]] [--build-number N] [--out DIR] [commit]

The release goes into `RELEASES_DIR/<build number>` (`$STATE/releases/`, files2:
`$STATE/releases-files2/`), so a plain `launcher/release-build.py` works every time unless it hits
a real problem (a failure, another build running). A test build reusing a number
(`--build-number`) gets `<number>.2`, `.3`, ...; nothing is overwritten. `--out DIR` puts it
elsewhere; DIR must be new or empty.

1. Updates a local mirror of the Launcher repo (`$STATE/launcher.git`) and checks out the
   branch (default `develop`) at the commit (default: its head), with submodules. The
   commit must be on the branch.
2. Builds every platform in `PLATFORMS` in parallel, each logging to
   `build-<platform>.log`. Unit tests run for all but macOS (macOS binaries). Any failure
   stops the release.
3. Packages them like TeamCity's deploy did: `packages/mmc-<branch>-<platform>.tar.gz`
   (`.zip` for win32), same layout as the shipped packages; and `deploy/<platform>.tar.gz`,
   the bundle with its permissions, for `files/deploy.py`.
4. Runs `launcher/compat-check.py` for each platform against its description (`GATE_DESCRIPTIONS`).
5. Writes `manifest.json`: commit, build number, version name (`major.minor.hotfix` from
   the Launcher's `CMakeLists.txt`), mode, `UPDATER_BASE`, package hashes, gate results,
   `releasable`; and per platform what a machine needs to run it, as the gate measured it:
   `requires` (the updater's requirements, below) and `measured` (the gate's measurements:
   system libraries or DLLs, symbol versions, PE versions, minimum macOS).

Exit status 0 only if everything built and passed the gate. About 2 minutes for a clean
build on a 32-core machine. Output files belong to the user running it.

### Build numbers

The build number must keep increasing for the updater. `BUILD_COUNTER` is a file holding
the last number used; without `--build-number`, `launcher/release-build.py` takes the next one and
records it, but only once nothing can stop the build before it starts: the commit is on the
branch, the toolchain images and the gate's descriptions are there, and the source is checked out
(prepared in `RELEASES_DIR/.preparing-<pid>`, renamed to the build number after). A typo in
a commit, a missing image or a failed checkout leaves the counter alone; a build that fails
after that uses up its number, so no number is handed out twice. `--build-number N` (test builds, such as 9999) leaves the counter alone.
The counter isn't created automatically: a missing one stops the build.

- Production (`$STATE/build-number`): not seeded yet. At cutover, seed it with the highest
  of TeamCity's last build number and every version ID already in the channels (4278 on
  2026-10-23), so builds continue above both.
- files2 (`$STATE/build-number-files2`): seeded with 5002, its own numbering.

Standard output gets exactly one line, the release directory, as soon as it exists (also if
the build fails later); all logging goes to standard error. So
`files/deploy.py "$(launcher/release-build.py)"` works.

### Cleanup (`daemon/releases.py`)

    daemon/releases.py [--config FILE] list
    daemon/releases.py [--config FILE] cleanup [--dry-run]

A Launcher release is ~1.2 GB, almost all of it `build/` (the compiler trees; win32 alone
~750 MB). files/deploy.py only needs `packages/` and `deploy/`, so the cleanup first trims:
Launcher releases older than the newest `RELEASES_KEEP_BUILD_TREES` (3) lose `build/` and
`src/`, keeping packages, deploy archives, logs, compat reports and the manifest (~70 MB; a
`TRIMMED` file says when). They can still be inspected and deployed again. Of each kind
(launcher, translations, patrons, template) only the newest `RELEASES_KEEP` (30) are kept
at all. Nothing younger than `RELEASES_MIN_AGE_HOURS` (24) is touched; leftovers of
interrupted builds (`.preparing-*`) go once they are older. It takes the release lock (and
skips the run if a build holds it). The release jobs run it after every publish.

### One at a time

One release build runs at a time, in any mode: they share the Launcher mirror and the
build counter, and each uses every CPU anyway. Builds into a platform's output directory
are locked too (`launcher/run-build.py` and `launcher/release-build.py` alike). `files/deploy.py`, `files/publish.py`,
`files/pull.py` and `files/snapshots.py` lock the files tree they work on. A second run
stops with an error naming the lock; locks go away with the process, however it ends.

### Stopping

SIGTERM (what a service manager sends) and Ctrl-C stop a release build cleanly: it stops its
platform builds and exits. Every container runs with `docker run --init`: as process 1 the
build script would ignore SIGTERM (Wine kept the Windows container alive), the init process
passes it on and ends the container with it. Verified 2026-10-24: a release build stopped
mid-build exits within a second and leaves no containers. `--init` changes nothing in the
output (a build with and without it: same objects, same binaries apart from the known
timestamps and the win32 linker bytes, see below).

### Incremental builds (`--incremental`)

For development. The source checkout and one build tree per platform persist in
`$STATE/work/` (locked like every release build):

- the checkout is updated with git (`checkout -B`, `reset --hard`, `clean -ffdx`, same for
  submodules), so only files that actually changed get new timestamps;
- build trees are mounted at the same container paths every time, so CMake's cached paths
  stay valid; CMake configure runs on every build, so CMake changes and the embedded commit
  are always picked up; install trees and packages are always made fresh;
- the Windows Wine prefix is kept too.

`--clean` starts the persistent trees from scratch; use it after a toolchain image changes,
since the CMake caches remember the old one's paths. Without `--incremental` every build is
clean (fresh checkout and build trees in the output directory), as for releases.

Verified 2026-10-21: an incremental build of 2a684511 on top of a305dab9 (one changed file;
34 s instead of ~2 min) gave the same binaries on all four platforms as a clean build of
2a684511; only the jars (zip timestamps) and `libLauncher_nbt++.dll.a` (archive timestamps)
differ. An unchanged rebuild takes ~26 s. This needed a fixed `SOURCE_DATE_EPOCH` on macOS:
Qt 5.15's `rcc` embeds file times, and with the commit time an incremental build kept an
older commit's time in resources that didn't change.

Windows: two builds with the same scripts give the same binaries, but the old MinGW linker
writes a few bytes that look like leftover memory into some tables, and those change when
the container's environment does. When the scripts moved from bash to Python (2026-10-23),
`MultiMC.exe` and `libLauncher_iconfix.dll` changed in 4 bytes each, with all 392 object
files identical.

## Compatibility gate (`launcher/compat-check.py`)

    launcher/compat-check.py check <platform> <install tree> <description.toml> [--json out.json] [--set NAME=VALUE]...
    launcher/compat-check.py describe <platform> <install tree or package> [--source NAME]

The gate checks each build against a description of what it must be:
`launcher/expectations/<platform>.toml`, edited by hand. When the Launcher changes on purpose (a new
file, a new system library, a URL it no longer embeds), the description is changed with it,
with a comment saying why; so every change to the gate's conditions is deliberate and
reviewed. `describe` writes a description from an existing build; it was used once
(2026-10-25), on the builds TeamCity shipped, and is there to start over from.

| Section | Checks |
|---------|--------|
| `[files]` | exactly the listed files in the bundle |
| `[urls]` | the listed URLs are embedded in the main binary; `"${NAME}"` is each base URL of the setting `NAME` (`FILES_MIRRORS`, `META_MIRRORS`) |
| (no section) | the updater's keys in the main binary: every pinned root key (`--set UPDATER_ROOT_KEYS`) is there, and every string that looks like a key (64 lowercase hex digits) is a pinned root key or a key in the floor (`--set UPDATER_KEYS_FLOOR`, a file); both default to none, as in production. So a build pinned to another setup's keys fails |
| `[elf]` (lin64, lin32) | ELF architecture; only the listed system libraries; no symbol versions newer than listed (`GLIBC_2.14`, `GLIBCXX_3.4.21`, ...) |
| `[pe]` (win32, win32-5.15.2) | i386 only; PE OS/subsystem version at most 5.1 (XP) for win32 and 6.1 (7) for win32-5.15.2, as `steps/win32.sh` links our binaries (since 2026-11-06; MinGW's default is 4.0); only the listed system DLLs; the bundled toolchain files (Qt, MinGW runtime, OpenSSL, zlib) byte-identical (SHA-256s) |
| `[macho]` (osx64-5.15.2) | x86_64 only; minimum macOS at most 10.13; nothing linked from outside the bundle, `/usr/lib`, `/System` |

`launcher/release-build.py` passes each site setting with `--set`: the build's own value if the
Launcher being built has the matching `Launcher_<setting>` CMake option, otherwise the
production default. So files2 builds are checked against files2's sites. A Launcher from
before those options embeds none of the base URLs on their own and fails; build it with a
gate description of its time (`GATE_DESCRIPTIONS`).

Edits so far: `${TRANSLATIONS_BASE_URL}index_v2.json` is no longer required (2026-10-25):
with `Launcher_TRANSLATIONS_BASE_URL` (branch `translations-url`) the Launcher builds the
index URL at runtime. The sites replace `${UPDATER_BASE}`, `${NOTIFICATION_URL}`,
`${TRANSLATIONS_BASE_URL}`, `https://files.multimc.org/fmllibs/`,
`https://files.multimc.org/patrons.txt` and `https://meta.multimc.org/v1/` with
`"${FILES_MIRRORS}"` and `"${META_MIRRORS}"` (2026-11-03). Checked against files2's develop
build 5026 (before the Launcher's sites): it fails on exactly the four base URLs, as it should.

Verified 2026-10-25: develop 2a684511 passes on all four platforms, with production's and
files2's settings; an extra file and a modified `Qt5Core.dll` fail; the `translations-url`
branch failed on exactly the index URL until that requirement was removed. (Earlier, with
the gate comparing against the shipped binaries, deliberately broken copies failed too:
GLIBC_2.34 library, subsystem 6.0, minimum macOS 10.15. The URL check was added after an
early macOS cross build turned out to lack the updater and notification URLs; never
deployed, it would have cut macOS users off from updates.)

Needs `objdump` and `llvm-otool` on the host.

### System requirements

`check` also measures what a machine needs to run the build, for the new updater's build
manifests (`requires` of a variant, `UpdaterFormats.md` in the Design repo): never written by
hand, always taken from the binaries. They are `requires` in the `--json` output, and
`launcher/release-build.py` copies them, with the measurements, into `manifest.json`.

| Field | Measured |
|-------|----------|
| `os` | `linux`, `windows` or `macos`, from the platform |
| `arch` | `x86`, `x86_64` or `arm64`: the one ELF/PE machine or Mach-O architecture of all binaries |
| `glibc`, `glibcxx` (Linux) | the highest `GLIBC_` and `GLIBCXX_` symbol versions needed (`2.14`, `3.4.21`) |
| `qt` (Linux) | the highest `Qt_5.x` symbol version needed (`5.6`): every object built against Qt 5.6 or newer references `qt_version_tag` at the version of the Qt it was compiled with, so this is the minimum Qt 5 of the system the Linux builds run on |
| `os_version` (Windows) | the higher of the PE OS and subsystem versions declared (`5.1`; `6.1` for win32-5.15.2) |
| `os_version` (macOS) | the highest minimum macOS of the binaries (`10.13`) |

The Qt requirement is a minimum version: Qt 5 keeps its binary compatibility within 5.x,
and the Launcher can compare a version with the system's Qt. The Qt libraries it needs, by
soname, are recorded too (`measured.qt_libs`, a part of `system_libs`), for any later use. Fails closed: a requirement that can't be measured
fails the gate (several machines, no such symbol versions, no minimum macOS), and so does
Qt's private API (`Qt_5_PRIVATE_API` symbols), with which a build needs exactly the Qt it
was built with and no minimum describes it.

Verified 2026-11-01: test build 9999 of develop 3613e928 is releasable, its manifest says
lin64 x86_64, glibc 2.14, GLIBCXX 3.4.21, Qt 5.6; lin32 x86, glibc 2.4, GLIBCXX 3.4.21,
Qt 5.6; win32 x86, 4.0; macOS x86_64, 10.13. A binary without Qt or GLIBCXX symbol versions,
one using Qt's private API (the system's Qt Designer), binaries of two machines and a
macOS tree without Mach-O files each failed with the missing requirement named.

## Deploying

    files/pull.py [--dry-run]                         # FILES_ROOT := exact copy of FILES_ORIGIN (the files server)
    files/deploy.py <release dir>                           # deploy a release into FILES_ROOT
    files/publish.py [--dry-run] [--no-bucket] [--no-purge] # publish what changed since the last publish

Three kinds of release go into the tree: Launcher builds (`launcher/release-build.py`), translations
(`translations/build.py`) and the Patreon credits (`patrons/update.py`). Deploying changes
the local tree; publishing makes the live site match it.

### `files/deploy.py`

Snapshots the tree (to undo it: `files/snapshots.py restore`), then changes it. Nothing
is published; the tree is locked while it runs.

**Launcher builds:** does what TeamCity's deploy steps did on the files server, with the same
repoman (`IMAGE_DEPLOYER`, Toolchains: `containers/deployer`), as the calling user, without
network: for each platform `repoman -c update push <platform> <branch> <build> <version>
<bundle>/` (adds `update/<platform>/<branch>/<build>.json`, adds it to the channel's
`index.json`, copies files the store doesn't have yet into `files/<md5>-<name>`) and
`repoman push-file downloads <package>`. Refuses a build made for another system (the
manifest's `UPDATER_BASE` must be the tree's `update/config.json` `base_url`) and one whose
build number isn't higher than every version already in the channel.

repoman prints `Failed to load channel "main"` for every platform: `channels.json` lists a
`main` channel that has no directories. Harmless; TeamCity's deploys did the same.

**Translations:** `translations/` becomes the release's files, plus the files of languages
that kept their previous release (still named in the new index); everything else in there
is removed, so nothing piles up.

**Patreon credits:** `patrons.txt` replaces the tree's.

Verified 2026-10-23: Launcher build 9999 of 2a684511 deployed into a fresh copy: four
version files with the same structure, keys, URLs and permissions as TeamCity's last ones
(4275-4278), the indexes extended, 41 new store files (59 MB), the four downloads replaced.
Differences: win32 permissions are 0644/0755 instead of the VM's 0666/0777, and win32 gets
the right version name (TeamCity's VM builds were named `0.0.0`).

### `files/publish.py`

Publishes the tree, not a release: what was last published is a snapshot, named in
`FILES_ROOT.published`, and the difference between it and the tree now is what goes out,
whatever deploys made it (one or several), with each file's SHA-256 then and now. (A
snapshot keeps inode numbers, so only files whose inode, size or time differ are hashed.)
A successful publish records a new snapshot as published; `files/pull.py` records the
pulled tree, which equals the live one. Restoring an earlier snapshot and publishing puts
the live site back to it.

1. **The live tree** (`FILES_ORIGIN`): every changed file must be either its published or
   its new version. Anything else means someone else changed it, and it stops (pull and
   deploy again). Accepting the new version keeps publishing again, or after an
   interruption, working: already published files are reported and skipped.
2. **Copies** the files there (as `FILES_ORIGIN_OWNER`, mode `FILES_ORIGIN_MODE`, each
   replaced atomically) in an order that never lets clients see an index whose files
   aren't there yet: store files, translation files, version files, indexes, downloads,
   anything else; removals last. The new updater's tree (`UPDATER_ROOT`) goes in its own
   write order among them: content files, build manifests, keys documents and the mirror
   list, the tags document after all of those.
3. **The bucket** (`BUCKET_TARGET`, an rclone `remote:path`; the rclone config with its key
   is `BUCKET_CONFIG`, outside this repo) gets the same changes and removals, plus the
   download symlinks pointing at changed packages: the bucket has copies of those
   (`mmc-develop-osx64.tar.gz` -> `mmc-develop-osx64-5.15.2.tar.gz`).
4. **Purges** every changed or removed file (and those symlinks) from Cloudflare's cache at
   each base URL of `FILES_MIRRORS`: the files server and the bucket's domain (as of
   2026-11-03 Cloudflare caches nothing at the bucket's domains, `DYNAMIC`; purging there too
   keeps it right once it does). It needs `CLOUDFLARE_ZONE_ID` and an API token with Zone / Cache Purge
   permission in the file `CLOUDFLARE_TOKEN` (outside this repo). Up to 100 URLs per
   request (Free plan). If it fails, publishing again retries it (nothing is recorded as
   published until everything succeeded). Files changed by hand
   (e.g. a `channels.json`) aren't purged by this; purge them in the dashboard.

Without the bucket or purge settings `files/publish.py` refuses to start; `--no-bucket` and
`--no-purge` skip them deliberately. Production's bucket is the R2 bucket `multimc-files`
(served at `files-r2.multimc.org`), replacing Amazon S3; files2's is `multimc-files2`
(`files2-r2.multimc.org`). Both were seeded on 2026-10-24 (files2: 3.2 GB in 98 s;
production: synced from the files server's tree it had held files2's data before). Until cutover
TeamCity keeps releasing to the files server and S3, so the production bucket falls behind; re-sync
it then (`rclone sync` of `$STATE/files` after `files/pull.py`). The Patreon job doesn't
write to it yet either. `notifications.json` is edited by hand. (TeamCity's deploy instead
synced the whole live tree to S3 from the files server, where the tree is.) `FILES_ORIGIN` can also
be a local directory. Empty, there is no live tree: the whole local tree is synced to the
bucket, deleting what the tree no longer has.

The bucket is written with rclone (in the deployer image): 32 transfers and checks at once,
comparing by size and MD5 (`--checksum`), so objects uploaded earlier, also by s3cmd,
aren't uploaded again. Measured 2026-10-24 on files2: 605 files, 0.96 GB in 10 s (s3cmd,
one file at a time, had needed 40 min for the 3.2 GB seed); a release's bucket steps take
~8 s instead of ~25 s. s3cmd is no longer used.

Verified:
- 2026-10-23, against local stand-ins (copies of the tree for the live tree, a scratch
  prefix in the files2 bucket): the live stand-in ended identical to the local tree, with
  mode 664; the bucket got exactly the release's 104 files, including the osx64 symlink's
  copy (= the new package); a second run changed nothing; a version added to the live
  index meanwhile stopped it.
- 2026-10-24, the purge on files2: the lin64 download was in Cloudflare's cache (`HIT`,
  build 5002's); after publishing 5005 the next request was a `MISS` that returned 5005's
  package, and the one after a `HIT` again.
- 2026-10-24, the full production path on files2 (files server + R2 + purge): builds 5011 (with
  s3cmd) and 5012 (with rclone, through hopper); afterwards the bucket matched the local
  tree exactly (`rclone check`, 9774 files). Not yet run for files.multimc.org.
- 2026-10-25, on files2: build 5013 (through hopper, 26 files: only what changed; before,
  every publish handled all the release's ~100 files); afterwards the files server and the bucket
  matched the local tree; publishing again found everything already published; a live
  download changed by hand stopped it before anything was copied. Then the first
  translations release (the
  staged `release.py`, from a local test commit): 1 index changed, 1698 files removed
  (`.class`, and the unlisted `ach.qm`, `en.qm`, `pt.qm`), 16 s; files2 serves 56
  languages as `<lang>.qm`, all sizes and SHA-1s matching the index.
- 2026-10-25, publishing the tree since the last publish (replacing per-release publish
  lists, after a Patreon list deployed but not published was then skipped by the next,
  identical one): two Launcher builds deployed one after the other (5014, 5015) went out
  in one publish (26 files, both in the channel); publishing again found nothing.

Caching on files2 (2026-10-24): `files/*` has a Page Rule "Cache Everything" (store files
never change, so the cache can't go stale); `downloads/` archives are cached by Cloudflare's
defaults (4 h) and kept fresh by the purge; `update/` (indexes, version files) isn't cached,
so Launchers always see the current channel.

### Snapshots (`files/snapshots.py`)

    files/snapshots.py list | create <label> | restore <snapshot> | remove <snapshot>... | cleanup [--dry-run] | init

Each files tree (`FILES_ROOT`) is a btrfs subvolume. Read-only snapshots of it are kept
next to it, in `FILES_ROOT.snapshots/<time>-<label>`; `files/deploy.py` (`before-deploy-<build>`)
and `files/pull.py` (`before-pull`) take one before every change and refuse a tree that
isn't a subvolume. Snapshots share unchanged data with the tree, so each costs roughly what
changed after it (a release: ~100 MB). They're plain directories: `diff -r` a snapshot
against the tree to see what a deploy did.

`restore` takes a snapshot of the current state first (`before-restore`), so a restore can
be undone too, then makes the tree match the snapshot (`rsync --delete --checksum`). It
changes only the local tree. `remove` works without root (btrfs only lets the owner delete
a subvolume once it's writable and empty, which `remove` does). `init` turns an existing
directory into a subvolume: reflink copy, compare, swap; the old directory stays as
`FILES_ROOT.pre-subvolume` to delete by hand.

Every snapshot has its metadata next to it, `<snapshot>.json`: when, what kind (deploy,
published, pulled, before-pull, before-restore, init, manual) and what about (the release,
the number of files published); `list` shows them.

**Cleanup** (after every publish, or `files/snapshots.py cleanup [--dry-run]`), like
snapper's number cleanup on this workstation (which snap-pac runs around every pacman
transaction; borrowed as an idea, not used, to avoid the dependency and its root daemon):
keep the snapshot recorded as published, the newest `SNAPSHOTS_KEEP_PUBLISHED` (15)
published or pulled ones (states that were live: kept longer, also for binary diffs between
releases one day), the newest `SNAPSHOTS_KEEP` (30) others, and anything younger than
`SNAPSHOTS_MIN_AGE_HOURS` (24); remove the rest.

Verified 2026-10-23 on a copy of the tree: a deploy's snapshot showed exactly what it
changed, restoring it gave back the original tree byte for byte, and deploying again
worked. `$STATE/files` and `$STATE/files2` were converted with `init` the same day.

### Access to the files server (`host/fileserver/hopper-ssh`)

On the build server the automation reaches the files server with its own key (`hopper@builder`), which root's
`authorized_keys` there ties to `/usr/local/bin/hopper-ssh` (`restrict,command=...`). The
wrapper allows only `hopper-ssh hash <tree>` (SHA-256 of listed files; the live check),
`hopper-ssh remove <tree>` (removals when publishing) and rsync's server side, all inside
`/home/http/files` and `/home/http/files2`; anything else is refused and logged
(`journalctl -t hopper-ssh`). `files/publish.py` uses the same commands with any key, so
publishing from the workstation (root's key) goes through the wrapper too. Verified
2026-10-25: hash, rsync pull and push work; arbitrary commands, other directories, `..`
paths and rsync elsewhere are refused.

### `files/pull.py`

Overwrites the local tree (`rsync --delete`), so it also undoes local test deploys. The
first pull is ~3 GB (2 min), later ones only the changes.

## The new updater's formats (`updater/validate.py`)

    updater/validate.py document <file> [--name PATH] (--keys KEYS | --unsigned) [--now TIME]
    updater/validate.py tree <root> --floor KEYS [--now TIME] [--no-hashes]

Checks the new updater's served tree against its format spec (format 1, `UpdaterFormats.md`
in the Design repo): strict JSON, every field, the signatures, the keys chain, the tags and
the content files. It stops at the first problem with a non-zero exit. The checks are in
`lib/updater_formats.py`, so the new publisher and the tag operations use the same code;
signing is there too (`sign`, `public_key`), with the `openssl` CLI.

`document` checks one document and its `.sig`; its kind comes from its path in the tree
(`builds/4312.json`, `tags.json`, `mirrors.json`, `keys/3.json`), or `--name` for a file
elsewhere. `--keys` is the keys document to trust, `--unsigned` checks a document before it is
signed. `tree` checks a whole tree, starting from the keys document a Launcher would embed
(`--floor`).

Tests, with throwaway keys made by `openssl genpkey`:

    python3 -m unittest updater/test_formats.py
    updater/test_formats.py --sample <dir>    # the tests' sample tree and its keys, to look at

Tested 2026-11-01 (OpenSSL 3.6): the sample tree passes; changed documents and signatures,
untrusted signers, keys rotation, expiry, every structural rule of the spec and the command
line are covered by the tests. Signatures made here have not yet been checked with
Monocypher; that comes with the Launcher's own tests.

## The new updater's tree (`updater/deploy.py`)

    updater/deploy.py [--config FILE] [--dry-run] [--if-configured] <release dir>

The publisher of the new layout (component 4 of `Updater.md` in the Design repo), repoman's
replacement: it deploys a Launcher release into the setup's new updater tree, as
`files/deploy.py` deploys it into the current layout. The tree is `UPDATER_ROOT` in the
local files tree (`updater/` on files2), a directory of its own next to `update/` and
`files/`, so the same snapshots, lock and `files/publish.py` cover it, and it is served as
`<UPDATER_ROOT>/` below each base URL of `FILES_MIRRORS`. It never touches
the current layout. With `UPDATER_ROOT` empty (production, until its root key ceremony) it
refuses; `--if-configured` says so and exits 0 instead.

From the release's `deploy/<platform>.tar.gz` and `manifest.json` it makes one build for
all platforms:

1. **Content files:** every file of each bundle, `files/<xx>/<sha256>`, if the tree doesn't
   have it yet (files the same in several builds or platforms are stored once).
2. **The build manifest** `builds/<build>.json`: one variant per platform, with the
   requirements the compatibility gate measured (`requires` in `manifest.json`), the files
   with SHA-256, size and whether executable, and symbolic links. Paths are relative to the
   bundle (`MultiMC/`, `MultiMC.app/`), as in the current layout's file lists.
3. **Keys documents** from `UPDATER_KEYS` that the tree doesn't have yet; those it has must
   be identical, signatures included. **The mirror list**: the base URLs of `FILES_MIRRORS`
   (the files site's roots, the tree being at `UPDATER_ROOT` below each), each a group of its
   own, in order; if it changed.
4. **The tags document**, last: Latest moved to the build, the serial one higher. On a new
   tree the build is Latest, Testing and Stable; otherwise Testing and Stable stay where
   they are (moving them is the tag operations', still to come).

Everything new is signed with the online key (`UPDATER_ONLINE_KEY`, which must be an online
key of the newest keys document). Then, **before anything is written**, the whole tree as
it would be is checked with the validator (`updater_formats.check_tree` with the new files
as an overlay; the newest keys document as the floor; every content file hashed). Only if
that passes is the tree snapshotted (`before-updater-<build>`) and written, in the write
order above, each file renamed into place from `<UPDATER_ROOT>/.incoming/` (an interrupted
run leaves that behind; the next one removes it). The tree as written is checked again; if
that ever failed, the message names the snapshot to restore. Nothing is published:
`files/publish.py` publishes it with the rest of the tree.

Refused, with nothing written: a release that isn't releasable, isn't a Launcher build, was
built for another setup (its `UPDATER_BASE`), or lacks a platform's requirements (releases
from before 2026-11-01); a build number not higher than the tree's Latest, or withdrawn;
a bundle with anything the format can't express (a mode other than 0644 and 0755, hard
links, special files, empty directories, files outside the bundle directory).

| Platform | Variant ID | Name | Supersedes |
|----------|------------|------|------------|
| lin64 | `linux-x86_64-qt5` | Linux, 64-bit | |
| lin32 | `linux-x86-qt5` | Linux, 32-bit | |
| win32 | `windows-x86-qt5.6` | Windows, Qt 5.6 (for Windows XP and Vista) | |
| win32-5.15.2 | `windows-x86-qt5.15` | Windows, Qt 5.15 | `windows-x86-qt5.6` |
| osx64-5.15.2 | `macos-x86_64-qt5.15` | macOS, Qt 5.15 | |

The variants are in `lib/common.py` (`PLATFORMS`); a Launcher build has its own ID compiled
in and matches it as a whole string. The name is what users see when they choose between
variants. `supersedes` moves Launchers on Windows 7 and newer to Qt 5.15, unless their users
choose to stay (Design/Updater.md, System requirements).

It also writes the download archives of the tags that moved (Latest; on a new tree all
three, and Stable's archive), see Tag operations below.

Not done here: re-signing the documents already in the tree after the online key is
rotated (until then a publish with the new key is refused: the old documents no longer
verify).

Tests, with throwaway keys, on btrfs subvolumes made in `HOPPER_TEST_DIR` (default `$STATE`;
skipped elsewhere):

    python3 -m unittest updater/test_deploy.py

Verified 2026-11-01 on files2: build 5025 (`bd748be0`, 4 platforms, 92 content files)
deployed into `$STATE/files2/updater` in under a second, published with `files/publish.py`
(100 files, in write order, to the files server, the bucket and the purge; 10 s, nothing
else changed); the tree downloaded over HTTPS from `files2.multimc.org/updater/` and from
`files2-r2.multimc.org/updater/` each passed `updater/validate.py tree` with files2's
keys document as the floor. In the tests: the next build stores only new content and moves
Latest alone; a build that isn't newer, a bad bundle, a wrong setup, an online key outside
the keys document, a differing keys document and a corrupt content file already in the tree
are refused with the tree unchanged.

## Tag operations (`updater/tag.py`, `updater/remove.py`, `updater/prune.py`)

    updater/tag.py [--config FILE] [--dry-run] [--serial N] [--json] testing|stable <build> [testing|stable <build>]
    updater/remove.py [--config FILE] [--dry-run] [--serial N] [--json] <build>
    updater/prune.py [--config FILE] [--dry-run] [--if-configured] [--cap-mb N] [--serial N] [--json]

Component 5 of `Updater.md` in the Design repo: moving the Testing and Stable tags, removing
a build, and keeping the tree under its size cap. Each works like `updater/deploy.py`, on
`lib/updater_tree.py`: the new documents are signed with the online key (`Tree.sign`), the
whole tree as it would be is checked with the validator (`Tree.gate`), and only then is the
files tree snapshotted and written (`Tree.commit`); `--dry-run` stops before writing. None
of them publishes: `files/publish.py` does, removals included.

- **`tag.py testing <build>`**: the build becomes Testing. Tags only move forward: it must
  be newer than the current Testing build (and so than Stable). A bad Testing build is
  replaced by a newer one, or removed.
- **`tag.py stable <build>`**: any build newer than the current Stable becomes Stable; when
  Testing is then older, it moves along (Testing resolves to Stable).
- **`tag.py testing <build> stable <build>`**: both as one move, one new tags document; Testing
  can't be older than the new Stable.
- **`remove.py <build>`**: takes a build out for good. It goes into the tags document's
  `withdrawn`, a tag that names it moves back (Latest to the highest build left, Testing to
  the newest left between Stable and it), and its manifest and the content files no other
  build lists are removed, in the format's removal order (tags document first). The Stable
  build can't be removed: promote a replacement first. Launchers on it stay on it.
- **`prune.py`**: the retention. Builds a tag names, and builds that have been Stable, are
  never purged; while the content files only the other builds list are over
  `UPDATER_MAX_SIZE_MB` (4096), the oldest of those goes. A purged build isn't withdrawn and
  no tag moves; a Launcher on it still updates (it can't tell which files the next build
  dropped). Laminar's release job runs it after `updater/deploy.py`. `--cap-mb` is another cap,
  this once.
- All three: `--serial` refuses unless the tags document's serial is that one, and `--json`
  prints what was done, or would be, on stdout.
- A build, the tag's build, or a withdrawn build named where it doesn't fit is refused with
  the tree unchanged. Naming the build a tag already has changes no tags and writes that
  tag's download archives again.

**Download archives** (`lib/updater_downloads.py`): every tag move writes
`downloads/updater/mmc-<tag>-<platform>.<tar.gz|zip>` for the tag's build, and Stable also
`downloads/updater/archive/<build>/mmc-<build>-<platform>...`, kept for good: the archive is also the
record of which builds have been Stable, which `prune.py` never purges (a Stable build
without one, such as the first, set by `updater/deploy.py` before there were archives, gets
it when Stable moves on). They are made from the tree itself, the manifest and its content
files, so a download holds exactly what Launchers install: the bundle's top directory,
entries in path order, owned by root, every time the build's `built` time, so the same build
always gives the same bytes. They are not part of the updater's tree; `files/publish.py`
publishes them with the rest of `downloads/`. The current layout's `mmc-develop-*` downloads
are unchanged.

Tests (btrfs, like the publisher's):

    python3 -m unittest updater/test_tags.py

Verified 2026-11-05 on files2 (from the build server, then `files/publish.py`): `tag.py
stable 5025` and `testing 5025` wrote the missing downloads without moving a tag (the
archives hold exactly the files of 5025's release packages, lin64 and win32 compared);
Testing to 5035 and Stable to 5034 (serial 13, `archive/5034/`); backward moves and removing
the Stable build refused; `remove.py 5026` withdrew it and removed its manifest and the 14
content files only it listed; `prune.py` found 180 MB of 4096 MB and purged nothing. Both
copies (the files server and the bucket) then served the same tags document, 404 for
`builds/5026.json`, the same new downloads, and the current layout's `mmc-stable-*`
unchanged; the tree passed `updater/validate.py tree`.

## The updater's keys (`keys/`)

    keys/make-key.py <name>
    keys/write-keys.py <dir>/<version>.json --expires TIME --root KEY... --online KEY... [--previous FILE]
    keys/sign.py <file> <private key>... [--name PATH]
    keys/verify.py keys <N>.json... (--roots KEY... | --known FILE) [--now TIME]
    keys/verify.py file <file> [--name PATH] (--keys FILE | --key KEY...) [--now TIME]

The trust chain of the new updater (`Updater.md` in the Design repo, "Signed manifests"):
two offline root keys, pinned in the Launcher, sign a keys document that lists them and the
online key; the online key, which hopper keeps, signs everything else. One script per step,
each run on its own, so the root key ceremony can run on an offline machine with a copy of
this checkout: Python 3.12+ and the `openssl` CLI, nothing else, and only small text files
in and out. The checks are the validator's (`lib/updater_formats.py`).

- `make-key.py` writes `<name>.pem`, the private key (mode 600, unencrypted: encrypt it for
  storage, for example with `age -p`), and `<name>.pub`, the public key as 64 hex digits.
- `write-keys.py` writes a keys document, unsigned. Keys are given as hex or `.pub` files,
  the version is the number in the file name; with `--previous` it must be the next one.
- `sign.py` checks a document and adds a line per key to its `.sig`. The root keys sign one
  at a time, where each one is kept: carry the document and its `.sig` between them. It
  refuses to add to a `.sig` whose lines no longer verify.
- `verify.py keys` walks keys documents as a Launcher does: from the pinned root keys
  (`--roots`, version 1 must list exactly those) or a known document (`--known`), each next
  version signed by the root keys of the one before, never a lower or repeated version, the
  last one unexpired. `verify.py file` checks a document against the online keys of a keys
  document (`--keys`), or against given keys (`--key`, e.g. that the first root key has
  signed before the document goes to the second).

A new keys document (rotating the online key, or before the current one expires, about two
years), from the newest one `N.json`, where the root keys are:

    keys/write-keys.py keys/<N+1>.json --previous keys/<N>.json --expires <in two years> \
        --root root1.pub --root root2.pub --online online2.pub
    keys/sign.py keys/<N+1>.json root1.pem
    keys/verify.py keys keys/<N+1>.json --known keys/<N>.json

Each setup's keys documents are in `config/keys/<setup>/`, with their signatures; they hold
public keys only. Private keys are never in the checkout or in `$STATE`.

Tests, with throwaway keys:

    python3 -m unittest keys/test_keys.py

### files2's test keys

Made 2026-11-01 on the build server as hopper with these scripts: both test root keys and
the online key are in `~/keys/files2/` there (mode 700, outside the checkout and `$STATE`).
They are test keys and never reach production; production's root keys come from the
offline ceremony before the bridge build. `config/keys/files2/1.json` is the first keys
document (expires 2028-11-01), signed by both test root keys:

| Key | Public key |
|-----|------------|
| root1 | `b00524fc7df69bd16e387f7aa16fafb503553acda51575e2f7e62ad6355f545a` |
| root2 | `a81aaad40718fcb5b0fdf2918dd4a66d7b06db9ffac4a960ae61915952f5d568` |
| online1 | `6fa7862cf11cdb56e472704b2de2c79aedd2a3557f5a606435548dd0f6f77f07` |

Checked 2026-11-01: `verify.py keys` accepts it from the two root keys, and refuses it with
its expiry or online key changed (the signatures no longer verify), at its expiry time, from
a known version 2 or 1 (not newer), and with another key pinned in place of a root key.

## The files2 test system (`config/files2.env`)

A parallel system set up like production, so the whole path can be exercised without
touching files.multimc.org. Since 2026-10-24 the files server serves the live tree
(`/home/http/files2`, Caddy site `files2.multimc.org` next to `files`, see the files server's own config),
and the Cloudflare R2 bucket `multimc-files` holds a copy, as production's bucket will; it can serve files2 as a fallback through its custom domain (the `r2.dev`
address is off). `$STATE/files2` is the local working tree, nothing else writes to files2.
Builds made with `--config config/files2.env` update from files2 only.

Moving files2 onto the files server (2026-10-24): the production tree was copied on the server,
rsync then made it identical to `$STATE/files2` (131 files, 219 MB); build 5011 was then
published through the full production path (live-index check, copy to the files server, upload to
R2, purge), 39 s.

    launcher/release-build.py --config config/files2.env
    files/deploy.py --config config/files2.env $STATE/releases-files2/<build>
    updater/deploy.py --config config/files2.env $STATE/releases-files2/<build>
    files/publish.py --config config/files2.env [--dry-run]

files2 also has the new updater's tree, `updater/` (since 2026-11-01, build 5025), signed
with files2's test keys (`UPDATER_*` in `config/files2.env`), its mirror list naming the
files server first and the bucket second. Laminar's `launcher-files2` adds every new build
to it. Since 2026-11-04 files2's Launchers are built with the new updater
(`UPDATER_VERSION=2`), pinned to files2's root keys, so they update from that tree.

Seeded 2026-10-23 (as R2 only) from a copy of the live tree, with `update/config.json` (repoman's URLs)
and every `update/<platform>/channels.json` pointed at files2. The Launcher takes the channel
URLs from `channels.json`, so without the second part a files2 build checked
files.multimc.org for updates. Build numbers there started at 5000, clear of TeamCity's
(~4280). Its bucket is `multimc-files2` since 2026-10-24 (before that, files2 used
`multimc-files`, now production's). The R2 key in `BUCKET_CONFIG` covers both buckets.

Verified 2026-10-23: a Launcher from files2 build 5000 (lin64) offered build 5001 after it
was published, and updated itself to it. Then Launchers from build 5001 on Windows 11,
macOS 11 (macmini) and Linux (lin64) all updated themselves to 5002, the first real runs
of the cross-built Windows and macOS Launchers (they also start, log in and launch the
game). lin32 is not tested yet.

## Translations (`translations/build.py`)

    translations/build.py [--config FILE] [--out DIR] [commit]

Mirrors `TRANSLATIONS_REPO` (`$STATE/translations.git`), checks out `TRANSLATIONS_BRANCH`
at the commit (default: its head) and runs the repo's `release.py` in
`IMAGE_TRANSLATIONS`, without network. A language whose `.po` can't be used keeps its
previous release, taken from the local files tree's `translations/index_v2.json`. The
release goes into `RELEASES_DIR/translations-<time>-<commit>` with a manifest (kind
`translations`); `files/deploy.py` and `files/publish.py` handle it like a Launcher build. One release
build at a time, shared with `launcher/release-build.py`. Translations releases have fixed names
(`<lang>.qm`); the first one removes the `<sha1>.class` files that piled up before.

### The template (`translations/template.py`)

    translations/template.py [--config FILE] [--dry-run] [launcher commit]

Laminar's job `template-production` runs it after each production Launcher release (below); TeamCity's "Update Template" job was only run by hand. Checks out the Translations repo's
`TRANSLATIONS_BRANCH` with the Launcher (default: the head of `LAUNCHER_BRANCH`, with
submodules) in its `src/`, in `RELEASES_DIR/template-<time>/`; runs the repo's `update.py`
in `IMAGE_TRANSLATIONS` without network; if `template.pot` changed, commits it ("Update
translation template for latest MultiMC code") and pushes it to `TRANSLATIONS_PUSH_URL`
with the deploy key `TRANSLATIONS_DEPLOY_KEY` (outside this repo; GitHub's host key must
already be known). If the branch moved meanwhile the push fails and nothing changes; run it
again. `--dry-run` stops before the push. Crowdin picks the template up from the repo.

Verified 2026-10-25 (dry run, Launcher e073ab64): 1111 strings, none added or removed, in
3 s; the commit only reorders entries and the source locations within four of them, the
one-time effect of `update.py` handing files to `lupdate` in sorted order.

## Patreon credits (`patrons/update.py`)

    patrons/update.py [--config FILE]

`patrons.txt` (the Launcher's credits) lists the active patrons of the tier in
`tokens.cfg` (`[Config] reward`, the "Patron" tier) by name, then "And N others who wish to
remain anonymous." for active patrons without a tier; `patrons.json` adds image and
Patreon URLs. Made with Patreon's API v2, plain HTTP. Then `files/deploy.py` and `files/publish.py`, as
for any release (only `patrons.txt` goes live, as before). Laminar's job `patrons-production`
runs all three daily at noon.

- **The token** is `PATRONS_CONFIG/tokens.cfg` (`~/.config/multimc-deploy/patreon/`, mode
  600): the API client and a refresh token. Every run refreshes it first; Patreon then
  issues a new refresh token and the old one stops working, so the new pair is saved
  (atomically, the old file kept as `tokens.cfg.prev`) before anything else is asked.
  It exists on one machine only; `config/files2.env` sets `PATRONS_CONFIG` empty, so files2 never
  runs it. One run at a time.
- **Checked before release:** an empty list, or one less than half as long as the live
  one, is refused, and the old list stays live.

History: TeamCity ran `patronupdater.py` (the deployer image) daily. Patreon switched off
API v1 on 2026-10-08 (410 Gone); the job kept "passing" because its step script had no
`set -e` and ended with `cat out/patrons.txt`, a file left by the last good run, which was
then re-published every day. On 2026-10-25 the API client was switched to v2 with new
tokens; the first v2 list had 19 patrons (17 unchanged, 2 new, 1 gone) and the same 6
anonymous.

## Website (`website/build.py`, `website/publish.py`)

    website/build.py [--config FILE] [--out DIR] [commit]
    website/publish.py [--config FILE] [--dry-run] [--no-purge] [release]
    website/publish.py [--config FILE] [--dry-run] [--no-purge] --production [release]

multimc.org is a static Hugo site from `WEBSITE_BRANCH` of `WEBSITE_REPO` (MultiMC/Website).
`website/build.py` builds it with `IMAGE_SITE`, TeamCity's image with Hugo 0.31.1 (not
rebuildable; it is loaded from Toolchains: `snapshots/2026-10-21-deth001`), into
`RELEASES_DIR/website-<time>-<commit>/site`. The output is the same as TeamCity's builds
(verified against test.multimc.org), except the copyright year, which Hugo takes from the
clock.

Publishing works like TeamCity's "Publish Test" and "Publish Prod", but with R2 instead of
AWS S3: `website/publish.py` makes the test bucket (`WEBSITE_TEST_TARGET`, served at
`WEBSITE_TEST_URL`) an exact copy of a release, purges its URLs (and those of removed files)
from Cloudflare's cache, and records the release in `RELEASES_DIR/website-test.live`.
Laminar's job `website-production` does this for every new commit. `--production` promotes the release
that is on test to `WEBSITE_TARGET` (`WEBSITE_URL`), so production gets exactly the files
checked on test; it's run by hand. The release cleanup never removes a release that is live
(`*.live`). The website is production's only (`WEBSITE_REPO` is empty in `config/files2.env`).

## On the build server

Everything runs on the build server, which stays on, as the `hopper` user (home `/srv/hopper`, the
checkout in `~/hopper`, its secrets in `~/.config/multimc-deploy/`, rootless podman, not in
the docker group). Its only access to the files server is the key tied to `hopper-ssh`. Laminar
(below) runs the releases and meta; `daemon/trigger.py` starts them. Both are systemd user
units linked from the checkout (`host/builder/`), so after a `git pull` that changes one:
`systemctl --user daemon-reload` and a restart. A `git pull` that changes the scripts takes
effect at the next run. The user has lingering, so the units run without a login.

Their output goes to the system journal, readable by an admin but not by hopper (a system
user's uid gets no journal of its own); what matters is in files hopper can read too:
Laminar's logs, `~/laminar/trigger.log`.

Scripts run by hand as `hopper` need `CONTAINER_ENGINE=podman` (hopper has no access to
docker, on purpose); its `~/.bash_profile` exports it, and `LAMINAR_HOST` for `laminarc`.
They take the same locks as Laminar's jobs: one started by hand while a job holds the lock
stops with an error (or waits, with `HOPPER_LOCK_WAIT` set), and the job waits for it the
same way.

Until 2026-10-30, hopper's own daemon (`daemon/hopper.py`, the `hopper@<setup>` units) did
what Laminar and the trigger do now: it watched the branches and the Patreon schedule and
ran the same scripts in the same order. Its records stay in `$STATE` (`hopper.json`,
`hopper-files2.json`, each with `.logs/`), no longer written; the code is in git history.

### Overview (`daemon/overview.py`)

    daemon/overview.py

Prints, as JSON, what each setup (each settings file but `defaults.env`) has live (the new
updater's tags, each update channel's newest builds, the download archives of these, the
website on test and production, the last published snapshot) and Laminar's jobs (what starts
each, its last run, what's running, the next scheduled time) and runs, newest first. A
download's URL is only there for an archive that exists in the local files tree, as a file:
`mmc-develop-osx64` is a link to another platform's archive, not that channel's build. It
only reads: files, `systemctl --user show`, Laminar's database (read only) and `laminarc
show-running`, and `/proc/locks` for the locks (it never takes one, so it can't get in a
build's way). Tagging, promotion and retention are commands next to the scripts (`updater/`),
working on the same files tree and plain state files.

### Web view (`web/`, `host/builder/web.service`)

    cd web && go test ./... && CGO_ENABLED=0 go build -trimpath -o bin/hopper-web .

`hopper-web` shows the overview in the browser: each setup's tags and their downloads (a
setup without a new updater tree, its newest develop build) and Laminar's jobs (`/`), the
current layout's update channels (`/old-layout`), a setup's builds (`/builds/<setup>`), the
newest runs (`/runs`), and a run's log (`/run/<job>/<number>`), which grows while the run
goes. `/api/overview` and `/api/builds/<setup>` are the same JSON, for anything else that
wants it. It holds no logic of its own. The data is `daemon/overview.py`, run at most once a second however many pages are open, and a log is
passed through from Laminar's web server (`LAMINAR_HTTP`, default `http://127.0.0.1:8091`)
as it comes. If the overview can't be had, the page says why, and it doesn't show an older
one instead (the API answers 502 and `{"error": why}`); a page that can't reach the service
says "not updating".

Everything is a GET except the actions on a setup's builds (below) and cancelling a queued run. It listens on `127.0.0.1:8092`, with
Caddy in front as https://hopper.peterix.cz, and has no login, like Laminar's own site:
whoever can reach it can look, and act on files2's builds; so runs don't say who asked for them.
Requests from other sites are refused (`http.CrossOriginProtection`). Settings are in `web/main.go`. To try it from another machine, on the build server's
data:

    ssh -N -L 18091:127.0.0.1:8091 hopper@<build server> &
    LISTEN=127.0.0.1:18092 LAMINAR_HTTP=http://127.0.0.1:18091 \
        OVERVIEW="ssh hopper@<build server> hopper/daemon/overview.py" \
        BUILDS="ssh hopper@<build server> hopper/daemon/builds.py" \
        RUNS="ssh hopper@<build server> hopper/daemon/runs.py" web/bin/hopper-web

`/runs` lists what the overview lists, the newest 100 runs of all jobs; the hourly `meta`
pushes older runs off it within days. Their logs stay at their URLs.

A queued run has **Cancel** next to it, there and on its page, which asks first.

### Cancelling a queued run (`daemon/runs.py`, `laminar/cfg/before`)

    daemon/runs.py cancel <job> <number>

Laminar can only abort a run that has started, so a queued one is cancelled in two steps.
`runs.py` refuses unless Laminar lists the run as queued, writes a mark,
`~/laminar/cancelled/<job>:<number>` (when it was cancelled), and checks the queue again: if
the run started meanwhile, it says so, and the run's log shows whether it stopped. When the
run's turn comes, `cfg/before`, which Laminar runs first in every run, finds the mark, logs
"Cancelled while queued" and aborts the run before its job starts: it ends as `aborted`.
The overview shows a marked queued run as cancelled. The marks stay, as a record; run numbers
are never used again.

### A setup's builds and the actions on them (`daemon/builds.py`, Laminar's `<action>-<setup>`)

    daemon/builds.py list <setup>
    daemon/builds.py check <setup> --serial N tag [testing <build>] [stable <build>]
    daemon/builds.py check <setup> --serial N remove <build>
    daemon/builds.py check <setup> --serial N prune [<MB>]
    daemon/builds.py queue <setup> --serial N ...

`list` gives the builds, newest first: number, commit and subject, when built, platforms and
their downloads, the tags naming them, and whether they keep an archive; for a setup without a
new updater tree, its Launcher releases in `RELEASES_DIR`, and no tags. `check` is the script's
(`updater/tag.py`, `remove.py`, `prune.py`) `--dry-run --json` report, and changes nothing.
`queue` checks again and queues the Laminar job `tag-<setup>`, `remove-<setup>` or
`prune-<setup>`, which runs the script and `files/publish.py`. A setup can only be acted on if it
has a tree and these jobs: `files2` for now. `--serial` is the tags document's serial as `list`
gave it: if the tags have changed since (a new build moves Latest, so that counts), the script
refuses, at each of these steps and in the job.

`/builds/<setup>` does these in steps, so nothing happens by accident: each shows in a popup,
from `check`, what it would do, with the commits, and only **Confirm** (Cancel has the focus)
runs `queue` and goes to the job's run.

- **Moving tags**: choose builds for Testing and Stable (older than the current ones can't be
  chosen, and Stable beyond Testing takes Testing along), then **Apply**.
- **Withdrawing**: **Withdraw…** on a build (not the Stable one), and Confirm only opens after
  typing the build's number: it can't be undone and the number is never used again.
- **Pruning**: **Prune now…**, to the cap (`UPDATER_MAX_SIZE_MB`) or to another one, this once. If
  nothing is over it, the popup says so and has no Confirm.

The page doesn't refresh itself, as that would drop the choices. Nothing is tried again after a
refusal: reload the page, and see the current state.


## Laminar (`host/builder/laminar.service`)

[Laminar](https://laminar.ohwg.net), a small CI server, runs jobs as the `hopper` user from
`~/laminar` (`LAMINAR_HOME`): Laminar's own files (`archive/`, `run/`, `laminar.sqlite`), hopper's `cancelled/` (below), and
`bin/` with `laminard` and `laminarc`. Its config is this repo's `laminar/cfg`, linked as
`~/laminar/cfg`. Each job is `laminar/cfg/jobs/<job>.run`, any executable; Laminar keeps every
run, its whole log and its result, and shows them at https://laminar.peterix.cz (Caddy in
front of `127.0.0.1:8091`; internal: this network and the VPN, no login). `laminarc`
reaches it over `~/laminar/rpc.sock`. It picks up new and changed jobs (a `git pull`) by
itself; only replacing the `~/laminar/cfg` link needs a restart of `laminar.service`, as it
watches the directory it found when it started. Its jobs so far:

- `meta`: the meta pipeline (below).
- `tag-files2`, `remove-files2`, `prune-files2`: move files2's tags, withdraw a build, purge
  what's over the cap, and publish that (see "A setup's builds and the actions on them").
- `launcher-production`, `launcher-files2`, `translations-production`, `translations-files2`,
  `website-production`, `patrons-production`: hopper's releases (below).
  `launcher-production` has no `TRIGGER` since 2026-11-03 (commented out in its `.conf`):
  no production Launcher releases until the Launcher's sites (`FILES_MIRRORS`) are verified
  on files2.
- `template-production`: the translations template (below).

### Its binaries (`laminar/build.sh`)

    laminar/build.sh [OUT]       # default ~/laminar/bin; then: systemctl --user restart laminar

builds `laminard` and `laminarc` in a container (`laminar/Containerfile`, podman) so that no
system update can break them: Cap'n Proto (built there from its release, static), SQLite,
zlib, libstdc++ and libgcc are linked in, and the only library left to load is glibc (built
on Debian 13, they need 2.38 or newer; the build server has 2.44). Every input is pinned: the base image by digest, Cap'n
Proto by checksum, Laminar by commit (1.4-4-g9968f74a; its CMake downloads its web UI's
scripts, checked by MD5). `build.sh` refuses binaries that load anything but glibc.

### Starting jobs (`daemon/trigger.py`, `host/builder/trigger.service`)

    daemon/trigger.py watch [--once]
    daemon/trigger.py status

Laminar doesn't watch repositories or keep schedules. `daemon/trigger.py` does, with
the rules hopper's daemon had, and only queues jobs. A job says what starts it in its
`cfg/jobs/<job>.conf` (Laminar only reads the keys it knows from there):

    TRIGGER=git https://github.com/MultiMC/Website.git master    # laminarc queue website-production COMMIT=<commit>
    TRIGGER=cron 0 * * * *                                        # laminarc queue meta SCHEDULED=<time>

Every minute it looks at each trigger: a new commit, or the most recent scheduled time not
handled yet, queues the job once (after a time off: once, not once per missed commit or
time; Laminar shows why, "trigger: new commit ..."). On a trigger's first look its current
commit or time is adopted, not queued. A job that's running or queued already isn't queued
again; it gets the newest commit when it's free. If `laminarc` fails, nothing is recorded,
and the next look tries again. What it handled is in `~/laminar/trigger.json`, its last
look in `trigger.status`, its log in `trigger.log`. Jobs without `TRIGGER` are started by
hand: `laminarc queue <job>`.

### The meta pipeline (Laminar's job `meta`)

meta.multimc.org's pipeline (the MultiMC/meta repo: `update.sh` and its Python scripts)
moved from the files server to the build server on 2026-10-26 as it was: same checkout, caches, upstream and
output repos and `config/` (deploy keys, Discord webhooks), in `~/meta` of the `hopper`
user. Since 2026-10-29 it's Laminar's job `meta` (`cfg/jobs/meta.run`: `update.sh`, then
the Forge maven copy, `~/forgemaven` as written by `enumerateForge.py`, to the files server's
`/home/http/forgemaven`), started hourly by the trigger (`TRIGGER=cron 0 * * * *`); each
run's commits of meta's `upstream/` and `multimc/` repos go to its archive. Its Python
libraries are the build server's system packages (as on the files server). Only `config/config_local.sh`
differs from the files server's: `update.sh` deploys to `root@deth001.multimc.org:/home/http/meta/v1/`
through `hopper-ssh`, and `DEPLOY_FORGE_MAVEN=false`. Changes to meta itself (a container
with fixed tool versions, ...) come later.

### The releases (Laminar's jobs `<stream>-<setup>`)

Since 2026-10-30 (before that, hopper's own daemon), Laminar's job `<stream>-<setup>` releases
a stream with `config/<setup>.env`: `launcher-production` and `launcher-files2` for each new commit of
the Launcher repo's `develop` (files2: its `LAUNCHER_BRANCH`, `updater` for the updater work), `translations-production` and `translations-files2` for each new commit
of the Translations repo's `master`, `website-production` for each new commit of the Website
repo's `master` (it replaced the build-only `website` job, whose runs stay in Laminar's
history), `patrons-production` every day at noon. Their `.run` are links to one script,
`cfg/scripts/release`, which runs the same scripts the daemon ran, in the same order:

- launcher: `launcher/release-build.py`, `files/deploy.py`, `files/publish.py`; then
  `updater/deploy.py --if-configured` and `files/publish.py` again, so the new updater's tree
  only goes out after the current layout is live, and a failure there fails the job without
  holding back the current layout (production has no tree yet and skips it, saying so)
- translations: `translations/build.py`, `files/deploy.py`, `files/publish.py`
- patrons: `patrons/update.py`, `files/deploy.py`, `files/publish.py`
- website: `website/build.py`, `website/publish.py` (to test; production is still promoted
  by hand, `website/publish.py --production`)

then `daemon/releases.py cleanup` (a failure of that is only logged). The release stays in
`RELEASES_DIR`, under its cleanup; the archive gets its path, `manifest.json` and its build
logs (the launcher's per platform, with its compatibility reports). The launcher's build
number comes from `release-build.py` and the setup's `BUILD_COUNTER`, as it did under the
daemon: the counter moved with the job, nothing else uses it. `laminarc queue translations-production COMMIT=<sha>` releases a given commit.

hopper's scripts stop at once when a lock they need is held (another release build, the
files tree). Nothing retries a Laminar job, so the job sets `HOPPER_LOCK_WAIT=3600`: they
wait up to an hour for the lock instead, and say so in the log. Their contexts,
`production` and `files2` (`EXECUTORS=1` each), run one job of each setup at a time.

Tested 2026-10-30 in a throwaway laminard with a scratch copy of files2's tree (live tree:
a local directory; the bucket and the purge skipped, the only change to the script): the
first run waited for the release lock held by hand, then built `9a051ca4`; a release of
`1e543c81` published 2 files to the stand-in live tree, and the head (`d6b1b66a`) put it
back, identical to files2's. The first release through it was files2's, `bd758fca`
(2026-10-30, 9 s, with the bucket and the purge).

`launcher-files2` was tested the same way (2026-10-30, its own state: release lock, mirror,
a copy of files2's build counter and tree): `a93647e8` built as 5020 in 3.5 min, all four
platforms passed the gate, the deploy and publish put 26 files in the stand-in live tree.
Aborting a run (`laminarc abort`) kills everything at once (SIGKILL, so the scripts can't
clean up), the build containers included; the build number stays used and the half-built
release stays in `RELEASES_DIR` until its cleanup. The next run (5022) was fine.
The first real one, `launcher-files2:1` (2026-10-30, `48bced51`, build 5020, with the
bucket and the purge), waited 3 min for the release lock held by hopper@production's build
of the same commit (4286), then took 3.5 min; production's launcher followed it to Laminar,
and the daemon was retired.

### The template (Laminar's job `template-production`)

`cfg/jobs/template-production.run` runs `translations/template.py` with
`config/production.env`, then `daemon/releases.py cleanup`. It has no `TRIGGER`:
`launcher-production`, once it has published, queues it with the commit it released
(`cfg/scripts/release`; a failure to queue is only logged, the release stands). So the
template follows what production ships, not every commit. A release that changes no
translatable string changes nothing (the run says "identical"). There's only the one: the
template goes to the Translations repo, which every setup builds from, so files2 has none.
It waits for the release lock like the releases (`HOPPER_LOCK_WAIT=3600`). If Crowdin pushes
to the Translations branch between the mirror update and the push, the push fails, nothing
changes and the run fails; the next release tries again, or
`laminarc queue template-production [COMMIT=<sha>]`. Each run leaves an 87 MB work directory in
`RELEASES_DIR` (the Launcher checkout with submodules), kept like the releases: the newest
`RELEASES_KEEP`.

## Requirements

Docker with the toolchain images built or loaded (from the Toolchains repo), git, python3 (3.12+),
rsync, outbound access to GitHub; the `openssl` CLI for the new updater's validator and keys.
