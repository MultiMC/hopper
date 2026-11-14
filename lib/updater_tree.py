"""The new updater's tree in the local files tree (UPDATER_ROOT in FILES_ROOT): reading what
is there, signing new documents with the online key, checking the tree as it would be, and
writing the changes in the order the Design repo's UpdaterFormats.md gives ("Write order").

The publisher (updater/deploy.py) adds builds with it; the tag operations work on the same
tree the same way: make the new documents, check the whole tree with them (gate), then
write them (commit). Nothing is written unless the tree as it would be passes the
validator, and the tags document, the only pointer, is always written last.
"""

import datetime
import os
import shutil
from pathlib import Path

import common
import updater_formats as formats

# Directories of FILES_ROOT that belong to the current layout and the other releases
NOT_UPDATER_ROOT = {"update", "files", "downloads", "translations"}
# Where files are written before they are renamed into place, in the tree's root (left
# behind only by an interrupted write; the next run removes it)
INCOMING = ".incoming"


class Tree:
    """A setup's tree: its root, its keys documents (UPDATER_KEYS), the online key and the
    mirror list it publishes. Stops with an error if the setup has none or it is incomplete."""

    def __init__(self, settings):
        name = settings["UPDATER_ROOT"]
        if not name:
            common.die("UPDATER_ROOT is empty: this setup has no new updater tree")
        if "/" in name or name.startswith(".") or name in NOT_UPDATER_ROOT:
            common.die("UPDATER_ROOT must be one new directory of FILES_ROOT, not %r" % name)
        self.files_root = Path(settings["FILES_ROOT"])
        self.root = self.files_root / name
        self.name = name
        self.online_key = Path(settings["UPDATER_ONLINE_KEY"])
        if not settings["UPDATER_ONLINE_KEY"] or not self.online_key.is_file():
            common.die("no online key: UPDATER_ONLINE_KEY (%s) must be its private key" % settings["UPDATER_ONLINE_KEY"])
        self.keys = keys_documents(settings["UPDATER_KEYS"])
        # the mirror list: the files site's base URLs, each a group of its own, in order
        self.mirrors = [[url] for url in common.site_urls(settings["FILES_MIRRORS"])]
        try:
            self.online_public = formats.public_key(self.online_key)
        except formats.Invalid as e:
            common.die(str(e))
        newest = self.newest_keys()
        if self.online_public not in newest["online_keys"]:
            common.die("the online key %s (%s) isn't an online key of keys/%d.json"
                       % (self.online_key, self.online_public, newest["version"]))

    def newest_keys(self):
        return self.keys[-1][1]

    def floor(self):
        """The newest keys document's bytes: what a Launcher built now would embed."""
        return self.keys[-1][2]

    def read(self, name):
        """A document of the tree as it is (bytes), or None."""
        path = self.root / name
        return path.read_bytes() if path.is_file() else None

    def builds(self):
        """The numbers of the builds in the tree, ascending."""
        directory = self.root / "builds"
        if not directory.is_dir():
            return []
        return sorted(formats.kind_of("builds/" + p.name)[1] for p in directory.iterdir() if p.name.endswith(".json"))

    def build(self, number):
        """A build's manifest as it is in the tree."""
        name = "builds/%d.json" % number
        data = self.read(name)
        if data is None:
            common.die("build %d isn't in the tree %s" % (number, self.root))
        return formats.check_document(name, data)

    def content_sizes(self, numbers):
        """{sha256: size} of the content files the given builds list."""
        listed = {}
        for number in numbers:
            listed.update(formats.content_files(self.build(number)))
        return listed

    def removal_changes(self, removed, kept):
        """Changes that take the builds `removed` out of the tree: their manifests and
        signatures, and the content files no build in `kept` lists (None: remove)."""
        changes = {}
        for number in removed:
            changes["builds/%d.json" % number] = None
            changes["builds/%d.json.sig" % number] = None
        still_listed = self.content_sizes(kept)
        for sha256 in self.content_sizes(removed):
            if sha256 not in still_listed:
                changes[formats.content_path(sha256)] = None
        return changes

    def tags(self):
        """The tags document as it is, or None for a tree without one (a new tree)."""
        data = self.read("tags.json")
        return formats.check_document("tags.json", data) if data is not None else None

    def check_serial(self, current, serial):
        """Stop unless the tags document (as tags() gave it) has this serial, if one is given:
        the tree is still what someone looked at when deciding."""
        if serial is not None and current["serial"] != serial:
            common.die("the tags have changed: their serial is %d, not %d" % (current["serial"], serial))

    def freed_size(self, changes, numbers):
        """The bytes of the content files of these builds that the changes remove."""
        return sum(size for sha256, size in self.content_sizes(numbers).items() if formats.content_path(sha256) in changes)

    def sign(self, name, doc):
        """A document and its signature file, as bytes: checked, signed with the online key,
        and the signature checked."""
        data = formats.dump_json(doc)
        try:
            formats.check_document(name, data)
            signature = formats.sign(data, self.online_key).encode("ascii")
            formats.verify(data, signature, {self.online_public})
        except formats.Invalid as e:
            common.die("%s: %s" % (name, e))
        return {name: data, name + ".sig": signature}

    def keys_changes(self):
        """The keys documents UPDATER_KEYS has and the tree doesn't yet. Those the tree has
        must be the same, signatures included."""
        changes = {}
        for version, _, data, signature in self.keys:
            name = "keys/%d.json" % version
            have, have_signature = self.read(name), self.read(name + ".sig")
            if have is None and have_signature is None:
                changes.update({name: data, name + ".sig": signature})
            elif (have, have_signature) != (data, signature):
                common.die("%s in the tree isn't the one in UPDATER_KEYS" % name)
        on_disk = len(list((self.root / "keys").glob("*.json"))) if (self.root / "keys").is_dir() else 0
        if on_disk > len(self.keys):
            common.die("the tree has keys documents that UPDATER_KEYS doesn't (%d, it has %d)" % (on_disk, len(self.keys)))
        return changes

    def mirrors_changes(self, now):
        """A new mirror list if FILES_MIRRORS differs from the tree's (or it has none)."""
        data = self.read("mirrors.json")
        if data is not None and formats.check_document("mirrors.json", data)["mirrors"] == self.mirrors:
            return {}
        return self.sign("mirrors.json", {"type": "mirrors", "format": formats.FORMAT, "issued": now,
                                          "mirrors": self.mirrors})

    def tags_changes(self, tags, withdrawn, now):
        """The tags document with these tags ({latest, testing, stable}) and withdrawn builds,
        its serial higher than the tree's (next_serial)."""
        current = self.tags()
        return self.sign("tags.json", {"type": "tags", "format": formats.FORMAT,
                                       "serial": next_serial(current, now), "issued": now,
                                       "tags": tags, "withdrawn": withdrawn})

    def gate(self, changes, hashes=True):
        """Check the whole tree as it would be with the changes ({name: bytes or Path}) written,
        against the newest keys document as the floor. Returns the validator's summary; raises
        formats.Invalid on the first problem."""
        summary, warnings = formats.check_tree(self.root, self.floor(), hashes=hashes, overlay=changes)
        for warning in warnings:
            common.emit("warning: %s" % warning)
        return summary

    def commit(self, changes):
        """Write the changes in the write order: content files, build manifests, keys
        documents and the mirror list, the tags document last. Each file is replaced
        atomically. Then remove the files whose change is None, the other way round: build
        manifests (each before its signature), then content files; so a removal's new tags
        document is written before the builds it no longer names go. The caller holds the
        files tree's lock and has checked them (gate)."""
        incoming = self.root / INCOMING
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.mkdir(parents=True)
        written = 0
        try:
            for name in sorted((n for n in changes if changes[n] is not None), key=write_order):
                source = changes[name]
                target = self.root / name
                temporary = incoming / name.replace("/", "_")
                if isinstance(source, bytes):
                    temporary.write_bytes(source)
                else:
                    shutil.copyfile(source, temporary)
                os.chmod(temporary, 0o644)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
                written += 1
        finally:
            shutil.rmtree(incoming)
        for name in sorted((n for n in changes if changes[n] is None), key=removal_order):
            target = self.root / name
            target.unlink(missing_ok=True)
            if name.startswith("files/") and not any(target.parent.iterdir()):
                target.parent.rmdir()
            written += 1
        return written

    def clean_incoming(self):
        """Remove what an interrupted write left behind (the caller holds the lock)."""
        incoming = self.root / INCOMING
        if incoming.exists():
            common.emit("removing %s, left behind by an interrupted write" % incoming)
            shutil.rmtree(incoming)


