#!/usr/bin/env python3
"""Deploy a release into the local files tree (FILES_ROOT): translations made by
translations/build.py, or the Patreon credits made by patrons/update.py.

    files/deploy.py [--config FILE] <release dir>

Before changing the tree it takes a read-only snapshot of it (files/snapshots.py lists and
restores them; a tree that isn't a btrfs subvolume is refused). Nothing is published here;
the tree stays local: files/publish.py publishes everything that changed since the last publish.
Refuses a release that isn't releasable.

Launcher builds are refused: they go into the new updater's tree (updater/deploy.py). The
old update layout (update/, files/, the mmc-* downloads) took its last build, the bridge,
at the cutover and stays as it is (README, "The cutover to the new updater").

Translations: translations/ becomes the release's files, plus the files of languages that
kept their previous release (still named in the new index); everything else in there is
removed, so nothing piles up.

Patreon credits: patrons.txt replaces the tree's.

Linux packages (packages/build.py): the source tarball, .deb and .rpm go into downloads/, for a
version not there yet; a version that is (its .deb) stays as it is. downloads/multimc.deb and
multimc.rpm, the names the website links to, become copies of the current version's.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common

log = common.logger("deploy")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("release", type=Path)
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    release = args.release.resolve()
    manifest = common.read_release(release)
    root = Path(settings["FILES_ROOT"])
    if not (root / "update" / "config.json").is_file():
        common.die("no files tree at %s (run files/pull.py)" % root)
    if manifest.get("kind") == "translations":
        deploy_translations(settings, release, manifest, root)
    elif manifest.get("kind") == "patrons":
        deploy_patrons(settings, release, root)
    elif manifest.get("kind") == "packages":
        deploy_packages(settings, release, manifest, root)
    elif "kind" not in manifest:
        common.die("%s is a Launcher build: those go into the new updater's tree (updater/deploy.py), "
                   "the old update layout takes none" % release)
    else:
        common.die("%s is a %s release, files/deploy.py takes translations, patrons and packages" % (release, manifest["kind"]))


def deploy_translations(settings, release, manifest, root):
    build = release / "build"
    with open(build / "index_v2.json") as f:
        index = json.load(f)
    keep = set(manifest["files"]) | {entry["file"] for entry in index["languages"].values()}
    with open(build / "index") as f:
        keep |= {line.strip() for line in f if line.strip()}
    log("translations from %s: %d files" % (manifest["commit"][:10], len(manifest["files"])))

    target = root / "translations"
    with common.lock_files(settings):
        missing = sorted(name for name in keep - set(manifest["files"]) if not (target / name).is_file())
        if missing:
            common.die("the new index names files that are neither in the release nor in the tree: %s"
                       % ", ".join(missing))
        snapshot = common.snapshot_files(settings, "before-translations-%s" % manifest["commit"][:10],
                                          kind="deploy", about=release.name)
        log("snapshot of the tree as it was: %s" % snapshot.name)

        target.mkdir(exist_ok=True)
        for name in manifest["files"]:
            shutil.copyfile(build / name, target / name)
        removed = sorted(name for name in os.listdir(target) if name not in keep)
        for name in removed:
            (target / name).unlink()
        log("%d files copied, %d removed" % (len(manifest["files"]), len(removed)))

        changes = common.tree_changes(snapshot, root)

    log("deployed to %s (not published): %d files changed" % (root, len(changes)))



# The packages' names that don't change with their version, for the website's links: copies of
# the current version's files (like the Launcher's downloads/MultiMC-<variant>, which are
# copies too), {stable name: versioned name}
def stable_packages(version):
    return {"multimc.deb": "multimc_%s-1.deb" % version, "multimc.rpm": "MultiMC5-%s-1.noarch.rpm" % version}


def deploy_packages(settings, release, manifest, root):
    build = release / "build"
    version = manifest["version"]
    target = root / "downloads"
    with common.lock_files(settings):
        # a published version stays as it is; the .deb says whether this one is
        published = (target / ("multimc_%s-1.deb" % version)).exists()
        if not published:
            clash = sorted(name for name in manifest["downloads"] if (target / name).exists())
            if clash:
                common.die("the files tree has %s already, but not the .deb of version %s" % (", ".join(clash), version))
            for name, digest in manifest["downloads"].items():
                if hashlib.sha256((build / name).read_bytes()).hexdigest() != digest:
                    common.die("%s changed since it was built (sha256 differs from the manifest)" % (build / name))
        source = target if published else build
        stale = sorted(name for name, versioned in stable_packages(version).items()
                       if not (target / name).is_file()
                       or common.sha256_file(target / name) != common.sha256_file(source / versioned))
        if published and not stale:
            log("Linux packages %s are published already, nothing deployed: raise VERSION in the "
                "Packaging repo to release these changes (%s)" % (version, manifest["commit"][:10]))
            return

        snapshot = common.snapshot_files(settings, "before-packages-%s" % version, kind="deploy", about=release.name)
        log("snapshot of the tree as it was: %s" % snapshot.name)
        if not published:
            for name in sorted(manifest["downloads"]):
                shutil.copyfile(build / name, target / name)
        for name in stale:
            shutil.copyfile(target / stable_packages(version)[name], target / name)
        changes = common.tree_changes(snapshot, root)

    if published:
        log("Linux packages %s are published already; updated %s to match" % (version, " and ".join(stale)))
    log("Linux packages %s from %s deployed to %s (not published): %d files changed"
        % (version, manifest["commit"][:10], root, len(changes)))


def deploy_patrons(settings, release, root):
    with common.lock_files(settings):
        snapshot = common.snapshot_files(settings, "before-patrons", kind="deploy", about=release.name)
        log("snapshot of the tree as it was: %s" % snapshot.name)
        shutil.copyfile(release / "patrons.txt", root / "patrons.txt")
        changes = common.tree_changes(snapshot, root)

    log("deployed to %s (not published): %d files changed" % (root, len(changes)))


if __name__ == "__main__":
    main()
