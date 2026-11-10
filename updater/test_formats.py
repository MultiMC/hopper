#!/usr/bin/env python3
"""Tests for lib/updater_formats.py and updater/validate.py, on trees made here with
throwaway keys (openssl genpkey). Run from the top of the checkout:

    python3 -m unittest updater/test_formats.py

    updater/test_formats.py --sample <dir>    write the sample tree and its keys, to look at
"""

import copy
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOPPER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOPPER / "lib"))

import updater_formats as formats
from updater_formats import Invalid

NOW = datetime.datetime(2026, 11, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


def make_key(directory, name):
    path = Path(directory) / ("%s.pem" % name)
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(path)], check=True, capture_output=True)
    return path


def make_keys(directory):
    return {name: make_key(directory, name) for name in ("root1", "root2", "root3", "online1", "online2", "stranger")}


def files(*entries):
    """Content for a variant: (path, bytes, executable) → (files list, {sha256: bytes})."""
    listed, contents = [], {}
    for path, data, executable in sorted(entries):
        sha256 = hashlib.sha256(data).hexdigest()
        listed.append({"path": path, "sha256": sha256, "size": len(data), "executable": executable})
        contents[sha256] = data
    return listed, contents


def sample_build(build):
    """A manifest with a Linux, a Windows and a macOS variant, and its content."""
    linux, c1 = files(("bin/MultiMC", b"linux binary %d" % build, True), ("bin/libQt5Core.so.5", b"qt", False),
                      ("MultiMC", b"#!/bin/sh\n", True))
    windows, c2 = files(("MultiMC.exe", b"windows binary %d" % build, True), ("Qt5Core.dll", b"qt dll", False))
    macos, c3 = files(("MultiMC.app/Contents/MacOS/MultiMC", b"mac binary %d" % build, True),
                      ("MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/5/QtCore", b"qt framework", False),
                      ("MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/5/Resources/Info.plist", b"plist", False))
    doc = {
        "type": "build",
        "format": 1,
        "build": build,
        "commit": "%040x" % build,
        "built": "2026-10-30T10:00:00Z",
        "variants": {
            "linux-x86_64-qt5": {
                "name": "Linux, 64-bit",
                "supersedes": [],
                "requires": {"os": "linux", "arch": "x86_64", "glibc": "2.19", "glibcxx": "3.4.21", "qt": "5.6"},
                "files": linux,
                "links": [],
            },
            "windows-x86-qt5.6": {
                "name": "Windows, Qt 5.6",
                "supersedes": [],
                "requires": {"os": "windows", "arch": "x86", "os_version": "5.1"},
                "files": windows,
                "links": [],
            },
            "macos-x86_64-qt5.15": {
                "name": "macOS, Qt 5.15",
                "supersedes": ["macos-x86_64-qt5.6"],
                "requires": {"os": "macos", "arch": "x86_64", "os_version": "10.13"},
                "files": macos,
                "links": [
                    {"path": "MultiMC.app/Contents/Frameworks/QtCore.framework/QtCore", "target": "Versions/Current/QtCore"},
                    {"path": "MultiMC.app/Contents/Frameworks/QtCore.framework/Resources", "target": "Versions/Current/Resources"},
                    {"path": "MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/Current", "target": "5"},
                ],
            },
        },
    }
    return doc, {**c1, **c2, **c3}


def keys_doc(version, roots, online, expires="2028-11-01T00:00:00Z"):
    return {"type": "keys", "format": 1, "version": version, "expires": expires,
            "root_keys": [formats.public_key(k) for k in roots],
            "online_keys": [formats.public_key(k) for k in online]}


class Tree:
    """A tree on disk, written the way the publisher would."""

    def __init__(self, root, keys):
        self.root = Path(root)
        self.keys = keys

    def write(self, name, doc, signers):
        data = doc if isinstance(doc, bytes) else formats.dump_json(doc)
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (self.root / (name + ".sig")).write_text("".join(formats.sign(data, self.keys[s]) for s in signers))

    def content(self, contents):
        for sha256, data in contents.items():
            path = self.root / formats.content_path(sha256)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def build(self, doc, contents, signers=("online1",)):
        self.content(contents)
        self.write("builds/%d.json" % doc["build"], doc, signers)

    def floor(self, version=1):
        return (self.root / ("keys/%d.json" % version)).read_bytes()

    def check(self, floor_version=1):
        return formats.check_tree(self.root, self.floor(floor_version), NOW)


