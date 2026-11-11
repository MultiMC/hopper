#!/usr/bin/env python3
"""Deploy a Launcher release into the new updater's tree (UPDATER_ROOT in FILES_ROOT, the
Design repo's UpdaterFormats.md): the publisher of the new layout, repoman's replacement.

    updater/deploy.py [--config FILE] [--dry-run] [--if-configured] [--new-tree] <release dir>

Reads the release made by launcher/release-build.py (deploy/<platform>.tar.gz and
manifest.json, with the requirements the compatibility gate measured) and adds it to the
tree as one build for all platforms:

- every file of each platform's bundle as a content file, files/<xx>/<sha256>, unless the
  tree has it already;
- the build manifest builds/<build>.json, one variant per platform, signed with the online
  key (UPDATER_ONLINE_KEY);
- the setup's keys documents (UPDATER_KEYS) the tree doesn't have yet, and a new mirror
  list (FILES_MIRRORS) if it changed;
- the tags document with Latest moved to the build and a higher serial (never lower than
  the time in seconds, lib/updater_tree.py next_serial), signed, last. Testing and Stable
  stay; on a new tree (--new-tree) the build is all three tags.

Before anything is written, the whole tree as it would be is checked with the validator
(lib/updater_formats.py); if that fails, nothing is written. Then the tree is snapshotted
(files/snapshots.py lists and restores the snapshots) and the files are written in the
format's write order, each replaced atomically, the tags document last; the tree is checked
once more as written. Then the download archives of the tags that moved are written from
the tree (lib/updater_downloads.py): downloads/updater/mmc-latest-<platform>, and on a new tree also
Testing's and Stable's, and Stable's archive. Nothing is published: files/publish.py
publishes the tree and the downloads.

Refuses a release that isn't releasable, isn't a Launcher build, was built for another
setup (its UPDATER_BASE, or its updater and pinned root keys), lacks a platform's requirements, or whose build number isn't
higher than the tree's Latest or was withdrawn. A bundle with anything the format can't
express is refused too: a mode other than 0644 or 0755, a hard link, a special file, an
empty directory. The current layout (update/, files/, the mmc-* downloads) isn't touched.

--dry-run   checks everything and shows what would be written, writes nothing
--if-configured
            for setups without a new updater tree (UPDATER_ROOT empty): say so and exit 0
            instead of failing (the release job runs it for every setup)
--new-tree  start the tree with this build: only by hand, for a tree without builds and
            without a tags document. Without it, a tree that has no tags document is
            refused, whatever happened to it; with it, a tree that has one, or has builds,
            is refused
"""

import argparse
import sys
import tarfile
import tempfile
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_downloads as downloads
import updater_formats as formats
import updater_tree

log = common.logger("updater")

MODES = {0o644: False, 0o755: True}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--if-configured", action="store_true")
    parser.add_argument("--new-tree", action="store_true")
    parser.add_argument("release", type=Path)
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    if args.if_configured and not settings["UPDATER_ROOT"]:
        log("this setup has no new updater tree (UPDATER_ROOT is empty); nothing to do")
        return
    tree = updater_tree.Tree(settings)
    release = args.release.resolve()
    manifest = common.read_release(release)
    if "kind" in manifest:
        common.die("%s is a %s release; only Launcher builds go into the updater tree" % (release, manifest["kind"]))
    if manifest.get("updater_base") != settings["UPDATER_BASE"]:
        common.die("the release updates from %s, this setup is %s (wrong --config?)"
                   % (manifest.get("updater_base"), settings["UPDATER_BASE"]))
    common.check_release_updater(settings, manifest, release)
    build = manifest["build_number"]
    platforms = [common.platform(name) for name in manifest["platforms"]]
    log("%srelease %d (%s): %s -> %s" % ("(dry run) " if args.dry_run else "", build, manifest["commit"][:10],
                                         " ".join(p.name for p in platforms), tree.root))

    with common.lock_files(settings):
        current = tree.tags()
        if current:
            if args.new_tree:
                common.die("%s has a tags document already: not a new tree (--new-tree)" % tree.root)
            if build <= current["tags"]["latest"]:
                common.die("the tree's Latest is build %d; %d is not newer" % (current["tags"]["latest"], build))
            if build in current["withdrawn"]:
                common.die("build %d was withdrawn; build numbers are never reused" % build)
            tags = {**current["tags"], "latest": build}
            withdrawn = current["withdrawn"]
        else:
            # Never by itself: a tree that lost its tags document would get the newest build
            # as Stable, with a serial Launchers have passed long ago
            if not args.new_tree:
                common.die("%s has no tags document; if this is its first build, start the tree with --new-tree "
                           "(the build becomes Latest, Testing and Stable)" % tree.root)
            if tree.builds():
                common.die("%s isn't a new tree: it has builds (%s), but no tags document"
                           % (tree.root, " ".join(map(str, tree.builds()))))
            log("a new tree: build %d becomes Latest, Testing and Stable" % build)
            tags = {"latest": build, "testing": build, "stable": build}
            withdrawn = []
        if not args.dry_run:
            tree.clean_incoming()

        with tempfile.TemporaryDirectory(dir=release, prefix=".updater.") as work:
            now = updater_tree.now()
            doc, contents = build_manifest(release, manifest, platforms, Path(work))
            # Content files the tree has already stay as they are, and are checked as they are
            changes = {formats.content_path(sha256): path for sha256, path in contents.items()
                       if not (tree.root / formats.content_path(sha256)).exists()}
            changes.update(tree.sign("builds/%d.json" % build, doc))
            changes.update(tree.keys_changes())
            changes.update(tree.mirrors_changes(now))
            changes.update(tree.tags_changes(tags, withdrawn, now))
            new = sorted(name for name in changes if not (tree.root / name).is_file())
            log("%d content files, %d of them new; documents: %s"
                % (len(contents), sum(1 for name in changes if name.startswith("files/")),
                   " ".join(name for name in sorted(changes, key=updater_tree.write_order)
                            if not name.startswith("files/") and not name.endswith(".sig"))))
            try:
                summary = tree.gate(changes)
            except formats.Invalid as e:
                common.die("the tree wouldn't be valid, nothing written: %s" % e)
            log("the tree with build %d would be valid: %s" % (build, summary))
            if args.dry_run:
                for name in sorted(new, key=updater_tree.write_order):
                    if not name.startswith("files/"):
                        print("    " + name)
                log("dry run: nothing written")
                return

            snapshot = common.snapshot_files(settings, "before-updater-%d" % build, kind="deploy", about=release.name)
            log("snapshot of the tree as it was: %s" % snapshot.name)
            written = tree.commit(changes)

        try:
            summary = tree.gate({})
        except formats.Invalid as e:
            common.die("the tree as written isn't valid: %s; don't publish it (files/snapshots.py restore %s)"
                       % (e, snapshot.name))
        written_downloads = []
        for tag in ("latest", "testing", "stable"):
            if not current or current["tags"][tag] != tags[tag]:
                written_downloads += downloads.write_tag(settings, tree, tag, tags[tag])
        if not current:
            written_downloads += downloads.write_archive(settings, tree, build)
        log("download archives: %s" % " ".join(str(path.relative_to(settings["FILES_ROOT"])) for path in written_downloads))
    log("deployed build %d to %s (not published): %d files written; %s" % (build, tree.root, written, summary))


