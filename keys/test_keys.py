#!/usr/bin/env python3
"""Tests for the key scripts in keys/, run as commands with throwaway keys, the way the
ceremony runs them. Run from the top of the checkout:

    python3 -m unittest keys/test_keys.py
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOPPER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOPPER / "lib"))

import updater_formats as formats

NOW = "2026-11-01T12:00:00Z"
EXPIRES = "2028-11-01T00:00:00Z"


def run(script, *args):
    return subprocess.run([sys.executable, str(HOPPER / script), *map(str, args)], capture_output=True, text=True)


class Keys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_dir = Path(tempfile.mkdtemp())
        for name in ("root1", "root2", "root3", "online1", "online2", "stranger"):
            result = run("keys/make-key.py", cls.key_dir / name)
            assert result.returncode == 0, result.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_dir)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "keys").mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def key(self, name, kind="pub"):
        return self.key_dir / ("%s.%s" % (name, kind))

    def ok(self, script, *args):
        result = run(script, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def fails(self, text, script, *args):
        result = run(script, *args)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(text, result.stderr)

    def write(self, version, roots, online, expires=EXPIRES, previous=None):
        args = ["keys/write-keys.py", self.dir / "keys" / ("%d.json" % version), "--expires", expires]
        for name in roots:
            args += ["--root", self.key(name)]
        for name in online:
            args += ["--online", self.key(name)]
        if previous:
            args += ["--previous", self.dir / "keys" / ("%d.json" % previous)]
        self.ok(*args)
        return self.dir / "keys" / ("%d.json" % version)

    def sign(self, path, *names):
        self.ok("keys/sign.py", path, *(self.key(n, "pem") for n in names))

    def chain(self):
        """Version 1 with root1, root2 and online1; version 2 with online2 instead."""
        one = self.write(1, ["root1", "root2"], ["online1"])
        self.sign(one, "root1")
        self.sign(one, "root2")
        two = self.write(2, ["root1", "root2"], ["online2"], previous=1)
        self.sign(two, "root2")
        return one, two

    def test_make_key(self):
        private, public = self.key("root1", "pem"), self.key("root1")
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o600)
        self.assertEqual(public.read_text(), formats.public_key(private) + "\n")
        self.fails("exists already", "keys/make-key.py", self.key_dir / "root1")

    def test_chain_is_valid(self):
        one, two = self.chain()
        self.ok("keys/verify.py", "keys", one, two, "--roots", self.key("root1"), self.key("root2"), "--now", NOW)
        self.ok("keys/verify.py", "keys", two, "--known", one, "--now", NOW)
        self.assertEqual(len((self.dir / "keys/1.json.sig").read_text().splitlines()), 2)
        # The validator agrees
        self.ok("updater/validate.py", "document", one)
        self.ok("updater/validate.py", "document", two, "--keys", one)

    def test_written_as_the_publisher_writes(self):
        one = self.write(1, ["root1", "root2"], ["online1"])
        self.assertEqual(one.read_bytes(), formats.dump_json(json.loads(one.read_text())))

    def test_modified_document(self):
        one, two = self.chain()
        two.write_text(two.read_text().replace(EXPIRES, "2030-11-01T00:00:00Z"))
        self.fails("keys/2.json: the signature by", "keys/verify.py", "keys", two, "--known", one, "--now", NOW)

    def test_expired(self):
        one = self.write(1, ["root1", "root2"], ["online1"], expires="2026-10-01T00:00:00Z")
        self.sign(one, "root1")
        self.fails("keys/1.json expired at 2026-10-01T00:00:00Z", "keys/verify.py", "keys", one,
                   "--roots", self.key("root1"), self.key("root2"), "--now", NOW)

    def test_expired_intermediate_is_fine(self):
        one = self.write(1, ["root1", "root2"], ["online1"], expires="2026-10-01T00:00:00Z")
        self.sign(one, "root1")
        two = self.write(2, ["root1", "root2"], ["online2"], previous=1)
        self.sign(two, "root1")
        self.ok("keys/verify.py", "keys", one, two, "--roots", self.key("root1"), self.key("root2"), "--now", NOW)

    def test_lower_version(self):
        one, two = self.chain()
        self.fails("version 1 isn't newer than the known version 2", "keys/verify.py", "keys", one,
                   "--known", two, "--now", NOW)
        self.fails("version 2 isn't newer than the known version 2", "keys/verify.py", "keys", two,
                   "--known", two, "--now", NOW)

    def test_skipped_version(self):
        one, two = self.chain()
        self.fails("versions are skipped", "keys/verify.py", "keys", two, "--roots", self.key("root1"),
                   self.key("root2"), "--now", NOW)

    def test_not_the_pinned_roots(self):
        one = self.write(1, ["root1", "root3"], ["online1"])
        self.sign(one, "root1")
        self.fails("aren't the pinned root keys", "keys/verify.py", "keys", one, "--roots", self.key("root1"),
                   self.key("root2"), "--now", NOW)

    def test_signed_by_an_online_key(self):
        one = self.write(1, ["root1", "root2"], ["online1"])
        self.sign(one, "root1")
        two = self.write(2, ["root1", "root2"], ["online2"], previous=1)
        self.sign(two, "online1", "stranger")
        self.fails("no signature by a trusted key", "keys/verify.py", "keys", two, "--known", one, "--now", NOW)

    def test_root_rotation(self):
        one = self.write(1, ["root1", "root2"], ["online1"])
        self.sign(one, "root1")
        two = self.write(2, ["root2", "root3"], ["online1"], previous=1)
        self.sign(two, "root1")
        three = self.write(3, ["root2", "root3"], ["online2"], previous=2)
        self.sign(three, "root3")
        self.ok("keys/verify.py", "keys", two, three, "--known", one, "--now", NOW)
        # root1 is gone after version 2
        (self.dir / "keys/3.json.sig").unlink()
        self.sign(three, "root1")
        self.fails("no signature by a trusted key", "keys/verify.py", "keys", three, "--known", two, "--now", NOW)

    def test_write_checks(self):
        self.write(1, ["root1"], ["online1"])
        self.fails("must be 2", "keys/write-keys.py", self.dir / "keys/3.json", "--expires", EXPIRES,
                   "--root", self.key("root1"), "--online", self.key("online1"), "--previous", self.dir / "keys/1.json")
        self.fails("both a root key and an online key", "keys/write-keys.py", self.dir / "keys/2.json",
                   "--expires", EXPIRES, "--root", self.key("root1"), "--online", self.key("root1"))
        self.fails("exists", "keys/write-keys.py", self.dir / "keys/1.json", "--expires", EXPIRES,
                   "--root", self.key("root1"), "--online", self.key("online1"))
        self.fails("doesn't hold a public key", "keys/write-keys.py", self.dir / "keys/2.json", "--expires", EXPIRES,
                   "--root", self.key("root1", "pem"), "--online", self.key("online1"))
        self.fails("isn't a valid time", "keys/write-keys.py", self.dir / "keys/2.json", "--expires",
                   "2028-02-30T00:00:00Z", "--root", self.key("root1"), "--online", self.key("online1"))

    def test_sign_refuses_a_stale_signature(self):
        one = self.write(1, ["root1", "root2"], ["online1"])
        self.sign(one, "root1")
        self.sign(one, "root1")
        self.assertEqual(len((self.dir / "keys/1.json.sig").read_text().splitlines()), 1)
        one.write_text(one.read_text().replace(EXPIRES, "2030-11-01T00:00:00Z"))
        self.fails("doesn't verify; the document changed", "keys/sign.py", one, self.key("root2", "pem"))

    def test_sign_checks_the_document(self):
        draft = self.dir / "draft.json"
        draft.write_text('{"type": "keys"}\n')
        self.fails("--name gives its path", "keys/sign.py", draft, self.key("root1", "pem"))
        self.assertFalse((self.dir / "draft.json.sig").exists())

    def test_file(self):
        one, two = self.chain()
        tags = self.dir / "tags.json"
        tags.write_bytes(formats.dump_json({"type": "tags", "format": 1, "serial": 1, "issued": NOW,
                                            "tags": {"latest": 3, "testing": 2, "stable": 1}, "withdrawn": []}))
        self.sign(tags, "online2")
        self.ok("keys/verify.py", "file", tags, "--keys", two, "--now", NOW)
        self.fails("no signature by a trusted key", "keys/verify.py", "file", tags, "--keys", one, "--now", NOW)
        self.fails("expired", "keys/verify.py", "file", tags, "--keys", two, "--now", "2029-01-01T00:00:00Z")
        self.ok("keys/verify.py", "file", one, "--key", self.key("root2"))
        self.fails("use `verify.py keys`", "keys/verify.py", "file", two, "--keys", one)


if __name__ == "__main__":
    unittest.main()