def sample_tree(root, keys):
    tree = Tree(root, keys)
    tree.write("keys/1.json", keys_doc(1, [keys["root1"], keys["root2"]], [keys["online1"]]), ["root1"])
    for build in (4290, 4305, 4312):
        tree.build(*sample_build(build))
    tree.write("tags.json", {"type": "tags", "format": 1, "serial": 1042, "issued": "2026-11-01T11:00:00Z",
                             "tags": {"latest": 4312, "testing": 4305, "stable": 4290}, "withdrawn": [4311]},
               ["online1"])
    tree.write("mirrors.json", {"type": "mirrors", "format": 1, "issued": "2026-11-01T11:00:00Z",
                                "mirrors": [["https://files.example.org/updater/"],
                                            ["https://bucket.example.org/updater/", "https://mirror.example.net/u/"]]},
               ["online1"])
    return tree


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_dir = tempfile.mkdtemp()
        cls.keys = make_keys(cls.key_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_dir)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tree = sample_tree(self.dir, self.keys)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def assertInvalid(self, text, check=None):
        with self.assertRaises(Invalid) as caught:
            (check or self.tree.check)()
        self.assertIn(text, str(caught.exception))

    def edit_json(self, name, change, signers=("online1",)):
        doc = json.loads((self.tree.root / name).read_text())
        change(doc)
        self.tree.write(name, doc, signers)


