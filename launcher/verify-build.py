#!/usr/bin/env python3
"""Check an incremental release against a clean build of the same commit. Publishes nothing.

    launcher/verify-build.py [--config FILE] [--archive DIR] [release dir]

Without a release directory: the newest release in RELEASES_DIR that reused a build tree
(an incremental build), is releasable, and hasn't been checked yet. If there is none, it says so and
exits 0.

Builds the release's commit from scratch with launcher/release-build.py (--build-number:
the same number, the counter is left alone; --out: RELEASES_DIR/.verify, not a release),
then compares the two with launcher/compare-releases.py. The result goes into the release
directory: verify.json (the comparison, or why there was none) and verify.txt, copied to
DIR with --archive (Laminar's $ARCHIVE). The clean
build is removed when it matches, and kept in RELEASES_DIR/.verify for a look when it
doesn't (until the next check).

A release built with toolchain images or build steps other than today's can't be rebuilt
the same way; it is marked as not comparable, which isn't a failure.

Exit status 0 when the builds match or can't be compared, 1 when they differ or the clean
build fails. Nothing is deployed or published, and no build number is used up.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common

log = common.logger("verify")

HERE = Path(__file__).resolve().parent
RESULT = "verify.json"


def candidates(settings):
    """Releases in RELEASES_DIR to check: built incrementally, releasable, not checked yet;
    newest first."""
    found = []
    for directory in Path(settings["RELEASES_DIR"]).iterdir():
        manifest_file = directory / "manifest.json"
        if directory.name.startswith(".") or not manifest_file.is_file() or (directory / RESULT).exists():
            continue
        manifest = json.loads(manifest_file.read_text())
        # at least one platform reused its build tree (a first build in WORK_DIR is clean anyway)
        reused = "incremental" in (manifest.get("builds") or {}).values()
        if manifest.get("mode") == "incremental" and reused and manifest.get("releasable"):
            found.append((manifest["build_number"], directory))
    return [directory for _, directory in sorted(found, reverse=True)]


def not_comparable(settings, manifest):
    """Why the release can't be rebuilt the way it was built, or None."""
    changes = []
    for name, info in manifest["platforms"].items():
        recorded = info.get("build_key")
        if recorded is None:
            changes.append("%s: no record of what it was built with" % name)
            continue
        p = common.platform(name)
        now = {"image": common.image_id(settings, settings[p.image_setting]), "steps": common.build_steps_hash()}
        changed = [what for what in ("image", "steps") if recorded.get(what) != now[what]]
        if changed:
            changes.append("%s: %s changed since" % (name, ", ".join(changed)))
    return "; ".join(changes) or None


def write_result(release, result, text, archive):
    result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (release / RESULT).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    (release / "verify.txt").write_text(text + "\n")
    if archive:
        for name in (RESULT, "verify.txt"):
            shutil.copy(release / name, archive / name)
    log(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("release", nargs="?", type=Path)
    args = parser.parse_args()
    common.stop_on_sigterm()
    settings = common.load_settings(args.config)

    if args.release:
        release = args.release.resolve()
        if not (release / "manifest.json").is_file():
            common.die("%s isn't a release directory (no manifest.json)" % release)
    else:
        found = candidates(settings)
        if not found:
            log("no incremental release to check in %s" % settings["RELEASES_DIR"])
            return
        release = found[0]
    manifest = json.loads((release / "manifest.json").read_text())
    build_number, commit, branch = manifest["build_number"], manifest["commit"], manifest["branch"]
    log("checking release %d (%s, %s) in %s" % (build_number, commit[:10], branch, release))

    reason = not_comparable(settings, manifest)
    if reason:
        write_result(release, {"comparable": False, "reason": reason},
                     "release %d can't be rebuilt the way it was built: %s" % (build_number, reason), args.archive)
        return

    out = Path(settings["RELEASES_DIR"]) / ".verify"
    if out.exists():
        shutil.rmtree(out)
    command = [sys.executable, HERE / "release-build.py", "--build-number", str(build_number), "--out", out, commit]
    if args.config:
        command[2:2] = ["--config", args.config]
    log("building %s from scratch in %s" % (commit[:10], out))
    # the release's branch, whatever the setup builds now; release-build.py's one line of
    # standard output (the directory) goes with the logging
    built = subprocess.run(command, env={**os.environ, "LAUNCHER_BRANCH": branch}, stdout=sys.stderr)
    if built.returncode != 0:
        write_result(release, {"comparable": True, "ok": False, "problems": ["the clean build failed, see %s" % out]},
                     "release %d: the clean build failed (exit status %d), see %s" % (build_number, built.returncode, out),
                     args.archive)
        sys.exit(1)

    compared = subprocess.run([sys.executable, HERE / "compare-releases.py", "--json", release / RESULT, release, out],
                              capture_output=True, text=True)
    if compared.returncode not in (0, 1):
        common.die("comparing failed: %s" % compared.stderr.strip())
    result = json.loads((release / RESULT).read_text())
    result["comparable"] = True
    write_result(release, result, compared.stdout.rstrip(), args.archive)
    if result["ok"]:
        shutil.rmtree(out)
    else:
        log("the clean build stays in %s for a look" % out)
        sys.exit(1)


if __name__ == "__main__":
    main()
