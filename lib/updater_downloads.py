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

Next to them, downloads/builds.html lists the builds that have downloads: their tags,
commit and each platform's download. A static page in the website's look, linked from it.
updater/deploy.py, tag.py, remove.py and prune.py write it again after each change
(write_page).
"""

import datetime
import gzip
import html
import os
import shutil
import subprocess
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


PAGE = "builds.html"
SITE = "https://multimc.org/"


def page_builds(settings, tree):
    """The builds that have downloads (a tag's or an archive), newest first, as
    [(build manifest, tags, archived, {variant ID: path relative to downloads/})]."""
    tags = tree.tags()
    if tags is None:
        common.die("%s has no tags document yet: no builds page to write" % tree.root)
    archives = archived_builds(settings)
    named = {}
    for tag, build in tags["tags"].items():
        named.setdefault(build, []).append(tag)
    rows = []
    for number in reversed(tree.builds()):
        build_tags = [tag for tag in ("latest", "testing", "stable") if tag in named.get(number, [])]
        if not build_tags and number not in archives:
            continue
        doc = tree.build(number)
        files = {}
        for variant_id in doc["variants"]:
            p = platform_of(variant_id)
            # the archive's name stays; a tag's moves on to the next build
            stem = archive_stem(doc, variant_id) if number in archives else tag_stem(build_tags[-1], variant_id)
            files[variant_id] = "%s.%s" % (stem, p.archive)
        rows.append((doc, build_tags, number in archives, files))
    return rows


def subjects(commits):
    """{commit: its subject line} from the Launcher's mirror; {} if it can't tell."""
    result = subprocess.run(["git", "--git-dir", str(common.LAUNCHER_MIRROR), "log", "--no-walk=unsorted", "--format=%H %s", *commits],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode:
        return {}
    return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)


def page(settings, tree):
    """downloads/builds.html: the builds with downloads, in the website's look (its stylesheet),
    without scripts."""
    rows = page_builds(settings, tree)
    titles = subjects([doc["commit"] for doc, _, _, _ in rows])
    repository = settings["LAUNCHER_REPO"].removesuffix(".git")
    names = {p.variant: p.name for p in common.PLATFORMS.values()}
    e = html.escape
    body = []
    for doc, build_tags, archived, files in rows:
        badges = " ".join('<span class="uk-badge%s">%s</span>' % ({"stable": " uk-badge-success", "testing": " uk-badge-warning"}.get(tag, ""), tag)
                          for tag in build_tags)
        if archived:
            badges += ' <span class="uk-text-muted">archived</span>'
        commit = '<a href="%s/commit/%s"><code>%s</code></a>' % (e(repository), e(doc["commit"]), e(doc["commit"][:10]))
        if titles.get(doc["commit"]):
            commit += " " + e(titles[doc["commit"]])
        platforms = ", ".join('<a href="%s" title="%s">%s</a>' % (e(path), e(doc["variants"][variant]["name"]), e(names.get(variant, variant)))
                              for variant, path in sorted(files.items(), key=lambda item: names.get(item[0], item[0])))
        body.append("<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td class=\"nowrap\">%s</td><td class=\"platforms\">%s</td></tr>"
                    % (e(doc.get("version") or str(doc["build"])), badges, commit, e(doc["built"][:16].replace("T", " ")), platforms))
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Builds &middot; MultiMC</title>
<link rel="stylesheet" href="{site}assets/css/uikit.multimc.min.css">
<link rel="icon" type="image/png" href="{site}favicon-32x32.png">
<style>.nowrap, .platforms a {{ white-space: nowrap; }}</style>
</head>
<body>
<div class="uk-navbar uk-navbar-attached">
<div class="uk-container uk-container-center uk-margin-top uk-margin-bottom">
<a href="{site}" class="uk-navbar-brand"><img src="{site}images/infinity32.png" alt="MultiMC logo" class="uk-float-left">
<h1 class="top-bar uk-float-left uk-margin-top-remove uk-margin-bottom-remove uk-margin-left">MultiMC</h1></a>
</div>
</div>
<div class="uk-container uk-container-center uk-margin-top">
<h2>Builds</h2>
<div class="uk-overflow-container">
<table class="uk-table uk-table-striped">
<thead><tr><th>Build</th><th>Tags</th><th>Commit</th><th>Built (UTC)</th><th>Platforms</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</div>
</body>
</html>
""".format(site=SITE, rows="\n".join(body))


def write_page(settings, tree):
    """downloads/builds.html, written next to it and renamed into place. Returns its path."""
    target = downloads_dir(settings) / PAGE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (TEMPORARY + target.name)
    temporary.write_text(page(settings, tree))
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
    return target


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