# The user agent the Launcher sends (BuildConfig.USER_AGENT): hosts and caches may answer some
# clients differently (Cloudflare gave Python's default one a 403)
LAUNCHER_USER_AGENT = "MultiMC/5.0"


def http_status(url):
    """The HTTP status a GET of url gets, as the Launcher would ask, or the error (a string)
    when there is no answer at all."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": LAUNCHER_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:
        return str(getattr(e, "reason", e))


def probe_next_keys(settings, log, status=http_status):
    """Ask every base URL Launchers use (the tree's mirror list and FILES_MIRRORS) for the
    signature of the keys document after the newest, as every update check does
    (UpdaterFormats.md, Serving). The walk only ends well when some mirror answers 404, so:
    none does -> stop with each answer (every check of every Launcher fails); some don't ->
    a warning for each (checks still work, by a detour). Returns {url: answer}."""
    root = Path(settings["FILES_ROOT"], settings["UPDATER_ROOT"])
    versions = []
    for path in (root / "keys").glob("*.json"):
        m = formats.KEYS_NAME.fullmatch("keys/" + path.name)
        if m:
            versions.append(int(m.group(1)))
    if not versions:
        return {}
    name = "%s/keys/%d.json.sig" % (settings["UPDATER_ROOT"], max(versions) + 1)
    bases = list(common.site_urls(settings["FILES_MIRRORS"]))
    data = (root / "mirrors.json").read_bytes() if (root / "mirrors.json").is_file() else None
    if data is not None:
        for group in formats.check_document("mirrors.json", data)["mirrors"]:
            bases += [base for base in group if base not in bases]
    answers = {base + name: status(base + name) for base in bases}
    others = {url: answer for url, answer in answers.items() if answer != 404}
    if len(others) == len(answers):
        common.die("no mirror answers 404 for %s, so every Launcher's update check fails: %s"
                   % (name, "; ".join("%s: %s" % item for item in sorted(answers.items()))))
    for url, answer in sorted(others.items()):
        log("warning: %s answers %s, not 404: Launchers' checks take a detour through the other mirrors" % (url, answer))
    log("the next keys document (%s) is a 404 on %d of %d base URLs" % (name, len(answers) - len(others), len(answers)))
    return answers


def check_continuity(moves, allow, log):
    """Refuse a tag move that would strand installations (updater_formats.continuity), for
    each (old manifest, new manifest) pair, unless `allow` (--allow-discontinuity: when it's
    meant). Variants retired in hopper's platform table (common.PLATFORMS) may go. Warnings
    are only logged."""
    found = []
    for old, new in moves:
        problems, warnings = formats.continuity(old, new, common.retired_variants())
        for warning in warnings:
            log("warning: %s" % warning)
        found += problems
    if not found:
        return
    if allow:
        for problem in found:
            log("allowed (--allow-discontinuity): %s" % problem)
        return
    common.die("installations would be left without an update, nothing written: %s. A variant dropped on "
               "purpose is retired in lib/common.py PLATFORMS; for anything else that's meant (a stricter "
               "requirement), run it again with --allow-discontinuity" % "; ".join(found))


def write_order(name):
    """Content files, build manifests, keys documents and the mirror list, then the tags
    document; within each, a document before its signature."""
    if name.startswith("files/"):
        stage = 0
    elif name.startswith("builds/"):
        stage = 1
    elif name.startswith("keys/") or name.startswith("mirrors.json"):
        stage = 2
    elif name.startswith("tags.json"):
        stage = 3
    else:
        raise ValueError("%s isn't part of the tree" % name)
    return (stage, name)


def removal_order(name):
    """Build manifests (a document before its signature), then content files."""
    if name.startswith("builds/"):
        return (0, name)
    if name.startswith("files/"):
        return (1, name)
    raise ValueError("%s can't be removed from the tree" % name)


def keys_documents(directory):
    """The setup's keys documents, [(version, doc, bytes, signature bytes)] from 1 to N.
    The validator checks their chain and signatures with the rest of the tree."""
    if not directory or not Path(directory).is_dir():
        common.die("no keys documents: UPDATER_KEYS (%s) must be the setup's directory of them" % directory)
    found = {}
    for path in Path(directory).iterdir():
        if path.name.endswith(".sig"):
            continue
        try:
            _, version = formats.kind_of("keys/" + path.name)
            data = path.read_bytes()
            found[version] = (version, formats.check_document("keys/" + path.name, data), data,
                              Path(str(path) + ".sig").read_bytes())
        except (formats.Invalid, OSError) as e:
            common.die("%s: %s" % (path, e))
    if not found or sorted(found) != list(range(1, len(found) + 1)):
        common.die("%s: the keys documents must be 1.json to N.json without gaps" % directory)
    return [found[v] for v in sorted(found)]


def next_serial(current, now):
    """The serial of the tags document written at `now` (as now() gives it) after `current`
    (None on a new tree): one higher than the tree's, and never lower than the time in
    seconds. Launchers refuse a serial lower than one they have seen, so a tree that was
    restored from a snapshot or started again must not count on from a low number: with the
    time in it, its next tags document is above everything published before."""
    seconds = int(datetime.datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp())
    return max(current["serial"] + 1 if current else 1, seconds)


def check_published_serial(published, root, name):
    """Stop unless the tags document of the tree `name` in the files tree `root` may replace
    the one in the snapshot `published` (what is live): a changed document needs a higher
    serial, and one that is live can't go away. Launchers that have seen the live serial
    refuse a lower one on every check, and nothing tells their users."""
    old, new = Path(published) / name / "tags.json", Path(root) / name / "tags.json"
    if not old.is_file():
        return
    if not new.is_file():
        common.die("%s is published, but the tree no longer has it; Launchers would find no tags" % old.relative_to(published))
    old_data, new_data = old.read_bytes(), new.read_bytes()
    if old_data == new_data:
        return
    try:
        old_serial = formats.check_document("tags.json", old_data)["serial"]
        new_serial = formats.check_document("tags.json", new_data)["serial"]
    except formats.Invalid as e:
        common.die("%s/tags.json: %s" % (name, e))
    if new_serial <= old_serial:
        common.die("%s/tags.json has serial %d, but serial %d is published: Launchers that have seen it would refuse "
                   "this one (a restored snapshot?). The next deploy or tag move writes a higher serial"
                   % (name, new_serial, old_serial))


def show_tags(tags):
    return "latest %d, testing %d, stable %d" % (tags["latest"], tags["testing"], tags["stable"])


def now():
    """The current time as the formats write it."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
