#!/usr/bin/env python3
"""Keep the new updater's tree under its size cap (the Design repo's Updater.md, "Tag-based
releases and promotion"): purge the oldest builds that nothing needs any more.

    updater/prune.py [--config FILE] [--dry-run] [--if-configured] [--cap-mb N] [--serial N] [--json]

What counts toward the cap (UPDATER_MAX_SIZE_MB) is the content files that only purgeable
builds list. Never purged: the builds the tags name (Latest, Testing, Stable) and every build
that has been Stable (it has a download archive, downloads/archive/<version>/,
lib/updater_downloads.py); the files only they list don't count. While the rest is over the
cap, the oldest purgeable build goes: its manifest and signature, then the content files no
build left lists.

A purge is not a removal: the build isn't withdrawn and the tags don't change. A Launcher on a
purged build still updates; it only can't tell which of its files the next build no longer
has.

The tree as it would be is checked with the validator first, and nothing changes if that
fails; the tree is snapshotted before. Nothing is published: files/publish.py does that.

--dry-run   shows what would be purged, changes nothing
--if-configured
            for setups without a new updater tree (UPDATER_ROOT empty): say so and exit 0
            instead of failing (the release job runs it for every setup)
--cap-mb N  this cap, this once, in place of UPDATER_MAX_SIZE_MB
--serial N  refuse unless the tags document's serial is N: the tree is still what someone
            decided on
--json      at the end, print what was done, or would be, as JSON on stdout
"""

import argparse
import json
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_downloads as downloads
import updater_formats as formats
import updater_tree

log = common.logger("prune")

MB = 1024 * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--if-configured", action="store_true")
    parser.add_argument("--cap-mb", type=int)
    parser.add_argument("--serial", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    if args.if_configured and not settings["UPDATER_ROOT"]:
        log("this setup has no new updater tree (UPDATER_ROOT is empty); nothing to do")
        return
    try:
        cap = int(settings["UPDATER_MAX_SIZE_MB"]) * MB
    except ValueError:
        common.die("UPDATER_MAX_SIZE_MB must be a number of megabytes, not %r" % settings["UPDATER_MAX_SIZE_MB"])
    if args.cap_mb is not None:
        if args.cap_mb < 0:
            common.die("--cap-mb can't be negative")
        cap = args.cap_mb * MB
    tree = updater_tree.Tree(settings)
    with common.lock_files(settings):
        current = tree.tags()
        if current is None:
            log("%s has no tags document yet; nothing to prune" % tree.root)
            if args.json:
                print(json.dumps({"purged": [], "builds_total": 0, "protected": 0, "counted": 0, "cap": cap, "bytes": 0, "dry_run": args.dry_run}))
            return
        tree.check_serial(current, args.serial)
        present = tree.builds()
        protected = set(current["tags"].values())
        protected |= downloads.archived_builds(settings) & set(present)
        purgeable = [b for b in present if b not in protected]
        sizes = {b: formats.content_files(tree.build(b)) for b in present}

        def counted(builds):
            kept = {sha for b in protected for sha in sizes[b]}
            return sum({sha: size for b in builds for sha, size in sizes[b].items() if sha not in kept}.values())

        purged = []
        while purgeable and counted(purgeable) > cap:
            purged.append(purgeable.pop(0))
        log("%d builds, %d never purged (tags, archived Stable builds); the rest is %.0f MB of %d MB%s"
            % (len(present), len(protected), counted(purgeable + purged) / MB, cap // MB,
               ("; purging %s" % " ".join(map(str, purged))) if purged else ", nothing to purge"))
        report = {"serial": current["serial"], "purged": purged, "builds_total": len(present), "protected": len(protected),
                  "counted": counted(purgeable + purged), "cap": cap, "bytes": 0, "dry_run": args.dry_run}
        if not purged:
            if args.json:
                print(json.dumps(report))
            return

        changes = tree.removal_changes(purged, [b for b in present if b not in purged])
        try:
            summary = tree.gate(changes, hashes=False)
        except formats.Invalid as e:
            common.die("the tree wouldn't be valid, nothing changed: %s" % e)
        log("the tree without them would be valid: %s" % summary)
        report["bytes"] = tree.freed_size(changes, purged)
        if args.dry_run:
            log("dry run: nothing changed")
            if args.json:
                print(json.dumps(report))
            return

        tree.clean_incoming()
        snapshot = common.snapshot_files(settings, "before-prune", kind="deploy",
                                         about="purge %s" % " ".join(map(str, purged)))
        log("snapshot of the tree as it was: %s" % snapshot.name)
        removed = tree.commit(changes)
        try:
            tree.gate({}, hashes=False)
        except formats.Invalid as e:
            common.die("the tree as written isn't valid: %s; don't publish it (files/snapshots.py restore %s)"
                       % (e, snapshot.name))
    log("purged %d builds, %d files removed from %s (not published)" % (len(purged), removed, tree.root))
    if args.json:
        print(json.dumps(report))


if __name__ == "__main__":
    main()
