#!/usr/bin/env python3
"""Move the new updater's Testing or Stable tag (the Design repo's Updater.md, "Tag-based
releases and promotion"; the tags document in UpdaterFormats.md).

    updater/tag.py [--config FILE] [--dry-run] [--serial N] [--json] [--allow-discontinuity] testing <build>
    updater/tag.py [--config FILE] [--dry-run] [--serial N] [--json] [--allow-discontinuity] stable <build>
    updater/tag.py [--config FILE] [--dry-run] [--serial N] [--json] [--allow-discontinuity] testing <build> stable <build>

testing  the build becomes the Testing build. It must be newer than the current Testing
         build (and so than Stable): tags only move forward. A bad Testing build is
         replaced by a newer one, or removed (updater/remove.py).
stable   the build becomes Stable: any build newer than the current Stable. When Testing is
         then older than it, Testing moves to it too (Testing resolves to Stable).

Both named at once are one move, one new tags document: Testing must not be older than the
new Stable.

The build must be in the tree and not withdrawn. The tags document is rewritten with its
serial higher (lib/updater_tree.py next_serial) and signed with the online key; the tree as it would be is checked with
the validator first, and nothing is written if that fails. Then the tree is snapshotted and
the tags document written, and the download archives of every tag that moved are written
from the tree (lib/updater_downloads.py): downloads/MultiMC-<variant> for Stable, MultiMC-<tag>-<variant>
for the others, and for Stable also downloads/archive/<version>/, which keeps it for good. A Stable build that has no archive yet
(the first one, set by updater/deploy.py) gets one when Stable moves on.

Naming the build a tag already has changes no tags; it writes that tag's download archives
again (after an interrupted run, say). Nothing is published: files/publish.py does that.

--dry-run   checks everything and shows the tags as they would be, writes nothing
--serial N  refuse unless the tags document's serial is N: the tags are still those someone
            decided on (any deployed build moves Latest, and so the serial, too)
--json      at the end, print what was done, or would be, as JSON on stdout
--allow-discontinuity
            move the tags even when the installations on a tag's old build would be left
            without an update by its new one (lib/updater_formats.py continuity: a new or
            stricter requirement, a variant that disappears): only when that's meant. A
            variant dropped on purpose is retired in lib/common.py PLATFORMS instead.
            Without it, such a move is refused, with the tree unchanged
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

log = common.logger("tag")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--serial", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-discontinuity", action="store_true")
    parser.add_argument("moves", nargs="+", metavar="TAG BUILD")
    args = parser.parse_args()
    named = parse_moves(args.moves)

    settings = common.load_settings(args.config)
    tree = updater_tree.Tree(settings)
    with common.lock_files(settings):
        current = tree.tags()
        if current is None:
            common.die("%s has no tags document yet; updater/deploy.py --new-tree starts a tree" % tree.root)
        tree.check_serial(current, args.serial)
        tags = dict(current["tags"])
        old_stable = tags["stable"]
        for tag, build in named.items():
            if build in current["withdrawn"]:
                common.die("build %d was withdrawn" % build)
            if build not in tree.builds():
                common.die("build %d isn't in the tree %s" % (build, tree.root))
            if tags[tag] == build:
                log("%s is already build %d; writing its download archives again" % (tag, build))
            elif build < tags[tag]:
                common.die("%s is build %d; tags only move forward, %d is older" % (tag.capitalize(), tags[tag], build))
            tags[tag] = build
        if "stable" in named:
            if "testing" in named and named["testing"] < tags["stable"]:
                common.die("Testing can't be older than Stable: %d, %d" % (named["testing"], tags["stable"]))
            # Testing is never older than Stable; that also keeps a Testing move newer than Stable
            tags["testing"] = max(tags["testing"], tags["stable"])
        moved = [tag for tag in ("testing", "stable") if tags[tag] != current["tags"][tag] or tag in named]
        log("%stags: %s -> %s" % ("(dry run) " if args.dry_run else "", updater_tree.show_tags(current["tags"]), updater_tree.show_tags(tags)))

        # the installations on each moved tag's old build update to its new one
        pairs = sorted({(current["tags"][tag], tags[tag]) for tag in moved if tags[tag] != current["tags"][tag]})
        updater_tree.check_continuity([(tree.build(old), tree.build(new)) for old, new in pairs], args.allow_discontinuity, log)

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
        log("download archives to write: %s" % ", ".join(["%s's" % tag for tag in moved]
                                                         + ["archive/%d/" % b for b in archives]))
        report = {"serial": current["serial"], "before": current["tags"], "after": tags, "moved": moved,
                  "archives": archives, "dry_run": args.dry_run}
        if args.dry_run:
            log("dry run: nothing written")
            if args.json:
                print(json.dumps(report))
            return

        snapshot = common.snapshot_files(settings, "before-tag-" + "-".join("%s-%d" % move for move in named.items()), kind="deploy",
                                         about=", ".join("%s -> %d" % move for move in named.items()))
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
    log("%s in %s (not published)" % (", ".join("%s is build %d" % (tag, tags[tag]) for tag in moved), tree.root))
    if args.json:
        print(json.dumps(report))


def parse_moves(moves):
    """["testing", "12", "stable", "11"] as {tag: build}, Stable first, the order they apply in."""
    if len(moves) % 2:
        common.die("say each tag and its build: testing <build> stable <build>")
    named = {}
    for tag, build in zip(moves[::2], moves[1::2]):
        if tag not in ("testing", "stable"):
            common.die("no tag %s: testing or stable" % tag)
        if tag in named:
            common.die("%s named twice" % tag)
        if not build.isdigit():
            common.die("not a build number: %s" % build)
        named[tag] = int(build)
    return {tag: named[tag] for tag in ("stable", "testing") if tag in named}


if __name__ == "__main__":
    main()