class TestTree(Base):
    def test_sample_is_valid(self):
        summary, warnings = self.tree.check()
        self.assertEqual(warnings, [])
        self.assertIn("tags serial 1042", summary)

    def test_modified_document(self):
        path = self.tree.root / "builds/4305.json"
        path.write_bytes(path.read_bytes().replace(b"10:00:00", b"10:00:01"))
        self.assertInvalid("builds/4305.json: the signature by")

    def test_modified_signature(self):
        path = self.tree.root / "tags.json.sig"
        text = path.read_text()
        path.write_text(text[:100] + ("0" if text[100] != "0" else "1") + text[101:])
        self.assertInvalid("tags.json: the signature by")

    def test_untrusted_signer(self):
        self.edit_json("tags.json", lambda d: None, signers=("stranger",))
        self.assertInvalid("tags.json: no signature by a trusted key")

    def test_root_key_cant_sign_tags(self):
        self.edit_json("tags.json", lambda d: None, signers=("root1",))
        self.assertInvalid("no signature by a trusted key")

    def test_overlay(self):
        # The tree as it would be with a new build and tags, checked before anything is written
        doc, contents = sample_build(4313)
        overlay = {}
        pending = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, pending)
        for sha256, data in contents.items():
            if not (self.tree.root / formats.content_path(sha256)).exists():
                path = pending / sha256
                path.write_bytes(data)
                overlay[formats.content_path(sha256)] = path
        data = formats.dump_json(doc)
        overlay["builds/4313.json"] = data
        overlay["builds/4313.json.sig"] = formats.sign(data, self.keys["online1"]).encode()
        tags = {"type": "tags", "format": 1, "serial": 1043, "issued": "2026-11-01T12:00:00Z",
                "tags": {"latest": 4313, "testing": 4305, "stable": 4290}, "withdrawn": [4311]}
        data = formats.dump_json(tags)
        overlay["tags.json"] = data
        overlay["tags.json.sig"] = formats.sign(data, self.keys["online1"]).encode()
        floor = self.tree.floor()
        summary, _ = formats.check_tree(self.tree.root, floor, NOW, overlay=overlay)
        self.assertIn("tags serial 1043 (latest 4313", summary)
        self.assertFalse((self.tree.root / "builds/4313.json").exists())
        self.assertIn("tags serial 1042", self.tree.check()[0])

        missing = dict(overlay)
        del missing["builds/4313.json"], missing["builds/4313.json.sig"]
        with self.assertRaises(Invalid) as caught:
            formats.check_tree(self.tree.root, floor, NOW, overlay=missing)
        self.assertIn("latest is build 4313, which isn't in builds/", str(caught.exception))
        content = next(name for name in overlay if name.startswith("files/"))
        overlay[content].write_bytes(b"changed")
        with self.assertRaises(Invalid) as caught:
            formats.check_tree(self.tree.root, floor, NOW, overlay=overlay)
        self.assertIn("%s is 7 bytes" % content, str(caught.exception))

    def test_extra_untrusted_line_is_ignored(self):
        self.edit_json("tags.json", lambda d: None, signers=("stranger", "online1"))
        self.tree.check()

    def test_trusted_line_with_bad_signature(self):
        # Two online keys: a good signature by one doesn't excuse a bad one by the other
        self.tree.write("keys/2.json", keys_doc(2, [self.keys["root1"]], [self.keys["online1"], self.keys["online2"]]),
                        ["root1"])
        data = (self.tree.root / "tags.json").read_bytes()
        good = formats.sign(data, self.keys["online1"])
        bad = formats.sign(b"something else", self.keys["online2"])
        (self.tree.root / "tags.json.sig").write_text(good + bad)
        self.assertInvalid("tags.json: the signature by %s doesn't verify" % formats.public_key(self.keys["online2"]))

    def test_missing_signature(self):
        (self.tree.root / "mirrors.json.sig").unlink()
        self.assertInvalid("mirrors.json has no signature")

    def test_unknown_file(self):
        (self.tree.root / "README").write_text("hi")
        self.assertInvalid("README isn't a document of the tree")

    def test_symlink_in_tree(self):
        (self.tree.root / "builds/4400.json").symlink_to("4312.json")
        self.assertInvalid("is a symbolic link")

    def test_content_in_wrong_directory(self):
        sha = hashlib.sha256(b"x").hexdigest()
        (self.tree.root / "files/00").mkdir(exist_ok=True)
        (self.tree.root / "files/00" / sha).write_bytes(b"x")
        self.assertInvalid("in the wrong directory")

    def test_missing_content(self):
        doc, contents = sample_build(4312)
        sha = doc["variants"]["windows-x86-qt5.6"]["files"][0]["sha256"]
        (self.tree.root / formats.content_path(sha)).unlink()
        self.assertInvalid("%s is missing" % formats.content_path(sha))

    def test_content_hash_mismatch(self):
        doc, contents = sample_build(4312)
        entry = doc["variants"]["linux-x86_64-qt5"]["files"][0]
        (self.tree.root / formats.content_path(entry["sha256"])).write_bytes(b"X" * entry["size"])
        self.assertInvalid("has SHA-256")

    def test_content_size_mismatch(self):
        doc, contents = sample_build(4312)
        entry = doc["variants"]["linux-x86_64-qt5"]["files"][0]
        (self.tree.root / formats.content_path(entry["sha256"])).write_bytes(b"short")
        self.assertInvalid("the manifests say")

    def test_unlisted_content_is_a_warning(self):
        self.tree.content({hashlib.sha256(b"left over").hexdigest(): b"left over"})
        summary, warnings = self.tree.check()
        self.assertEqual(len(warnings), 1)
        self.assertIn("isn't listed by any manifest", warnings[0])

    def test_tag_on_missing_build(self):
        self.edit_json("tags.json", lambda d: d["tags"].update(testing=4300))
        self.assertInvalid("testing is build 4300, which isn't in builds/")

    def test_latest_is_highest(self):
        self.edit_json("tags.json", lambda d: d["tags"].update(latest=4305))
        self.assertInvalid("latest is build 4305, but the highest build is 4312")

    def test_withdrawn_build_present(self):
        self.tree.build(*sample_build(4311))
        self.assertInvalid("build 4311 is withdrawn")

    def test_build_number_matches_name(self):
        doc, contents = sample_build(4312)
        doc["build"] = 4313
        self.tree.content(contents)
        self.tree.write("builds/4312.json", doc, ["online1"])
        self.assertInvalid("but the file is for build 4312")

    def test_tags_served_as_build(self):
        shutil.copy(self.tree.root / "tags.json", self.tree.root / "builds/4312.json")
        shutil.copy(self.tree.root / "tags.json.sig", self.tree.root / "builds/4312.json.sig")
        self.assertInvalid("type is 'tags', expected 'build'")


