#!/usr/bin/env python3
"""Push a Linux packages release's PKGBUILD and .SRCINFO to the AUR (multimc-bin).

    packages/aur.py [--config FILE] [--dry-run] [--if-configured] <release dir>

The release is one made by packages/build.py. Its PKGBUILD downloads the wrapper's source
tarball from the files site, so it's pushed only when the files tree has that exact tarball
(the same sha256): this release deployed it, and files/publish.py, run before this, made it
live. A release that deployed nothing (its version was published already) pushes nothing.

Clones AUR_PUSH_URL into the release directory (aur-repo/), puts the two files in, and when
they differ from what's there, commits them ("multimc-bin <version>-1", as Automation) and
pushes to master with the SSH key AUR_DEPLOY_KEY (an AUR account that maintains multimc-bin;
the AUR's host key must be in ~/.ssh/known_hosts). The push fails, changing nothing, if
someone pushed meanwhile; run it again.

--dry-run: everything but the push. --if-configured: no key at AUR_DEPLOY_KEY is a log line,
not an error (the release job uses it).
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common

log = common.logger("aur")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--if-configured", action="store_true")
    parser.add_argument("release", type=Path)
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    release = args.release.resolve()
    manifest = common.read_release(release)
    if manifest.get("kind") != "packages":
        common.die("%s is not a Linux packages release" % release)
    version = manifest["version"]

    key = Path(settings["AUR_DEPLOY_KEY"]) if settings["AUR_DEPLOY_KEY"] else None
    if not args.dry_run and (key is None or not key.is_file()):
        if args.if_configured:
            log("no AUR key at %s: not pushing multimc-bin %s" % (key, version))
            return
        common.die("no AUR key at %s (or use --dry-run)" % key)

    tarball = "multimc-wrapper-%s.tar.gz" % version
    live = Path(settings["FILES_ROOT"]) / "downloads" / tarball
    if not live.is_file() or hashlib.sha256(live.read_bytes()).hexdigest() != manifest["downloads"][tarball]:
        log("the files tree doesn't have this release's %s: it deployed nothing, nothing to push" % tarball)
        return

    ssh = "ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes"
    if key is not None:
        ssh += " -i %s" % key
    env = {**os.environ, "GIT_SSH_COMMAND": ssh}
    repo = release / "aur-repo"
    shutil.rmtree(repo, ignore_errors=True)
    log("cloning %s" % settings["AUR_PUSH_URL"])
    common.run("git", "clone", "-q", settings["AUR_PUSH_URL"], repo, env=env)
    for name in manifest["aur"]:
        shutil.copyfile(release / "build" / name, repo / Path(name).name)
    common.run("git", "-C", repo, "add", "PKGBUILD", ".SRCINFO")
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode == 0:
        log("the AUR has multimc-bin %s like this already, nothing to push" % version)
        return

    common.run("git", "-C", repo, "-c", "user.name=Automation", "-c", "user.email=automation@multimc.org",
               "commit", "-q", "-m", "multimc-bin %s-1\n\nFrom MultiMC/Packaging %s." % (version, manifest["commit"]))
    commit = common.output("git", "-C", repo, "rev-parse", "HEAD")
    if args.dry_run:
        log("dry run: committed %s in %s, not pushed" % (commit[:10], repo))
        return
    log("pushing multimc-bin %s (%s) to %s" % (version, commit[:10], settings["AUR_PUSH_URL"]))
    common.run("git", "-C", repo, "push", "-q", "origin", "HEAD:refs/heads/master", env=env)
    log("pushed")


if __name__ == "__main__":
    main()
