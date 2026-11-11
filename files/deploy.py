#!/usr/bin/env python3
"""Deploy a release into the local files tree (FILES_ROOT): a Launcher build made by
launcher/release-build.py, translations made by translations/build.py, or the Patreon credits
made by patrons/update.py.

    files/deploy.py [--config FILE] [--bridge | --skip-frozen] <release dir>

Before changing the tree it takes a read-only snapshot of it (files/snapshots.py lists and
restores them; a tree that isn't a btrfs subvolume is refused). Nothing is published here;
the tree stays local: files/publish.py publishes everything that changed since the last publish.
Refuses a release that isn't releasable.

Launcher builds: does what TeamCity's deploy steps did on the files server, with the same repoman
(IMAGE_DEPLOYER), as the calling user, without network: for each platform,

    repoman -c update push <platform> <branch> <build> <version> <bundle>/
    repoman push-file downloads <package>

which adds update/<platform>/<branch>/<build>.json, adds it to the channel's index.json (a new
channel, the first build of a branch, is added to the platform's channels.json; its
directory is made here first, as repoman doesn't),
copies files the store doesn't have yet into files/<md5>-<name>, and replaces the download
package. Refuses a build made for another tree (its UPDATER_BASE must be the tree's
update/config.json base_url), one built with another updater or other pinned root keys
than this setup's (UPDATER_VERSION, UPDATER_ROOT_KEYS), and one whose build number isn't
higher than every version already in the channel (the updater only offers newer IDs).

The cutover to the new updater: once the current layout is frozen (CURRENT_LAYOUT_FROZEN_AT,
the last build made with updater 1), Launcher builds are refused here; they go into the new
updater's tree only (updater/deploy.py), where no Launcher with updater 1 sees them.

--bridge       deploy this build as the bridge: the one build the frozen layout takes, by
               hand, which moves every Launcher with updater 1 onto the new updater. Only
               when the layout is still at the build it was frozen at, the release is a build
               of develop (the channel those Launchers read) with updater 2 and every
               platform, and the build is Stable in the new updater's tree as published.
               Test it first: installed from the new tree's downloads, updating to a later
               build, on every platform. After it, nothing goes into the current layout.
--skip-frozen  for the release job: a Launcher build for a frozen layout is skipped with a
               log line, exit 0, instead of refused

Translations: translations/ becomes the release's files, plus the files of languages that
kept their previous release (still named in the new index); everything else in there is
removed, so nothing piles up.

Patreon credits: patrons.txt replaces the tree's.
"""

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("deploy")

# The channel every Launcher with updater 1 reads, whatever its own branch or setting
# (the Launcher's updater/v1/UpdateChecker.cpp)
BRIDGE_CHANNEL = "develop"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--bridge", action="store_true")
    parser.add_argument("--skip-frozen", action="store_true")
    parser.add_argument("release", type=Path)
    args = parser.parse_args()
    if args.bridge and args.skip_frozen:
        common.die("--bridge and --skip-frozen don't go together")

    settings = common.load_settings(args.config)
    release = args.release.resolve()
    manifest = common.read_release(release)
    root = Path(settings["FILES_ROOT"])
    if not (root / "update" / "config.json").is_file():
        common.die("no files tree at %s (run files/pull.py)" % root)
    if "kind" in manifest and args.bridge:
        common.die("--bridge is for a Launcher build, %s is a %s release" % (release, manifest["kind"]))
    if manifest.get("kind") == "translations":
        deploy_translations(settings, release, manifest, root)
    elif manifest.get("kind") == "patrons":
        deploy_patrons(settings, release, root)
    else:
        deploy_launcher(settings, release, manifest, root, args.bridge, args.skip_frozen)


def frozen_at(settings):
    """The build the current layout is frozen at (CURRENT_LAYOUT_FROZEN_AT), or None."""
    value = settings["CURRENT_LAYOUT_FROZEN_AT"]
    if not value:
        return None
    if not value.isdigit() or int(value) < 1:
        common.die("CURRENT_LAYOUT_FROZEN_AT must be a build number, not %r" % value)
    return int(value)


def stable_build(directory, settings):
    """The new updater tree's Stable build in a files tree (the local one, or a snapshot), or
    None if it has no tree."""
    if not settings["UPDATER_ROOT"]:
        return None
    tags = Path(directory) / settings["UPDATER_ROOT"] / "tags.json"
    if not tags.is_file():
        return None
    try:
        return formats.check_document("tags.json", tags.read_bytes())["tags"]["stable"]
    except formats.Invalid as e:
        common.die("%s: %s" % (tags, e))