class TestKeys(Base):
    def test_floor_must_be_in_tree(self):
        floor = formats.dump_json(keys_doc(1, [self.keys["root3"]], [self.keys["online1"]]))
        with self.assertRaises(Invalid) as caught:
            formats.check_tree(self.tree.root, floor, NOW)
        self.assertIn("isn't the same as the floor", str(caught.exception))

    def test_expired(self):
        self.tree.write("keys/1.json", keys_doc(1, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]],
                                                expires="2026-11-01T00:00:00Z"), ["root1"])
        self.assertInvalid("keys/1.json expired")

    def test_expiring_soon_is_a_warning(self):
        self.tree.write("keys/1.json", keys_doc(1, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]],
                                                expires="2026-12-01T00:00:00Z"), ["root1"])
        summary, warnings = self.tree.check()
        self.assertIn("keys/1.json expires at", warnings[0])

    def test_rotation(self):
        # Version 2 drops online1 for online2 and root2 for root3, signed by version 1's root2
        self.tree.write("keys/2.json", keys_doc(2, [self.keys["root1"], self.keys["root3"]], [self.keys["online2"]]),
                        ["root2"])
        self.assertInvalid("tags.json: no signature by a trusted key")
        for name in ("tags.json", "mirrors.json", "builds/4290.json", "builds/4305.json", "builds/4312.json"):
            self.edit_json(name, lambda d: None, signers=("online2",))
        self.tree.check()
        self.tree.check(floor_version=2)

    def test_expired_intermediate_is_fine(self):
        self.tree.write("keys/1.json", keys_doc(1, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]],
                                                expires="2025-01-01T00:00:00Z"), ["root1"])
        self.tree.write("keys/2.json", keys_doc(2, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]]),
                        ["root1"])
        self.tree.check()

    def test_new_roots_cant_sign_their_own_document(self):
        self.tree.write("keys/2.json", keys_doc(2, [self.keys["root3"]], [self.keys["online1"]]), ["root3"])
        self.assertInvalid("keys/2.json: no signature by a trusted key")

    def test_online_key_cant_sign_keys(self):
        self.tree.write("keys/2.json", keys_doc(2, [self.keys["root1"]], [self.keys["online1"]]), ["online1"])
        self.assertInvalid("keys/2.json: no signature by a trusted key")

    def test_gap(self):
        self.tree.write("keys/3.json", keys_doc(3, [self.keys["root1"]], [self.keys["online1"]]), ["root1"])
        self.assertInvalid("without gaps")

    def test_version_matches_name(self):
        self.tree.write("keys/2.json", keys_doc(3, [self.keys["root1"]], [self.keys["online1"]]), ["root1"])
        self.assertInvalid("version is 3, but the file is for version 2")

    def test_key_in_both_lists(self):
        doc = keys_doc(1, [self.keys["root1"]], [self.keys["root1"]])
        with self.assertRaises(Invalid):
            formats.check_keys(doc)


