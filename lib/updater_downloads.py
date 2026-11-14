"""The download archives that go with the new updater's tags, in the files tree's downloads/,
next to the current layout's (downloads/mmc-develop-*, and the mmc-stable-* frozen since 2023,
which must not change; every name here starts with MultiMC-, so none of them collide):

    downloads/MultiMC-<variant>.<ext>                Stable's build: what the website offers
    downloads/MultiMC-<tag>-<variant>.<ext>          Testing's and Latest's
    downloads/archive/<version>/MultiMC-<version>-<variant>.<ext>
                                                     every build that has been Stable

<variant> is the variant ID (UpdaterFormats.md: linux-x86_64-qt5, windows-x86-qt5.15, ...),
<version> the build's version (5.0.5123), or its build number for builds made before versions
had one (decided 2026-11-14, the Design repo's Updater.md, The cutover).

They are made from the updater's tree itself (the build manifest and its content files), so a
download holds exactly what Launchers install, and the same build always gives the same bytes:
entries in path order, owned by root, every time the build's `built` time. Linux and macOS
get .tar.gz, Windows .zip (lib/common.py, PLATFORMS). A build that has been Stable keeps its
archive for good: it is also the record of which builds were Stable, which the retention
(updater/prune.py) never purges. They are not part of the updater's tree (UpdaterFormats.md);
files/publish.py publishes them with the rest of downloads/.
"""

import datetime
import gzip
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

import common
import updater_formats as formats

# Written next to the target under this prefix, then renamed into place; what an interrupted
# write leaves is removed by the next one
TEMPORARY = ".tmp-"


PREFIX = "MultiMC-"


def downloads_dir(settings):
    return Path(settings["FILES_ROOT"]) / "downloads"


def tag_stem(tag, variant):
    """A tag's download, relative to downloads/, without the extension."""
    return PREFIX + variant if tag == "stable" else "%s%s-%s" % (PREFIX, tag, variant)


def archive_label(doc):
    """The name of a build's archive directory: its version, or its build number."""
    return doc.get("version") or str(doc["build"])


def archive_stem(doc, variant):
    """A build's archived download, relative to downloads/, without the extension."""
    label = archive_label(doc)
    return "archive/%s/%s%s-%s" % (label, PREFIX, label, variant)


def archive_dir(settings, doc):
    return downloads_dir(settings) / "archive" / archive_label(doc)


def label_build(label):
    """The build number an archive directory's name stands for, or None for any other name."""
    if label.isdigit():
        return int(label)
    m = formats.BUILD_VERSION.fullmatch(label)
    return int(m.group(1)) if m else None


def archive_dirs(settings):
    """{build: directory} of the archives in downloads/archive/: only directories named like one,
    holding MultiMC-* files (anything else there isn't ours)."""
    directory = downloads_dir(settings) / "archive"
    if not directory.is_dir():
        return {}
    found = {}
    for p in directory.iterdir():
        build = label_build(p.name)
        if build is not None and p.is_dir() and any(p.glob(PREFIX + "*")):
            found[build] = p
    return found


def archived_builds(settings):
    """The builds that have been Stable: those with an archive."""
    return set(archive_dirs(settings))


def platform_of(variant):
    for p in common.PLATFORMS.values():
        if p.variant == variant:
            return p
    common.die("no platform has the variant %s (lib/common.py, PLATFORMS)" % variant)


def write_tag(settings, tree, tag, build):
    """The tag's download for every variant of the build; a variant's download from an earlier
    build that this one lacks is removed. Returns the names written."""
    doc = tree.build(build)
    directory = downloads_dir(settings)
    written = write_archives(tree, doc, directory, lambda variant: tag_stem(tag, variant))
    for p in common.PLATFORMS.values():
        stale = directory / ("%s.%s" % (tag_stem(tag, p.variant), p.archive))
        if p.variant not in doc["variants"] and stale.exists():
            common.emit("build %d has no %s, removing %s" % (build, p.variant, stale.name))
            stale.unlink()
    return written


def write_archive(settings, tree, build):
    """downloads/archive/<version>/, for a build that is or has been Stable."""
    doc = tree.build(build)
    return write_archives(tree, doc, downloads_dir(settings), lambda variant: archive_stem(doc, variant))


def remove_archive(settings, build):
    directory = archive_dirs(settings).get(build)
    if directory is None:
        return False
    shutil.rmtree(directory)
    return True


def write_archives(tree, doc, downloads, stem):
    """Each variant's download at downloads/<stem(variant)>.<ext>."""
    mtime = int(datetime.datetime.strptime(doc["built"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=datetime.timezone.utc).timestamp())
    written = []
    for variant_id, variant in sorted(doc["variants"].items()):
        p = platform_of(variant_id)
        target = downloads / ("%s.%s" % (stem(variant_id), p.archive))
        target.parent.mkdir(parents=True, exist_ok=True)
        for leftover in target.parent.glob(TEMPORARY + "*"):
            leftover.unlink()
        temporary = target.parent / (TEMPORARY + target.name)
        entries = bundle_entries(tree, p.bundle, variant)
        if p.archive == "zip":
            write_zip(temporary, entries, mtime)
        else:
            write_tar_gz(temporary, entries, mtime)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        written.append(target)
    return written


def bundle_entries(tree, top, variant):
    """[(path, kind, detail)] in path order, under the bundle's top directory: ("dir", None),
    ("file", (content file, executable)), ("link", target)."""
    entries = {top: ("dir", None)}
    for entry in variant["files"]:
        entries[top + "/" + entry["path"]] = ("file", (tree.root / formats.content_path(entry["sha256"]), entry["executable"]))
    for entry in variant["links"]:
        entries[top + "/" + entry["path"]] = ("link", entry["target"])
    for path in list(entries):
        parts = path.split("/")
        for n in range(1, len(parts)):
            entries.setdefault("/".join(parts[:n]), ("dir", None))
    return [(path,) + entries[path] for path in sorted(entries)]


def write_tar_gz(target, entries, mtime):
    with open(target, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=mtime) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path, kind, detail in entries:
            info = tarfile.TarInfo(path)
            info.mtime, info.uid, info.gid, info.uname, info.gname = mtime, 0, 0, "root", "root"
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == "link":
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, detail, 0o777
                tar.addfile(info)
            else:
                source, executable = detail
                info.size, info.mode = source.stat().st_size, 0o755 if executable else 0o644
                with open(source, "rb") as f:
                    tar.addfile(info, f)


def write_zip(target, entries, mtime):
    date_time = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path, kind, detail in entries:
            if kind == "link":
                common.die("%s is a symbolic link, which a .zip can't hold" % path)
            info = zipfile.ZipInfo(path + "/" if kind == "dir" else path, date_time=date_time)
            if kind == "dir":
                info.external_attr = (0o40755 << 16) | 0x10
                z.writestr(info, b"")
            else:
                source, executable = detail
                info.external_attr = (0o100755 if executable else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(source, "rb") as f, z.open(info, "w") as out:
                    shutil.copyfileobj(f, out)
