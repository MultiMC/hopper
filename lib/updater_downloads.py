"""The download archives that go with the new updater's tags, in the files tree's
downloads/updater/, apart from the current layout's downloads (downloads/mmc-develop-*, and the
mmc-stable-* frozen since 2023, which the website links and which must not change):

    downloads/updater/mmc-<tag>-<platform>.<ext>     a tag's build (latest, testing, stable)
    downloads/updater/archive/<build>/mmc-<build>-<platform>.<ext>
                                                     every build that has been Stable

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


def downloads_dir(settings):
    return Path(settings["FILES_ROOT"]) / "downloads" / "updater"


def archive_dir(settings, build):
    return downloads_dir(settings) / "archive" / str(build)


def archived_builds(settings):
    """The builds that have been Stable: those with an archive."""
    directory = downloads_dir(settings) / "archive"
    if not directory.is_dir():
        return set()
    return {int(p.name) for p in directory.iterdir() if p.is_dir() and p.name.isdigit()}


def platform_of(variant):
    for p in common.PLATFORMS.values():
        if p.variant == variant:
            return p
    common.die("no platform has the variant %s (lib/common.py, PLATFORMS)" % variant)


def write_tag(settings, tree, tag, build):
    """downloads/updater/mmc-<tag>-<platform> for every platform of the build; a platform's archive
    from an earlier build that this one lacks is removed. Returns the names written."""
    doc = tree.build(build)
    directory = downloads_dir(settings)
    written = write_archives(tree, doc, directory, "mmc-%s-" % tag)
    for p in common.PLATFORMS.values():
        stale = directory / ("mmc-%s-%s.%s" % (tag, p.name, p.archive))
        if p.variant not in doc["variants"] and stale.exists():
            common.emit("build %d has no %s, removing %s" % (build, p.name, stale.name))
            stale.unlink()
    return written


def write_archive(settings, tree, build):
    """downloads/updater/archive/<build>/, for a build that is or has been Stable."""
    return write_archives(tree, tree.build(build), archive_dir(settings, build), "mmc-%d-" % build)


def remove_archive(settings, build):
    directory = archive_dir(settings, build)
    if directory.exists():
        shutil.rmtree(directory)
        return True
    return False


def write_archives(tree, doc, directory, prefix):
    directory.mkdir(parents=True, exist_ok=True)
    for leftover in directory.glob(TEMPORARY + "*"):
        leftover.unlink()
    mtime = int(datetime.datetime.strptime(doc["built"], "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=datetime.timezone.utc).timestamp())
    written = []
    for variant_id, variant in sorted(doc["variants"].items()):
        p = platform_of(variant_id)
        target = directory / ("%s%s.%s" % (prefix, p.name, p.archive))
        temporary = directory / (TEMPORARY + target.name)
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