def build_manifest(release, manifest, platforms, work):
    """The build manifest for the release (unsigned) and its content files, {sha256: path},
    from the deploy archives unpacked into work."""
    variants, contents = {}, {}
    for p in platforms:
        info = manifest["platforms"][p.name]
        if "requires" not in info:
            common.die("%s: the manifest has no requirements for %s (a release from before the gate measured them)"
                       % (release, p.name))
        files, links, found = bundle(release / info["deploy_archive"], p, work / p.name)
        variants[p.variant] = {"name": p.variant_name, "supersedes": list(p.supersedes), "requires": info["requires"], "files": files, "links": links}
        for sha256, path in found.items():
            contents.setdefault(sha256, path)
    doc = {"type": "build", "format": formats.FORMAT, "build": manifest["build_number"],
           "commit": manifest["commit"], "built": manifest["built_at"], "variants": variants}
    # releases built before 2026-11-10 have a version without the build number ("0.7.0"): left out
    version = formats.BUILD_VERSION.fullmatch(manifest["version_name"])
    if version and int(version.group(1)) == manifest["build_number"]:
        doc["version"] = manifest["version_name"]
    return doc, contents


def bundle(archive, p, directory):
    """A platform's deploy archive as a variant's files and links (sorted by path, relative to
    its bundle, the root the current layout's file lists use) and its content files,
    {sha256: path} in the unpacked copy in directory."""
    files, links, contents, directories = [], [], {}, set()
    prefix = p.bundle + "/"
    try:
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            tar.extractall(directory, filter="data")
    except (OSError, tarfile.TarError) as e:
        common.die("%s: %s" % (archive, e))
    for member in members:
        if member.name == p.bundle and member.isdir():
            continue
        if not member.name.startswith(prefix):
            common.die("%s: %s is outside the bundle %s/" % (archive, member.name, p.bundle))
        path = member.name[len(prefix):]
        if member.isdir():
            directories.add(path)
        elif member.issym():
            links.append({"path": path, "target": member.linkname})
        elif member.isreg():
            if member.mode not in MODES:
                common.die("%s: %s has mode %o; the format has only 0644 and 0755" % (archive, member.name, member.mode))
            source = directory / member.name
            sha256 = common.sha256_file(source)
            files.append({"path": path, "sha256": sha256, "size": source.stat().st_size,
                          "executable": MODES[member.mode]})
            contents[sha256] = source
        else:
            common.die("%s: %s is neither a file, a directory nor a symbolic link" % (archive, member.name))
    listed = [entry["path"] for entry in files + links]
    used = {"/".join(path.split("/")[:n]) for path in listed for n in range(1, path.count("/") + 1)}
    empty = sorted(directories - used)
    if empty:
        common.die("%s: empty directories can't be expressed in the format: %s" % (archive, ", ".join(empty)))
    return sorted(files, key=lambda e: e["path"]), sorted(links, key=lambda e: e["path"]), contents


if __name__ == "__main__":
    main()
