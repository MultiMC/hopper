#!/usr/bin/env python3
"""Tests for the tag operations of the new updater's tree: updater/tag.py, updater/remove.py,
updater/prune.py, and the download archives (lib/updater_downloads.py), on trees made with
updater/deploy.py from the releases and keys of updater/test_deploy.py. Run from the top of
the checkout:

    python3 -m unittest updater/test_tags.py

Like updater/test_deploy.py, they need HOPPER_TEST_DIR (default: $STATE) on btrfs.
"""

import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

HOPPER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOPPER / "lib"))
sys.path.insert(0, str(HOPPER / "updater"))

import test_deploy as fixtures
from test_deploy import make_release


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(fixtures.ON_BTRFS, "%s isn't on btrfs (HOPPER_TEST_DIR)" % fixtures.TEST_DIR)
class TestTags(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestDeploy.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestDeploy.tearDownClass.__func__)
    setUp = fixtures.TestDeploy.setUp
    tearDown = fixtures.TestDeploy.tearDown
    snapshot_state = fixtures.TestDeploy.snapshot_state
    valid = fixtures.TestDeploy.valid
    doc = fixtures.TestDeploy.doc

    def run_script(self, script, *args, **settings):
        config = self.dir / "test.env"
        config.write_text("".join('%s="%s"\n' % item for item in {**self.settings, **settings}.items()))
        return subprocess.run([sys.executable, str(HOPPER / "updater" / script), "--config", str(config), *map(str, args)],
                              capture_output=True, text=True)

    def ok(self, script, *args, **settings):
        result = self.run_script(script, *args, **settings)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.valid()
        return result

    def refused(self, text, script, *args, **settings):
        before = self.snapshot_state()
        result = self.run_script(script, *args, **settings)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(text, result.stderr)
        self.assertEqual(self.snapshot_state(), before, "a refused run changed the tree")

    def deploy(self, *builds):
        for build in builds:
            new_tree = [] if (self.tree / "tags.json").exists() else ["--new-tree"]
            self.ok("deploy.py", *new_tree, make_release(self.dir, build))

    def tags(self):
        return self.doc("tags.json")["tags"]

    def test_both_tags_at_once(self):
        self.deploy(10, 11, 12)
        serial = self.doc("tags.json")["serial"]
        self.ok("tag.py", "testing", 12, "stable", 11)
        self.assertEqual(self.tags(), {"latest": 12, "testing": 12, "stable": 11})
        self.assertGreater(self.doc("tags.json")["serial"], serial, "a move writes a higher serial")
        self.assertTrue((self.downloads() / "archive" / "11").is_dir())

    def test_both_tags_refused(self):
        self.deploy(10, 11, 12)
        self.refused("can't be older than Stable", "tag.py", "stable", 12, "testing", 11)
        self.refused("named twice", "tag.py", "testing", 12, "testing", 12)
        self.refused("no tag latest", "tag.py", "latest", 12)
        self.refused("each tag and its build", "tag.py", "testing")

    def test_continuity(self):
        # Latest went to a build with a stricter requirement (allowed then); Stable following
        # it is a move of its own, checked again
        self.deploy(10)
        stricter = make_release(self.dir, 11, requires={"os": "linux", "arch": "x86_64", "glibc": "2.27",
                                                        "glibcxx": "3.4.21", "qt": "5.6"})
        self.ok("deploy.py", "--allow-discontinuity", stricter)
        self.refused("requires glibc 2.27 in build 11, up from 2.14 in build 10", "tag.py", "stable", 11)
        self.ok("tag.py", "--allow-discontinuity", "stable", 11)
        self.assertEqual(self.tags(), {"latest": 11, "testing": 11, "stable": 11})

    def test_serial_guards_what_was_decided(self):
        self.deploy(10, 11)
        serial = self.doc("tags.json")["serial"]
        self.refused("the tags have changed", "tag.py", "--serial", serial - 1, "testing", 11)
        self.ok("tag.py", "--serial", serial, "testing", 11)

    def test_remove_guarded_and_reported(self):
        self.deploy(10, 11, 12)
        serial = self.doc("tags.json")["serial"]
        self.refused("the tags have changed", "remove.py", "--serial", serial - 1, 12)
        report = json.loads(self.ok("remove.py", "--dry-run", "--json", "--serial", serial, 12).stdout)
        self.assertEqual(self.doc("tags.json")["serial"], serial, "a dry run wrote")
        self.assertEqual(report["build"], 12)
        self.assertEqual(report["moved"], ["latest"])
        self.assertEqual(report["after"]["latest"], 11)
        self.assertFalse(report["archive_removed"])
        self.assertGreater(report["files"], 0)
        self.assertGreater(report["bytes"], 0)

    def test_prune_guarded_and_reported(self):
        self.deploy(10, 11, 12, 13)
        serial = self.doc("tags.json")["serial"]
        self.refused("the tags have changed", "prune.py", "--serial", serial - 1)
        nothing = json.loads(self.ok("prune.py", "--dry-run", "--json").stdout)
        self.assertEqual(nothing["purged"], [])
        report = json.loads(self.ok("prune.py", "--dry-run", "--json", "--serial", serial, UPDATER_MAX_SIZE_MB=0).stdout)
        self.assertEqual(report["purged"], [11, 12])
        self.assertGreater(report["bytes"], 0)
        self.assertTrue((self.files / self.settings["UPDATER_ROOT"] / "builds" / "11.json").exists(), "a dry run purged")
        self.assertEqual(json.loads(self.ok("prune.py", "--dry-run", "--json", "--cap-mb", 0).stdout)["purged"], [11, 12])
        self.refused("can't be negative", "prune.py", "--cap-mb=-1")

    def test_json_report(self):
        self.deploy(10, 11, 12)
        serial = self.doc("tags.json")["serial"]
        report = json.loads(self.ok("tag.py", "--dry-run", "--json", "stable", 11).stdout)
        self.assertEqual(self.doc("tags.json")["serial"], serial, "a dry run wrote")
        self.assertEqual(report["before"]["testing"], 10)
        self.assertEqual(report["after"], {"latest": 12, "testing": 11, "stable": 11})
        self.assertEqual(report["moved"], ["testing", "stable"])
        self.assertTrue(report["dry_run"])

    def downloads(self):
        return self.files / "downloads" / "updater"

    def lin64_binary(self, archive):
        with tarfile.open(archive) as tar:
            return tar.extractfile("MultiMC/bin/MultiMC").read()

    def test_new_tree_writes_every_tag(self):
        self.deploy(10)
        names = sorted(p.name for p in self.downloads().iterdir() if p.is_file())
        self.assertEqual(names, sorted("mmc-%s-%s.tar.gz" % (tag, platform) for tag in ("latest", "testing", "stable")
                                       for platform in ("lin64", "osx64-5.15.2")))
        self.assertTrue((self.downloads() / "archive/10/mmc-10-lin64.tar.gz").is_file())
        self.deploy(11)
        self.assertEqual(self.lin64_binary(self.downloads() / "mmc-latest-lin64.tar.gz"), b"linux 11")
        self.assertEqual(self.lin64_binary(self.downloads() / "mmc-stable-lin64.tar.gz"), b"linux 10")

    def test_archive_is_the_bundle(self):
        self.deploy(10)
        with tarfile.open(self.downloads() / "mmc-latest-osx64-5.15.2.tar.gz") as tar:
            members = {m.name: m for m in tar.getmembers()}
        self.assertEqual(members["MultiMC.app/Contents/MacOS/MultiMC"].mode, 0o755)
        self.assertEqual(members["MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/5/QtCore"].mode, 0o644)
        self.assertEqual(members["MultiMC.app/Contents/Frameworks/QtCore.framework/QtCore"].linkname, "Versions/Current/QtCore")
        self.assertTrue(members["MultiMC.app/Contents"].isdir())
        self.assertEqual({m.mtime for m in members.values()}, {1793566512})
        # The same build gives the same bytes
        first = checksum(self.downloads() / "mmc-latest-lin64.tar.gz")
        self.ok("tag.py", "stable", 10)
        self.assertEqual(checksum(self.downloads() / "mmc-stable-lin64.tar.gz"), first)
        with gzip.open(self.downloads() / "mmc-latest-lin64.tar.gz") as gz:
            gz.read(1)  # the header is read with the first data
            self.assertEqual(gz.mtime, 1793566512)

    def test_testing_moves_forward_only(self):
        self.deploy(10, 11, 12)
        self.ok("tag.py", "testing", 11)
        self.assertEqual(self.tags(), {"latest": 12, "testing": 11, "stable": 10})
        self.assertEqual(self.lin64_binary(self.downloads() / "mmc-testing-lin64.tar.gz"), b"linux 11")
        serial = self.doc("tags.json")["serial"]
        self.ok("tag.py", "testing", 11)
        self.assertEqual(self.doc("tags.json")["serial"], serial, "naming the same build again moved the tags")
        self.refused("tags only move forward", "tag.py", "testing", 10)
        self.ok("tag.py", "testing", 12)
        self.refused("tags only move forward", "tag.py", "testing", 11)
        self.refused("isn't in the tree", "tag.py", "testing", 13)

    def test_stable_pulls_testing_along(self):
        self.deploy(10, 11, 12)
        self.ok("tag.py", "testing", 11)
        self.ok("tag.py", "stable", 12)
        self.assertEqual(self.tags(), {"latest": 12, "testing": 12, "stable": 12})
        self.assertTrue((self.downloads() / "archive/12/mmc-12-lin64.tar.gz").is_file())
        self.assertEqual(self.lin64_binary(self.downloads() / "mmc-testing-lin64.tar.gz"), b"linux 12")
        self.refused("tags only move forward", "tag.py", "stable", 11)

    def test_stable_keeps_newer_testing(self):
        self.deploy(10, 11, 12)
        self.ok("tag.py", "testing", 12)
        self.ok("tag.py", "stable", 11)
        self.assertEqual(self.tags(), {"latest": 12, "testing": 12, "stable": 11})

    def test_old_stable_gets_its_archive(self):
        self.deploy(10, 11)
        # As on files2, whose first Stable build was set before there were archives
        shutil.rmtree(self.downloads() / "archive/10")
        self.ok("tag.py", "stable", 11)
        self.assertTrue((self.downloads() / "archive/10/mmc-10-lin64.tar.gz").is_file())

    def test_dry_run(self):
        self.deploy(10, 11)
        before = self.snapshot_state()
        self.ok("tag.py", "--dry-run", "stable", 11)
        self.ok("remove.py", "--dry-run", 11)
        self.ok("prune.py", "--dry-run", UPDATER_MAX_SIZE_MB=0)
        self.assertEqual(self.snapshot_state(), before)

    def test_remove(self):
        self.deploy(10, 11, 12)
        self.ok("tag.py", "testing", 12)
        self.refused("is Stable and can't be removed", "remove.py", 10)
        only_12 = [name for name in fixtures.formats.content_files(self.doc("builds/12.json"))]
        self.ok("remove.py", 12)
        self.assertEqual(self.tags(), {"latest": 11, "testing": 11, "stable": 10})
        self.assertEqual(self.doc("tags.json")["withdrawn"], [12])
        self.assertFalse((self.tree / "builds/12.json").exists())
        self.assertFalse((self.tree / "builds/12.json.sig").exists())
        shared = fixtures.formats.content_files(self.doc("builds/11.json"))
        for sha256 in only_12:
            self.assertEqual((self.tree / fixtures.formats.content_path(sha256)).exists(), sha256 in shared)
        self.assertEqual(self.lin64_binary(self.downloads() / "mmc-latest-lin64.tar.gz"), b"linux 11")
        self.assertEqual(self.lin64_binary(self.downloads() / "mmc-testing-lin64.tar.gz"), b"linux 11")
        self.refused("withdrawn already", "remove.py", 12)
        self.refused("withdrawn", "tag.py", "testing", 12)
        self.refused("was withdrawn", "deploy.py", make_release(self.dir, 12))
        self.deploy(13)
        self.assertEqual(self.tags()["latest"], 13)

    def test_remove_old_stable_takes_its_archive(self):
        self.deploy(10, 11, 12)
        self.ok("tag.py", "stable", 11)
        self.ok("tag.py", "stable", 12)
        self.ok("remove.py", 11)
        self.assertFalse((self.downloads() / "archive/11").exists())
        self.assertTrue((self.downloads() / "archive/10").exists())

    def test_prune(self):
        self.deploy(10, 11, 12, 13, 14, 15)
        self.ok("tag.py", "stable", 11)
        self.ok("tag.py", "testing", 13)
        self.ok("prune.py")
        self.assertEqual(sorted(int(p.stem) for p in (self.tree / "builds").glob("*.json")), [10, 11, 12, 13, 14, 15],
                         "the default cap purged builds")
        self.ok("prune.py", UPDATER_MAX_SIZE_MB=0)
        # 10 and 11 have been Stable, 13 is Testing, 15 Latest
        self.assertEqual(sorted(int(p.stem) for p in (self.tree / "builds").glob("*.json")), [10, 11, 13, 15])
        self.assertEqual(self.doc("tags.json")["withdrawn"], [])
        self.assertEqual(self.tags(), {"latest": 15, "testing": 13, "stable": 11})



