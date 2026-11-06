#!/usr/bin/env python3
"""Remove a build from the new updater's tree for good: when something ended up in a build
that has to go (the Design repo's Updater.md, "A UI for releases"; UpdaterFormats.md, "Write
order").

    updater/remove.py [--config FILE] [--dry-run] [--serial N] [--json] <build>

The build goes into the tags document's `withdrawn` (build numbers are never reused, so it
stays there), and a tag that names it moves back: Latest to the highest build left, Testing
to the newest build left that is not newer than it and not older than Stable. The Stable
build can't be removed: promote a replacement first (updater/tag.py stable). Launchers on
the build stay on it until the next one; they can tell the user it was withdrawn.

Written in the format's removal order: the tags document first, then the build's manifest
and signature, then the content files no other build lists. The tree as it would be is
checked with the validator first, and nothing changes if that fails; the tree is
snapshotted before. Then the download archives of the tags that moved are written again, and
the build's own archive (downloads/updater/archive/<build>/, if it was ever Stable) is removed.
Nothing is published: files/publish.py does that, and removes the files from the files
server and the bucket.

--dry-run   checks everything and shows what would change, changes nothing
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

log = common.logger("remove")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--serial", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("build", type=int)
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    tree = updater_tree.Tree(settings)
    build = args.build
    with common.lock_files(settings):
        current = tree.tags()
        if current is None:
            common.die("%s has no tags document; there is nothing to remove" % tree.root)
        tree.check_serial(current, args.serial)
        if build in current["withdrawn"]:
            common.die("build %d was withdrawn already" % build)
        present = tree.builds()
        if build not in present:
            common.die("build %d isn't in the tree %s" % (build, tree.root))
        tags = dict(current["tags"])
        if tags["stable"] == build:
            common.die("build %d is Stable and can't be removed; promote a replacement first (updater/tag.py stable)" % build)
        kept = [b for b in present if b != build]
        tags["latest"] = max(kept)
        if tags["testing"] == build:
            tags["testing"] = max(b for b in kept if tags["stable"] <= b < build)
        moved = [tag for tag in ("latest", "testing") if tags[tag] != current["tags"][tag]]
        withdrawn = sorted(current["withdrawn"] + [build])

        changes = tree.tags_changes(tags, withdrawn, updater_tree.now())
        changes.update(tree.removal_changes([build], kept))
        freed = sorted(name for name in changes if name.startswith("files/"))
        log("%sremoving build %d: tags %s -> %s; %d content files no other build lists"
            % ("(dry run) " if args.dry_run else "", build, updater_tree.show_tags(current["tags"]), updater_tree.show_tags(tags), len(freed)))
        try:
            summary = tree.gate(changes, hashes=False)
        except formats.Invalid as e:
            common.die("the tree wouldn't be valid, nothing changed: %s" % e)
        log("the tree without build %d would be valid: %s" % (build, summary))
        archived = build in downloads.archived_builds(settings)
        report = {"serial": current["serial"], "before": current["tags"], "after": tags, "moved": moved, "build": build,
                  "files": len(freed), "bytes": tree.freed_size(changes, [build]), "archive_removed": archived,
                  "dry_run": args.dry_run}
        if args.dry_run:
            log("download archives to write again: %s%s" % (", ".join("mmc-%s-*" % tag for tag in moved) or "none",
                                                           "; archive/%d/ to remove" % build if archived else ""))
            log("dry run: nothing changed")
            if args.json:
                print(json.dumps(report))
            return

        tree.clean_incoming()
        snapshot = common.snapshot_files(settings, "before-remove-%d" % build, kind="deploy", about="remove %d" % build)
        log("snapshot of the tree as it was: %s" % snapshot.name)
        tree.commit(changes)
        try:
            tree.gate({}, hashes=False)
        except formats.Invalid as e:
            common.die("the tree as written isn't valid: %s; don't publish it (files/snapshots.py restore %s)"
                       % (e, snapshot.name))
        for tag in moved:
            for path in downloads.write_tag(settings, tree, tag, tags[tag]):
                log("wrote %s" % path.relative_to(settings["FILES_ROOT"]))
        if downloads.remove_archive(settings, build):
            log("removed downloads/updater/archive/%d/" % build)
    log("build %d is withdrawn and removed from %s (not published)" % (build, tree.root))
    if args.json:
        print(json.dumps(report))


if __name__ == "__main__":
    main()