class TestDocuments(unittest.TestCase):
    def setUp(self):
        self.build, _ = sample_build(4312)

    def check(self, doc):
        return formats.check_document("builds/4312.json", formats.dump_json(doc))

    def assertInvalid(self, text, doc=None, name="builds/4312.json", data=None):
        with self.assertRaises(Invalid) as caught:
            formats.check_document(name, data if data is not None else formats.dump_json(doc))
        self.assertIn(text, str(caught.exception))

    def macos(self, doc):
        return doc["variants"]["macos-x86_64-qt5.15"]

    def test_sample(self):
        self.check(self.build)

    def test_unknown_field(self):
        self.build["variants"]["linux-x86_64-qt5"]["files"][0]["mode"] = 493
        self.assertInvalid("unknown mode", self.build)

    def test_version(self):
        # optional: builds from before 2026-11-10 have none
        self.build["version"] = "0.7.4312"
        self.check(self.build)
        for wrong in ("0.7.0", "0.7.4311", "0.7", "0.7.4312.0", "v0.7.4312", "0.07.4312"):
            self.build["version"] = wrong
            self.assertInvalid("version", self.build)
        self.build["version"] = 4312
        self.assertInvalid("version: not a string", self.build)

    def test_unknown_top_field(self):
        self.build["channel"] = "develop"
        self.assertInvalid("unknown channel", self.build)

    def test_missing_field(self):
        del self.build["commit"]
        self.assertInvalid("missing commit", self.build)

    def test_null(self):
        self.build["commit"] = None
        self.assertInvalid("commit: not a string", self.build)

    def test_format_2(self):
        self.build["format"] = 2
        self.assertInvalid("format 2 isn't supported", self.build)

    def test_format_true(self):
        self.build["format"] = True
        self.assertInvalid("isn't supported", self.build)

    def test_duplicate_key(self):
        data = formats.dump_json(self.build).replace(b'"build": 4312,', b'"build": 4312, "build": 4312,')
        self.assertInvalid("duplicate key", data=data)

    def test_float(self):
        data = formats.dump_json(self.build).replace(b'"build": 4312', b'"build": 4312.0')
        self.assertInvalid("isn't an integer", data=data)

    def test_exponent(self):
        data = formats.dump_json(self.build).replace(b'"build": 4312', b'"build": 4.312e3')
        self.assertInvalid("isn't an integer", data=data)

    def test_bool_as_integer(self):
        self.build["variants"]["linux-x86_64-qt5"]["files"][0]["size"] = True
        self.assertInvalid("not an integer", self.build)

    def test_too_big_integer(self):
        self.build["variants"]["linux-x86_64-qt5"]["files"][0]["size"] = 2 ** 53
        self.assertInvalid("outside", self.build)

    def test_bom(self):
        self.assertInvalid("byte order mark", data=b"\xef\xbb\xbf" + formats.dump_json(self.build))

    def test_not_object(self):
        self.assertInvalid("not a JSON object", data=b"[]")

    def test_bad_time(self):
        self.build["built"] = "2026-10-30T10:00:00+01:00"
        self.assertInvalid("built", self.build)
        self.build["built"] = "2026-02-30T10:00:00Z"
        self.assertInvalid("isn't a valid time", self.build)

    def test_too_large(self):
        self.assertInvalid("more than", name="tags.json", data=b" " * (64 * 1024 + 1))

    def test_variant_id_matches_requires(self):
        self.build["variants"]["linux-x86-qt5"] = self.build["variants"].pop("linux-x86_64-qt5")
        self.assertInvalid("the ID doesn't match requires", self.build)

    def test_bad_variant_id(self):
        self.build["variants"]["Linux-x86_64"] = self.build["variants"].pop("linux-x86_64-qt5")
        self.assertInvalid("isn't a valid variant ID", self.build)

    def test_requirements_per_os(self):
        self.build["variants"]["linux-x86_64-qt5"]["requires"]["os_version"] = "5.0"
        self.assertInvalid("unknown os_version", self.build)

    def test_requirements_version(self):
        self.build["variants"]["windows-x86-qt5.6"]["requires"]["os_version"] = "5.01"
        self.assertInvalid("os_version", self.build)

    def test_variant_name(self):
        for name in ["", " leading", "trailing ", "tab\there", "caf\u00e9", "x" * 65]:
            self.macos(self.build)["name"] = name
            self.assertInvalid("name", self.build)
        self.macos(self.build)["name"] = "x" * 64
        self.check(self.build)

    def test_supersedes_itself(self):
        self.macos(self.build)["supersedes"] = ["macos-x86_64-qt5.15"]
        self.assertInvalid("supersedes itself", self.build)

    def test_superseded_twice(self):
        self.build["variants"]["linux-x86_64-qt5"]["supersedes"] = ["macos-x86_64-qt5.6"]
        self.assertInvalid("superseded by both", self.build)

    def test_supersedes_loop(self):
        self.build["variants"]["windows-x86-qt5.6"]["supersedes"] = ["linux-x86_64-qt5"]
        self.build["variants"]["linux-x86_64-qt5"]["supersedes"] = ["windows-x86-qt5.6"]
        self.assertInvalid("loops", self.build)

    def test_supersedes_chain(self):
        self.build["variants"]["windows-x86_64-qt5.15"] = copy.deepcopy(self.build["variants"]["windows-x86-qt5.6"])
        self.build["variants"]["windows-x86_64-qt5.15"]["requires"].update(arch="x86_64", os_version="6.1")
        self.build["variants"]["windows-x86_64-qt5.15"]["supersedes"] = ["windows-x86-qt5.6"]
        self.check(self.build)

    def test_empty_files(self):
        self.build["variants"]["linux-x86_64-qt5"]["files"] = []
        self.assertInvalid("files: 0 entries", self.build)

    def test_unsorted_files(self):
        self.build["variants"]["linux-x86_64-qt5"]["files"].reverse()
        self.assertInvalid("not sorted", self.build)

    def test_paths(self):
        for bad in ("../x", "a//b", "/abs", "a/./b", "a\\b", "C:/x", "a/b/", "tab\there", "\u00e9"):
            build, _ = sample_build(4312)
            build["variants"]["linux-x86_64-qt5"]["files"][0]["path"] = bad
            with self.assertRaises(Invalid, msg=bad):
                self.check(build)

    def test_case_clash(self):
        files = self.build["variants"]["linux-x86_64-qt5"]["files"]
        files.append(dict(files[0], path="BIN/x"))
        files.sort(key=lambda f: f["path"])
        self.assertInvalid("differ only in case", self.build)

    def test_file_is_directory(self):
        files = self.build["variants"]["linux-x86_64-qt5"]["files"]
        files.append(dict(files[0], path="MultiMC/inside"))
        files.sort(key=lambda f: f["path"])
        self.assertInvalid("other paths go through it", self.build)

    def test_executable_must_be_bool(self):
        self.build["variants"]["linux-x86_64-qt5"]["files"][0]["executable"] = 1
        self.assertInvalid("not true or false", self.build)

    def test_link_outside(self):
        self.macos(self.build)["links"][0]["target"] = "../../../../../../etc/passwd"
        self.assertInvalid("points outside the installation", self.build)

    def test_link_dangling(self):
        self.macos(self.build)["links"][2]["target"] = "6"
        self.assertInvalid("which isn't a listed file", self.build)

    def test_link_loop(self):
        links = self.macos(self.build)["links"]
        links[2]["target"] = "Current"
        self.assertInvalid("loops", self.build)

    def test_link_absolute(self):
        self.macos(self.build)["links"][2]["target"] = "/Library"
        self.assertInvalid("leading or trailing /", self.build)

    def test_path_through_link(self):
        files = self.macos(self.build)["files"]
        files.append(dict(files[0], path="MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/Current/QtCore"))
        files.sort(key=lambda f: f["path"])
        self.assertInvalid("other paths go through it", self.build)

    def test_link_and_file_same_path(self):
        links = self.macos(self.build)["links"]
        links.append({"path": "MultiMC.app/Contents/MacOS/MultiMC", "target": "../Resources"})
        self.assertInvalid("listed twice", self.build)

    def test_windows_link(self):
        self.build["variants"]["windows-x86-qt5.6"]["links"] = [{"path": "x", "target": "MultiMC.exe"}]
        self.assertInvalid("a Windows variant has no links", self.build)

    def test_two_sizes_for_one_hash(self):
        files = self.build["variants"]["linux-x86_64-qt5"]["files"]
        other = self.build["variants"]["windows-x86-qt5.6"]["files"]
        other[0] = dict(other[0], sha256=files[0]["sha256"], size=files[0]["size"] + 1)
        doc = self.check(self.build)
        with self.assertRaises(Invalid):
            formats.content_files(doc)


