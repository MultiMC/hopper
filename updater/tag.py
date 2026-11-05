#!/usr/bin/env python3
"""Move the new updater's Testing or Stable tag (the Design repo's Updater.md, "Tag-based
releases and promotion"; the tags document in UpdaterFormats.md).

    updater/tag.py [--config FILE] [--dry-run] testing <build>
    updater/tag.py [--config FILE] [--dry-run] stable <build>

testing  the build becomes the Testing build. It must be newer than the current Testing
         build (and so than Stable): tags only move forward. A bad Testing build is
         replaced by a newer one, or removed (updater/remove.py).
stable   the build becomes Stable: any build newer than the current Stable. When Testing is
         then older than it, Testing moves to it too (Testing resolves to Stable).

The build must be in the tree and not withdrawn. The tags document is rewritten with its
serial one higher and signed with the online key; the tree as it would be is checked with
the validator first, and nothing is written if that fails. Then the tree is snapshotted and
the tags document written, and the download archives of every tag that moved are written
from the tree (lib/updater_downloads.py): downloads/updater/mmc-<tag>-<platform>, and for Stable also
downloads/updater/archive/<build>/, which keeps it for good. A Stable build that has no archive yet
(the first one, set by updater/deploy.py) gets one when Stable moves on.

Naming the build a tag already has changes no tags; it writes that tag's download archives
again (after an interrupted run, say). Nothing is published: files/publish.py does that.

--dry-run   checks everything and shows the tags as they would be, writes nothing
"""

import argparse
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_downloads as downloads
import updater_formats as formats
import updater_tree

log = common.logger("tag")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("tag", choices=("testing", "stable"))
    parser.add_argument("build", type=int)
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    tree = updater_tree.Tree(settings)
    with common.lock_files(settings):
        current = tree.tags()
        if current is None:
            common.die("%s has no tags document yet; updater/deploy.py starts a tree" % tree.root)
        tags, build = dict(current["tags"]), args.build
        if build in current["withdrawn"]:
            common.die("build %d was withdrawn" % build)
        if build not in tree.builds():
            common.die("build %d isn't in the tree %s" % (build, tree.root))
        old_stable = tags["stable"]

        if tags[args.tag] == build:
            log("%s is already build %d; writing its download archives again" % (args.tag, build))
            moved = [args.tag]
        else:
            if args.tag == "testing":
                # Testing is never older than Stable, so this keeps it newer than Stable too
                if build < tags["testing"]:
                    common.die("Testing is build %d; tags only move forward, %d is older" % (tags["testing"], build))
                tags["testing"] = build
            else:
                if build < tags["stable"]:
                    common.die("Stable is build %d; tags only move forward, %d is older" % (tags["stable"], build))
                tags["stable"] = build
                tags["testing"] = max(tags["testing"], build)
            moved = [tag for tag in ("testing", "stable") if tags[tag] != current["tags"][tag]]
        log("%stags: %s -> %s" % ("(dry run) " if args.dry_run else "", updater_tree.show_tags(current["tags"]), updater_tree.show_tags(tags)))

        changes = {}
        if tags != current["tags"]:
            if not args.dry_run:
                tree.clean_incoming()
            changes = tree.tags_changes(tags, current["withdrawn"], updater_tree.now())
            try:
                summary = tree.gate(changes, hashes=False)
            except formats.Invalid as e:
                common.die("the tree wouldn't be valid, nothing written: %s" % e)
            log("the tree with these tags would be valid: %s" % summary)
        archives = [b for b in sorted({tags["stable"], old_stable}) if "stable" in moved]
        archives = [b for b in archives if b == tags["stable"] or b not in downloads.archived_builds(settings)]
        log("download archives to write: %s" % ", ".join(["mmc-%s-*" % tag for tag in moved]
                                                         + ["archive/%d/" % b for b in archives]))
        if args.dry_run:
            log("dry run: nothing written")
            return

        snapshot = common.snapshot_files(settings, "before-tag-%s-%d" % (args.tag, build), kind="deploy",
                                         about="%s -> %d" % (args.tag, build))
        log("snapshot of the tree as it was: %s" % snapshot.name)
        if changes:
            tree.commit(changes)
            try:
                tree.gate({}, hashes=False)
            except formats.Invalid as e:
                common.die("the tree as written isn't valid: %s; don't publish it (files/snapshots.py restore %s)"
                           % (e, snapshot.name))
        for tag in moved:
            for path in downloads.write_tag(settings, tree, tag, tags[tag]):
                log("wrote %s" % path.relative_to(settings["FILES_ROOT"]))
        for b in archives:
            for path in downloads.write_archive(settings, tree, b):
                log("wrote %s" % path.relative_to(settings["FILES_ROOT"]))
    log("%s is build %d in %s (not published)" % (args.tag, build, tree.root))


if __name__ == "__main__":
    main()
