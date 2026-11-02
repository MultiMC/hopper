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
        self.mirrors = mirror_groups(settings["UPDATER_MIRRORS"])
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

    def tags(self):
        """The tags document as it is, or None for a tree without one (a new tree)."""
        data = self.read("tags.json")
        return formats.check_document("tags.json", data) if data is not None else None

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
        """A new mirror list if UPDATER_MIRRORS differs from the tree's (or it has none)."""
        data = self.read("mirrors.json")
        if data is not None and formats.check_document("mirrors.json", data)["mirrors"] == self.mirrors:
            return {}
        return self.sign("mirrors.json", {"type": "mirrors", "format": formats.FORMAT, "issued": now,
                                          "mirrors": self.mirrors})

    def tags_changes(self, tags, withdrawn, now):
        """The tags document with these tags ({latest, testing, stable}) and withdrawn builds,
        its serial one higher than the tree's."""
        current = self.tags()
        return self.sign("tags.json", {"type": "tags", "format": formats.FORMAT,
                                       "serial": current["serial"] + 1 if current else 1, "issued": now,
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
        atomically. The caller holds the files tree's lock and has checked them (gate)."""
        incoming = self.root / INCOMING
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.mkdir(parents=True)
        written = 0
        try:
            for name in sorted(changes, key=write_order):
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
        return written

    def clean_incoming(self):
        """Remove what an interrupted write left behind (the caller holds the lock)."""
        incoming = self.root / INCOMING
        if incoming.exists():
            common.emit("removing %s, left behind by an interrupted write" % incoming)
            shutil.rmtree(incoming)


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


def mirror_groups(setting):
    """UPDATER_MIRRORS as the mirror list's groups: URLs separated by spaces, groups by ";"."""
    groups = [group.split() for group in setting.split(";")]
    if not setting.strip() or not all(groups):
        common.die("UPDATER_MIRRORS must be base URLs, in groups separated by \";\", not %r" % setting)
    return groups


def now():
    """The current time as the formats write it."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