class TestTagsAndMirrors(unittest.TestCase):
    def tags(self, **change):
        doc = {"type": "tags", "format": 1, "serial": 1, "issued": "2026-11-01T11:00:00Z",
               "tags": {"latest": 3, "testing": 2, "stable": 1}, "withdrawn": []}
        doc.update(change)
        return formats.check_document("tags.json", formats.dump_json(doc))

    def mirrors(self, groups):
        doc = {"type": "mirrors", "format": 1, "issued": "2026-11-01T11:00:00Z", "mirrors": groups}
        return formats.check_document("mirrors.json", formats.dump_json(doc))

    def test_tags(self):
        self.tags()
        self.tags(tags={"latest": 1, "testing": 1, "stable": 1})
        for change in ({"tags": {"latest": 3, "testing": 1, "stable": 2}},
                       {"tags": {"latest": 3, "testing": 2}},
                       {"tags": {"latest": 3, "testing": 2, "stable": 1, "develop": 3}},
                       {"withdrawn": [2]},
                       {"withdrawn": [5, 4]},
                       {"withdrawn": [4, 4]},
                       {"serial": 0},
                       {"serial": "1"}):
            with self.assertRaises(Invalid, msg=change):
                self.tags(**change)

    def test_mirrors(self):
        self.mirrors([["https://a.example/u/"], ["https://b.example/", "https://c.example:8443/x/"]])
        for groups in ([],
                       [[]],
                       ["https://a.example/"],
                       [["http://a.example/"]],
                       [["https://a.example"]],
                       [["https://user@a.example/"]],
                       [["https://a.example/?x=1/"]],
                       [["https://a.example/#f/"]],
                       [["https:///"]],
                       [["https://a.example/"], ["https://a.example/"]],
                       [["https://m%d.example/" % i for i in range(9)], ["https://n%d.example/" % i for i in range(8)]]):
            with self.assertRaises(Invalid, msg=groups):
                self.mirrors(groups)


