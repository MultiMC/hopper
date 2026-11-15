#!/usr/bin/env python3
"""Build a release of the Linux packages: the Packaging repo's build.py.

    packages/build.py [--config FILE] [--out DIR] [commit]

1. Updates a local mirror of PACKAGING_REPO ($STATE/packaging.git) and checks out
   PACKAGING_BRANCH at the commit (default: its head) into the release directory.
2. Runs its build.py there with CONTAINER_ENGINE: the wrapper's source tarball, the .deb and
   .rpm, and the AUR's PKGBUILD and .SRCINFO, each format built in its distribution's image
   (Debian, Fedora, Arch; with network, which those builds need to get their tools).
3. Writes manifest.json (kind "packages", the commit, the version, the downloads with their
   sha256, releasable).

The release goes into RELEASES_DIR/packages-<time>-<commit> (or --out DIR, which must be new
or empty). Standard output gets one line, the release directory; logging goes to standard
error. One release build at a time (shared with launcher/release-build.py). files/deploy.py
puts the downloads into the files tree, when the version is new; packages/aur.py pushes the
AUR files after files/publish.py.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common

log = common.logger("packages")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--out", type=Path)
    parser.add_argument("commit", nargs="?")
    args = parser.parse_args()
    common.stop_on_sigterm()

    settings = common.load_settings(args.config)
    if not settings["PACKAGING_REPO"]:
        common.die("this setup has no Linux packages (PACKAGING_REPO is empty)")
    branch = settings["PACKAGING_BRANCH"]

    with common.lock_release():
        log("updating mirror")
        common.update_mirror(settings["PACKAGING_REPO"], common.PACKAGING_MIRROR)
        commit = common.resolve_on_branch(common.PACKAGING_MIRROR, branch, args.commit)
        if args.out:
            out = args.out.resolve()
            if out.exists() and any(out.iterdir()):
                common.die("%s is not empty" % out)
        else:
            out = Path(settings["RELEASES_DIR"]) / ("packages-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), commit[:10]))
        out.mkdir(parents=True, exist_ok=True)
        log("release directory %s" % out)
        print(out, flush=True)

        src = out / "src"
        log("checking out %s at %s" % (branch, commit))
        common.run("git", "clone", "-q", "--no-hardlinks", "-b", branch, common.PACKAGING_MIRROR, src)
        common.run("git", "-C", src, "reset", "-q", "--hard", commit)
        version = (src / "VERSION").read_text().strip()

        log("running build.py for version %s" % version)
        build = out / "build"
        with open(out / "release.log", "w") as logfile:
            result = subprocess.run([sys.executable, "-I", str(src / "build.py"), "--engine", settings["CONTAINER_ENGINE"],
                                     "--out", str(build)], stdout=logfile, stderr=subprocess.STDOUT)

        downloads = ["multimc-wrapper-%s.tar.gz" % version, "multimc_%s-1.deb" % version, "MultiMC5-%s-1.noarch.rpm" % version]
        aur = ["aur/PKGBUILD", "aur/.SRCINFO"]
        present = all((build / name).is_file() for name in downloads + aur)
        releasable = result.returncode == 0 and present
        manifest = {"kind": "packages", "commit": commit, "branch": branch, "version": version,
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "downloads": {name: sha256(build / name) for name in downloads if (build / name).is_file()},
                    "aur": aur, "releasable": releasable}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    if not releasable:
        common.die("build.py failed or left out a package (see %s)" % (out / "release.log"))
    log("releasable: version %s from %s, see %s" % (version, commit[:10], out / "manifest.json"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    main()