class TestZip(unittest.TestCase):
    """The Windows download archive (the trees above have no Windows variant)."""

    def test_zip(self):
        import tempfile
        import zipfile
        import updater_downloads as downloads
        with tempfile.TemporaryDirectory() as tmp:
            exe, dll = Path(tmp) / "exe", Path(tmp) / "dll"
            exe.write_bytes(b"MZ exe")
            dll.write_bytes(b"MZ dll")
            entries = [("MultiMC", "dir", None), ("MultiMC/MultiMC.exe", "file", (exe, True)),
                       ("MultiMC/platforms", "dir", None), ("MultiMC/platforms/qwindows.dll", "file", (dll, False))]
            downloads.write_zip(Path(tmp) / "a.zip", entries, 1793566512)
            downloads.write_zip(Path(tmp) / "b.zip", entries, 1793566512)
            self.assertEqual(checksum(Path(tmp) / "a.zip"), checksum(Path(tmp) / "b.zip"))
            with zipfile.ZipFile(Path(tmp) / "a.zip") as z:
                self.assertEqual(z.namelist(), ["MultiMC/", "MultiMC/MultiMC.exe", "MultiMC/platforms/",
                                                "MultiMC/platforms/qwindows.dll"])
                self.assertEqual(z.read("MultiMC/platforms/qwindows.dll"), b"MZ dll")
                self.assertEqual(z.getinfo("MultiMC/MultiMC.exe").external_attr >> 16, 0o100755)
                self.assertEqual(z.getinfo("MultiMC/MultiMC.exe").date_time, (2026, 11, 1, 20, 55, 12))
            with self.assertRaises(SystemExit):
                downloads.write_zip(Path(tmp) / "c.zip", entries + [("MultiMC/link", "link", "x")], 1793566512)


if __name__ == "__main__":
    unittest.main()