class TestSignatureFiles(unittest.TestCase):
    def test_format(self):
        line = "a" * 64 + " " + "b" * 128 + "\n"
        self.assertEqual(formats.parse_signatures(line.encode()), [("a" * 64, "b" * 128)])
        for bad in ("", line[:-1], line + "\n", line.upper(), line.replace(" ", "  "), line + line,
                    "".join("%064x %s\n" % (i, "b" * 128) for i in range(9)), line.replace("\n", "\r\n")):
            with self.assertRaises(Invalid, msg=repr(bad)):
                formats.parse_signatures(bad.encode())


class TestCommandLine(Base):
    def run_validate(self, *args):
        return subprocess.run([sys.executable, str(HOPPER / "updater/validate.py"), *args],
                              capture_output=True, text=True)

    def test_tree(self):
        result = self.run_validate("tree", self.dir, "--floor", str(self.tree.root / "keys/1.json"),
                                   "--now", "2026-11-01T12:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valid:", result.stderr)

    def test_tree_fails(self):
        (self.tree.root / "tags.json.sig").unlink()
        result = self.run_validate("tree", self.dir, "--floor", str(self.tree.root / "keys/1.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: tags.json has no signature", result.stderr)

    def test_document(self):
        keys = str(self.tree.root / "keys/1.json")
        result = self.run_validate("document", str(self.tree.root / "builds/4312.json"), "--keys", keys,
                                   "--now", "2026-11-01T12:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_validate("document", keys)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_document_unsigned_elsewhere(self):
        outside = Path(self.dir) / "draft.json"
        shutil.copy(self.tree.root / "tags.json", outside)
        result = self.run_validate("document", str(outside), "--name", "tags.json", "--unsigned")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_validate("document", str(outside), "--name", "builds/1.json", "--unsigned")
        self.assertEqual(result.returncode, 1)
        self.assertIn("type is 'tags', expected 'build'", result.stderr)

    def test_document_expired_keys(self):
        self.tree.write("keys/1.json", keys_doc(1, [self.keys["root1"]], [self.keys["online1"]],
                                                expires="2026-01-01T00:00:00Z"), ["root1"])
        result = self.run_validate("document", str(self.tree.root / "tags.json"), "--keys",
                                   str(self.tree.root / "keys/1.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("expired", result.stderr)


def write_sample(directory):
    directory = Path(directory)
    (directory / "keys").mkdir(parents=True)
    keys = make_keys(directory / "keys")
    sample_tree(directory / "tree", keys)
    print("%s: the tree; %s: its throwaway keys; floor: tree/keys/1.json" % (directory / "tree", directory / "keys"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--sample"] and len(sys.argv) == 3:
        write_sample(sys.argv[2])
    else:
        unittest.main()