def deploy_launcher(settings, release, manifest, root, bridge=False, skip_frozen=False):
    branch, build, version = manifest["branch"], manifest["build_number"], manifest["version_name"]
    platforms = [common.platform(name) for name in manifest["platforms"]]
    log("release %s build %d (%s): %s" % (branch, build, version, " ".join(p.name for p in platforms)))

    tree_base = json.loads((root / "update" / "config.json").read_text())["base_url"]
    if manifest.get("updater_base") != tree_base:
        common.die("the release updates from %s, this tree is %s (wrong --config?)"
                   % (manifest.get("updater_base"), tree_base))
    common.check_release_updater(settings, manifest, release)
    frozen = frozen_at(settings)
    if frozen is not None and skip_frozen:
        log("the current layout is frozen at build %d (CURRENT_LAYOUT_FROZEN_AT); build %d isn't deployed to it"
            % (frozen, build))
        return
    if bridge and branch != BRIDGE_CHANNEL:
        common.die("the bridge must be a build of %s, the only channel Launchers with updater 1 read; this is %s"
                   % (BRIDGE_CHANNEL, branch))

    with common.lock_files(settings):
        last = {}
        for p in platforms:
            index = root / "update" / p.name / branch / "index.json"
            versions = common.channel_versions(index.read_text()) if index.exists() else []
            last[p.name] = max([int(v["Id"]) for v in versions] or [0])
        updater, stable = common.release_updater(manifest)["version"], stable_build(root, settings)
        # what is live only matters (and must be on record) once everything local is right
        live = bridge and frozen is not None and updater == 2 and stable == build
        refusal = common.frozen_layout_refusal(
            frozen, build, last, bridge, updater, stable,
            stable_build(common.published_snapshot(settings), settings) if live else None)
        if refusal:
            common.die(refusal)
        missing = sorted(set(p.name for p in settings.platforms()) - set(last))
        if bridge and missing:
            # a platform without the bridge would stay on the frozen build for good
            common.die("the bridge must have every platform (PLATFORMS); this release lacks %s" % " ".join(missing))
        if bridge:
            log("build %d is the bridge: the current layout takes it, and nothing after it" % build)
        for p in platforms:
            if build <= last[p.name]:
                common.die("%s/%s already has build %d; %d is not newer" % (p.name, branch, last[p.name], build))

        snapshot = common.snapshot_files(settings, "before-deploy-%d" % build, kind="deploy", about=release.name)
        log("snapshot of the tree as it was: %s" % snapshot.name)

        with tempfile.TemporaryDirectory(dir=release, prefix=".deploy.") as work:
            for p in platforms:
                bundle_dir = Path(work) / p.name
                with tarfile.open(release / "deploy" / ("%s.tar.gz" % p.name)) as tar:
                    tar.extractall(bundle_dir, filter="data")
                package = p.package(branch)
                # repoman adds a new channel to channels.json, but doesn't make its directory
                (root / "update" / p.name / branch).mkdir(exist_ok=True)
                log("%s: pushing version %d to %s, then %s to downloads/" % (p.name, build, branch, package))
                repoman = common.container_run(settings) + ["--network", "none",
                           "-v", "%s:/deploy/files" % root, "-v", "%s:/bundle:ro" % bundle_dir,
                           "-v", "%s:/packages:ro" % (release / "packages"),
                           "-w", "/deploy/files", settings["IMAGE_DEPLOYER"], "repoman"]
                common.run(*repoman, "-c", "update", "push", p.name, branch, build, version, "/bundle/%s/" % p.bundle)
                common.run(*repoman, "push-file", "downloads", "/packages/" + package)

        changes = common.tree_changes(snapshot, root)

    log("deployed to %s (not published): %d files changed" % (root, len(changes)))



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



def deploy_patrons(settings, release, root):
    with common.lock_files(settings):
        snapshot = common.snapshot_files(settings, "before-patrons", kind="deploy", about=release.name)
        log("snapshot of the tree as it was: %s" % snapshot.name)
        shutil.copyfile(release / "patrons.txt", root / "patrons.txt")
        changes = common.tree_changes(snapshot, root)

    log("deployed to %s (not published): %d files changed" % (root, len(changes)))


if __name__ == "__main__":
    main()
