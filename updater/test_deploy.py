#!/usr/bin/env python3
"""Tests for updater/deploy.py (the new updater's publisher) and lib/updater_tree.py, on
releases and files trees made here, with throwaway keys (openssl genpkey). Run from the top
of the checkout:

    python3 -m unittest updater/test_deploy.py

The files trees are btrfs subvolumes, as the scripts require; they are made in
HOPPER_TEST_DIR (default: $STATE), and the tests that need one are skipped if that isn't
on btrfs.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

HOPPER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOPPER / "lib"))

import common
import updater_formats as formats
import updater_tree

TEST_DIR = Path(os.environ.get("HOPPER_TEST_DIR") or common.STATE)
ON_BTRFS = TEST_DIR.is_dir() and subprocess.run(["stat", "-f", "-c", "%T", str(TEST_DIR)], capture_output=True,
                                                 text=True).stdout.strip() == "btrfs"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# FILES_MIRRORS: the files site, then its copy; UPDATER_BASE follows from the first
MIRRORS = "https://files.example.org/ https://bucket.example.org/"


def make_key(directory, name):
    path = Path(directory) / ("%s.pem" % name)
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(path)], check=True, capture_output=True)
    return path


def write_keys(directory, version, roots, online, signers):
    """A keys document and its .sig in directory, as UPDATER_KEYS holds them."""
    doc = {"type": "keys", "format": 1, "version": version, "expires": "2099-01-01T00:00:00Z",
           "root_keys": [formats.public_key(k) for k in roots], "online_keys": [formats.public_key(k) for k in online]}
    data = formats.dump_json(doc)
    path = Path(directory) / ("%d.json" % version)
    path.write_bytes(data)
    Path(str(path) + ".sig").write_text("".join(formats.sign(data, k) for k in signers))


def tar_add(tar, name, data=None, mode=0o644, link=None, directory=False):
    info = tarfile.TarInfo(name)
    if directory:
        info.type, info.mode = tarfile.DIRTYPE, 0o755
    elif link is not None:
        info.type, info.linkname = tarfile.SYMTYPE, link
    else:
        info.size, info.mode = len(data), mode
    tar.addfile(info, io.BytesIO(data) if data is not None else None)


LINUX = [("MultiMC/MultiMC", b"#!/bin/sh\n", 0o755), ("MultiMC/bin/MultiMC", b"linux %d", 0o755),
         ("MultiMC/bin/jars/NewLaunch.jar", b"jar", 0o644)]
MACOS = [("MultiMC.app/Contents/MacOS/MultiMC", b"mac %d", 0o755),
         ("MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/5/QtCore", b"qt", 0o644)]
MACOS_LINKS = [("MultiMC.app/Contents/Frameworks/QtCore.framework/Versions/Current", "5"),
               ("MultiMC.app/Contents/Frameworks/QtCore.framework/QtCore", "Versions/Current/QtCore")]


def make_release(directory, build, linux=None, requires=None, updater_base="https://files.example.org/update/",
                 updater=None):
    """A release as launcher/release-build.py writes it, for lin64 and osx64-5.15.2."""
    release = Path(tempfile.mkdtemp(dir=directory)) / str(build)
    (release / "deploy").mkdir(parents=True)
    with tarfile.open(release / "deploy/lin64.tar.gz", "w:gz") as tar:
        for d in ("MultiMC", "MultiMC/bin", "MultiMC/bin/jars"):
            tar_add(tar, d, directory=True)
        for name, data, mode in linux or LINUX:
            tar_add(tar, name, data.replace(b"%d", b"%d" % build), mode)
    with tarfile.open(release / "deploy/osx64-5.15.2.tar.gz", "w:gz") as tar:
        for name, data, mode in MACOS:
            tar_add(tar, name, data.replace(b"%d", b"%d" % build), mode)
        for name, target in MACOS_LINKS:
            tar_add(tar, name, link=target)
    manifest = {
        "branch": "develop", "build_number": build, "built_at": "2026-11-01T20:55:12Z", "commit": COMMIT,
        "mode": "clean", "releasable": True, "updater_base": updater_base, "version_name": "0.7.%d" % build,
        "platforms": {
            "lin64": {"deploy_archive": "deploy/lin64.tar.gz",
                      "requires": requires or {"os": "linux", "arch": "x86_64", "glibc": "2.14", "glibcxx": "3.4.21", "qt": "5.6"}},
            "osx64-5.15.2": {"deploy_archive": "deploy/osx64-5.15.2.tar.gz",
                             "requires": {"os": "macos", "arch": "x86_64", "os_version": "10.13"}},
        },
    }
    if updater:
        manifest["updater"] = updater
    (release / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return release


@unittest.skipUnless(ON_BTRFS, "%s isn't on btrfs (HOPPER_TEST_DIR)" % TEST_DIR)
class TestDeploy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_dir = Path(tempfile.mkdtemp())
        cls.keys = {name: make_key(cls.key_dir, name) for name in ("root1", "root2", "online1", "online2")}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_dir)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=TEST_DIR, prefix="test-updater-"))
        self.files = self.dir / "files"
        subprocess.run(["btrfs", "subvolume", "create", str(self.files)], check=True, capture_output=True)
        self.keys_dir = self.dir / "keys"
        self.keys_dir.mkdir()
        write_keys(self.keys_dir, 1, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]], [self.keys["root1"]])
        self.settings = {"FILES_ROOT": str(self.files), "FILES_MIRRORS": MIRRORS,
                         "UPDATER_ROOT": "updater", "UPDATER_KEYS": str(self.keys_dir),
                         "UPDATER_ONLINE_KEY": str(self.keys["online1"])}
        self.tree = self.files / "updater"

    def tearDown(self):
        for snapshot in (self.dir / "files.snapshots").glob("*"):
            if snapshot.is_dir():
                subprocess.run(["btrfs", "property", "set", str(snapshot), "ro", "false"], check=True)
        shutil.rmtree(self.dir)

    def deploy(self, release, *options, new_tree=None, **settings):
        """Run updater/deploy.py. A tree without a tags document is started (--new-tree),
        unless new_tree says otherwise."""
        config = self.dir / "test.env"
        config.write_text("".join('%s="%s"\n' % item for item in {**self.settings, **settings}.items()))
        if new_tree is None:
            new_tree = not (self.tree / "tags.json").exists()
        return subprocess.run([sys.executable, str(HOPPER / "updater/deploy.py"), "--config", str(config),
                               *(["--new-tree"] if new_tree else []), *options, str(release)], capture_output=True, text=True)

    def assertDeploys(self, release, *options, **settings):
        result = self.deploy(release, *options, **settings)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def assertRefused(self, release, text, new_tree=None, **settings):
        before = self.snapshot_state()
        result = self.deploy(release, new_tree=new_tree, **settings)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(text, result.stderr)
        self.assertEqual(self.snapshot_state(), before, "a refused deploy changed the tree")
        return result

    def snapshot_state(self):
        return {str(p.relative_to(self.files)): p.read_bytes() for p in self.files.rglob("*") if p.is_file()}

    def valid(self):
        summary, warnings = formats.check_tree(self.tree, (self.keys_dir / "1.json").read_bytes())
        return summary

    def doc(self, name):
        return json.loads((self.tree / name).read_text())

    def test_new_tree(self):
        started = int(time.time())
        result = self.assertDeploys(make_release(self.dir, 10))
        self.assertIn("a new tree", result.stderr)
        self.assertIn("1 builds, 5 content files", self.valid())
        tags = self.doc("tags.json")
        self.assertEqual((tags["tags"], tags["withdrawn"]), ({"latest": 10, "testing": 10, "stable": 10}, []))
        self.assertGreaterEqual(tags["serial"], started, "a new tree's serial starts at the time, not at 1")
        build = self.doc("builds/10.json")
        self.assertEqual(build["version"], "0.7.10")
        self.assertEqual(sorted(build["variants"]), ["linux-x86_64-qt5", "macos-x86_64-qt5.15"])
        linux = build["variants"]["linux-x86_64-qt5"]
        self.assertEqual([(f["path"], f["executable"]) for f in linux["files"]],
                         [("MultiMC", True), ("bin/MultiMC", True), ("bin/jars/NewLaunch.jar", False)])
        self.assertEqual(build["variants"]["macos-x86_64-qt5.15"]["links"],
                         [{"path": "Contents/Frameworks/QtCore.framework/QtCore", "target": "Versions/Current/QtCore"},
                          {"path": "Contents/Frameworks/QtCore.framework/Versions/Current", "target": "5"}])
        self.assertEqual((self.tree / "keys/1.json").read_bytes(), (self.keys_dir / "1.json").read_bytes())
        self.assertEqual([p.name for p in (self.dir / "files.snapshots").glob("*") if p.is_dir()][0][16:],
                         "before-updater-10")
        self.assertFalse((self.tree / ".incoming").exists())

    def test_release_without_build_in_version(self):
        # built before 2026-11-10: its version name is "0.7.0", which isn't this build's version
        release = make_release(self.dir, 10)
        manifest = json.loads((release / "manifest.json").read_text())
        manifest["version_name"] = "0.7.0"
        (release / "manifest.json").write_text(json.dumps(manifest))
        self.assertDeploys(release)
        self.assertNotIn("version", self.doc("builds/10.json"))

    def test_next_build(self):
        self.assertDeploys(make_release(self.dir, 10))
        serial = self.doc("tags.json")["serial"]
        mirrors = (self.tree / "mirrors.json").read_bytes()
        result = self.assertDeploys(make_release(self.dir, 11))
        self.assertIn("5 content files, 2 of them new", result.stderr)
        self.assertIn("2 builds, 7 content files", self.valid())
        tags = self.doc("tags.json")
        self.assertEqual(tags["tags"], {"latest": 11, "testing": 10, "stable": 10})
        self.assertGreater(tags["serial"], serial)
        self.assertEqual((self.tree / "mirrors.json").read_bytes(), mirrors)

    def test_new_tree_only_when_asked(self):
        self.assertRefused(make_release(self.dir, 10), "has no tags document", new_tree=False)
        self.assertFalse(self.tree.exists())
        self.assertDeploys(make_release(self.dir, 10))
        self.assertRefused(make_release(self.dir, 11), "has a tags document already", new_tree=True)

    def test_lost_tags_document_is_no_new_tree(self):
        # the newest build must not become Stable, with a serial that starts over
        self.assertDeploys(make_release(self.dir, 10))
        self.assertDeploys(make_release(self.dir, 11))
        (self.tree / "tags.json").unlink()
        (self.tree / "tags.json.sig").unlink()
        self.assertRefused(make_release(self.dir, 12), "has no tags document", new_tree=False)
        self.assertRefused(make_release(self.dir, 12), "isn't a new tree: it has builds (10 11)", new_tree=True)

    def test_serial_passes_the_time_after_a_restore(self):
        # a tree put back to an old tags document (a restored snapshot) still gets a serial
        # above what was published since
        self.assertDeploys(make_release(self.dir, 10))
        tags = self.doc("tags.json")
        published = tags["serial"]
        tags["serial"] = 3
        tree = updater_tree.Tree(common.Settings({**self.settings, "UPDATER_ONLINE_KEY": str(self.keys["online1"])}, {}))
        for name, data in tree.sign("tags.json", tags).items():
            (self.tree / name).write_bytes(data)
        self.assertDeploys(make_release(self.dir, 11))
        self.assertGreaterEqual(self.doc("tags.json")["serial"], published)

    def test_continuity(self):
        """A build that would leave Latest's installations without an update is refused,
        unless that's meant (--allow-discontinuity)."""
        self.assertDeploys(make_release(self.dir, 10))
        # a platform left out: its installations would find nothing to update to
        release = make_release(self.dir, 11)
        manifest = json.loads((release / "manifest.json").read_text())
        del manifest["platforms"]["osx64-5.15.2"]
        (release / "manifest.json").write_text(json.dumps(manifest))
        self.assertRefused(release, "variant macos-x86_64-qt5.15 of build 10 isn't in build 11")
        # a stricter requirement than Latest's
        stricter = make_release(self.dir, 12, requires={"os": "linux", "arch": "x86_64", "glibc": "2.27",
                                                        "glibcxx": "3.4.21", "qt": "5.6"})
        self.assertRefused(stricter, "requires glibc 2.27 in build 12, up from 2.14")
        result = self.assertDeploys(stricter, "--allow-discontinuity")
        self.assertIn("allowed (--allow-discontinuity)", result.stderr)

    def test_not_newer(self):
        self.assertDeploys(make_release(self.dir, 10))
        self.assertRefused(make_release(self.dir, 10), "the tree's Latest is build 10; 10 is not newer")
        self.assertRefused(make_release(self.dir, 9), "not newer")

    def test_dry_run(self):
        result = self.assertDeploys(make_release(self.dir, 10), "--dry-run")
        self.assertIn("would be valid", result.stderr)
        self.assertIn("tags.json", result.stdout)
        self.assertFalse(self.tree.exists())
        self.assertFalse((self.dir / "files.snapshots").exists())

    def test_invalid_writes_nothing(self):
        # The variant ID says x86_64; the validator refuses the manifest before anything is written
        release = make_release(self.dir, 10, requires={"os": "linux", "arch": "arm64", "glibc": "2.14",
                                                       "glibcxx": "3.4.21", "qt": "5.6"})
        self.assertRefused(release, "builds/10.json: variants.linux-x86_64-qt5: the ID doesn't match requires")
        self.assertFalse(self.tree.exists())

    def test_bundle_refused(self):
        for linux, text in (
                (LINUX + [("MultiMC/bin/plugin.so", b"x", 0o664)], "has mode 664"),
                (LINUX + [("MultiMC/bin/setuid", b"x", 0o4755)], "has mode 4755"),
                (LINUX + [("Other/file", b"x", 0o644)], "is outside the bundle MultiMC/")):
            with self.subTest(text=text):
                self.assertRefused(make_release(self.dir, 10, linux=linux), text)
        release = make_release(self.dir, 10)
        with tarfile.open(release / "deploy/lin64.tar.gz", "w:gz") as tar:
            for name, data, mode in LINUX:
                tar_add(tar, name, data, mode)
            tar_add(tar, "MultiMC/empty", directory=True)
        self.assertRefused(release, "empty directories can't be expressed in the format: empty")

    def test_wrong_setup(self):
        self.assertRefused(make_release(self.dir, 10, updater_base="https://elsewhere.example.org/update/"),
                           "wrong --config?")

    def test_built_for_another_updater(self):
        # This setup builds updater 1 (the default); a release pinned to keys goes elsewhere
        release = make_release(self.dir, 10, updater={"version": 2, "root_keys": [formats.public_key(self.keys["root1"])]})
        self.assertRefused(release, "was built with updater 2 pinned to")

    def test_online_key_not_in_keys(self):
        self.assertRefused(make_release(self.dir, 10), "isn't an online key of keys/1.json",
                           UPDATER_ONLINE_KEY=str(self.keys["online2"]))

    def test_not_configured(self):
        result = self.assertDeploys(make_release(self.dir, 10), "--if-configured", UPDATER_ROOT="")
        self.assertIn("no new updater tree", result.stderr)
        self.assertRefused(make_release(self.dir, 10), "UPDATER_ROOT is empty", UPDATER_ROOT="")
        self.assertRefused(make_release(self.dir, 10), "must be one new directory", UPDATER_ROOT="update")

    def test_mirrors_change(self):
        self.assertDeploys(make_release(self.dir, 10))
        self.assertEqual(self.doc("mirrors.json")["mirrors"], [["https://files.example.org/"], ["https://bucket.example.org/"]])
        self.assertDeploys(make_release(self.dir, 11), FILES_MIRRORS="https://files.example.org/")
        self.assertEqual(self.doc("mirrors.json")["mirrors"], [["https://files.example.org/"]])
        self.valid()

    def test_keys_rotation(self):
        self.assertDeploys(make_release(self.dir, 10))
        write_keys(self.keys_dir, 2, [self.keys["root1"], self.keys["root2"]], [self.keys["online2"]], [self.keys["root2"]])
        self.assertRefused(make_release(self.dir, 11), "isn't an online key of keys/2.json")
        # The documents already there are signed by the old online key only; re-signing them is
        # part of a rotation, not of a publish
        self.assertRefused(make_release(self.dir, 11), "mirrors.json: no signature by a trusted key",
                           UPDATER_ONLINE_KEY=str(self.keys["online2"]))

    def test_keys_differ(self):
        self.assertDeploys(make_release(self.dir, 10))
        write_keys(self.keys_dir, 1, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]],
                   [self.keys["root1"], self.keys["root2"]])
        self.assertRefused(make_release(self.dir, 11), "keys/1.json in the tree isn't the one in UPDATER_KEYS")

    def test_interrupted_write(self):
        self.assertDeploys(make_release(self.dir, 10))
        (self.tree / ".incoming").mkdir()
        (self.tree / ".incoming/files_ab_cd").write_bytes(b"half")
        result = self.assertDeploys(make_release(self.dir, 11))
        self.assertIn("left behind by an interrupted write", result.stderr)
        self.valid()

    def test_corrupt_content_in_tree(self):
        self.assertDeploys(make_release(self.dir, 10))
        sha256 = self.doc("builds/10.json")["variants"]["linux-x86_64-qt5"]["files"][2]["sha256"]
        (self.tree / formats.content_path(sha256)).write_bytes(b"JAR")
        self.assertRefused(make_release(self.dir, 11), "has SHA-256")


class TestPublishingOrder(unittest.TestCase):
    def test_updater_tree_in_write_order(self):
        sys.path.insert(0, str(HOPPER / "files"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("publish", HOPPER / "files/publish.py")
        publish = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publish)
        changes = {path: {"old": None, "new": "x"} for path in (
            "updater/tags.json", "updater/tags.json.sig", "updater/mirrors.json", "updater/keys/1.json",
            "updater/builds/5.json", "updater/files/ab/abcd", "files/0123-MultiMC", "update/lin64/develop/5.json",
            "update/lin64/develop/index.json", "downloads/mmc-develop-lin64.tar.gz", "mirrors/x")}
        order = [path for _, paths in publish.in_stages(changes, publish.publishing_order({"UPDATER_ROOT": "updater"}))
                 for path in paths]
        position = {path: order.index(path) for path in order}
        self.assertLess(position["updater/files/ab/abcd"], position["updater/builds/5.json"])
        self.assertLess(position["updater/builds/5.json"], position["updater/keys/1.json"])
        self.assertLess(position["updater/mirrors.json"], position["updater/tags.json"])
        self.assertEqual(order[-4:-2], ["updater/tags.json", "updater/tags.json.sig"])
        without = publish.in_stages(changes, publish.publishing_order({"UPDATER_ROOT": ""}))
        self.assertIn("updater/tags.json", without[-1][1])



class TestFrozenLayout(unittest.TestCase):
    """The cutover's rules for the current layout: common.frozen_layout_refusal, and
    files/deploy.py as far as it gets before repoman."""

    def refusal(self, frozen=100, build=105, last=None, bridge=True, updater=2, stable=105, published_stable=105):
        return common.frozen_layout_refusal(frozen, build, last or {"lin64": 100, "win32": 100}, bridge, updater,
                                            stable, published_stable)

    def test_not_frozen(self):
        self.assertIsNone(self.refusal(frozen=None, bridge=False))
        self.assertIn("isn't frozen", self.refusal(frozen=None))

    def test_frozen(self):
        self.assertIn("frozen at build 100", self.refusal(bridge=False))
        self.assertIn("frozen at build 100", self.refusal(bridge=False, build=100))

    def test_bridge(self):
        self.assertIsNone(self.refusal())
        self.assertIn("one bridge, and nothing after it", self.refusal(build=106, stable=106, published_stable=106,
                                                                         last={"lin64": 105, "win32": 105}))
        self.assertIn("one bridge", self.refusal(last={"lin64": 100, "win32": 105}), "half a bridge")
        self.assertIn("isn't at the build it is frozen at (win32: 99", self.refusal(last={"lin64": 100, "win32": 99}))
        self.assertIn("must be built with updater 2", self.refusal(updater=1))
        self.assertIn("Stable is build 104", self.refusal(stable=104))
        self.assertIn("no tree", self.refusal(stable=None))
        self.assertIn("isn't published yet", self.refusal(published_stable=104))
        self.assertIn("isn't published yet", self.refusal(published_stable=None))

    def deploy(self, directory, *options, frozen="", branch="develop"):
        """files/deploy.py on a tree whose develop channels are at build 100; (exit status, stderr)."""
        root = Path(directory) / "files"
        for platform in ("lin64", "osx64-5.15.2"):
            channel = root / "update" / platform / "develop"
            channel.mkdir(parents=True, exist_ok=True)
            (channel / "index.json").write_text(json.dumps({"Versions": [{"Id": 100, "Name": "0.7.100"}]}))
        (root / "update" / "config.json").write_text(json.dumps({"base_url": "https://files.example.org/update/"}))
        config = Path(directory) / "test.env"
        config.write_text('FILES_ROOT="%s"\nFILES_MIRRORS="%s"\nCURRENT_LAYOUT_FROZEN_AT="%s"\n' % (root, MIRRORS, frozen))
        release = make_release(directory, 105)
        manifest = json.loads((release / "manifest.json").read_text())
        manifest["branch"] = branch
        (release / "manifest.json").write_text(json.dumps(manifest))
        result = subprocess.run([sys.executable, str(HOPPER / "files/deploy.py"), "--config", str(config), *options,
                                 str(release)], capture_output=True, text=True)
        return result.returncode, result.stderr

    def test_deploy_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.deploy(directory, frozen="100")[0], 1)
            self.assertIn("frozen at build 100", self.deploy(directory, frozen="100")[1])
            status, output = self.deploy(directory, "--skip-frozen", frozen="100")
            self.assertEqual((status, "isn't deployed to it" in output), (0, True), output)
            self.assertIn("isn't frozen", self.deploy(directory, "--bridge")[1])
            self.assertIn("must be a build number", self.deploy(directory, frozen="soon")[1])
            # built with updater 1, as this setup is: no bridge
            self.assertIn("must be built with updater 2", self.deploy(directory, "--bridge", frozen="100")[1])
            self.assertIn("must be a build of develop", self.deploy(directory, "--bridge", frozen="100", branch="updater")[1])
            self.assertIn("don't go together", self.deploy(directory, "--bridge", "--skip-frozen", frozen="100")[1])


class TestSerial(unittest.TestCase):
    """The tags document's serial: what the next one is, and what may be published."""

    def test_next_serial(self):
        now = "2026-11-11T12:00:00Z"
        seconds = 1794398400
        self.assertEqual(updater_tree.next_serial(None, now), seconds, "a new tree starts at the time")
        self.assertEqual(updater_tree.next_serial({"serial": 17}, now), seconds, "a low serial jumps to the time")
        self.assertEqual(updater_tree.next_serial({"serial": seconds}, now), seconds + 1, "two in one second")
        self.assertEqual(updater_tree.next_serial({"serial": seconds + 500}, now), seconds + 501, "a clock that went back")

    def tags(self, directory, serial, latest=10):
        path = Path(directory) / "updater" / "tags.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(formats.dump_json({"type": "tags", "format": 1, "serial": serial, "issued": "2026-11-11T12:00:00Z",
                                            "tags": {"latest": latest, "testing": 10, "stable": 10}, "withdrawn": []}))

    def check(self, published, root):
        """None if the tree may be published, or what refused it."""
        errors = io.StringIO()
        stderr, sys.stderr = sys.stderr, errors
        try:
            updater_tree.check_published_serial(published, root, "updater")
        except SystemExit:
            return errors.getvalue()
        finally:
            sys.stderr = stderr
        return None

    def test_published_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            published, root = Path(directory) / "published", Path(directory) / "root"
            published.mkdir()
            root.mkdir()
            self.assertIsNone(self.check(published, root), "no tree on either side")
            self.tags(root, 20)
            self.assertIsNone(self.check(published, root), "a new tree")
            self.tags(published, 20)
            self.assertIsNone(self.check(published, root), "the same document")
            self.tags(root, 21, latest=11)
            self.assertIsNone(self.check(published, root), "a higher serial")
            self.tags(root, 20, latest=11)
            self.assertIn("serial 20 is published", self.check(published, root), "another document, the same serial")
            self.tags(root, 3)
            self.assertIn("has serial 3, but serial 20 is published", self.check(published, root), "a restored snapshot")
            (root / "updater" / "tags.json").unlink()
            self.assertIn("the tree no longer has it", self.check(published, root))


class TestUpdaterSettings(unittest.TestCase):
    """common.updater_floor: what a setup's Launchers compile in, and the setups it refuses."""

    @classmethod
    def setUpClass(cls):
        cls.key_dir = Path(tempfile.mkdtemp())
        cls.keys = {name: make_key(cls.key_dir, name) for name in ("root1", "root2", "root3", "online1")}
        cls.roots = [formats.public_key(cls.keys[k]) for k in ("root1", "root2")]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.key_dir)

    def setUp(self):
        self.keys_dir = Path(tempfile.mkdtemp())
        write_keys(self.keys_dir, 1, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]], [self.keys["root1"]])

    def tearDown(self):
        shutil.rmtree(self.keys_dir)

    def floor(self, **settings):
        return common.updater_floor({"UPDATER_VERSION": "2", "UPDATER_ROOT": "updater", "UPDATER_KEYS": str(self.keys_dir),
                                     "UPDATER_ROOT_KEYS": " ".join(self.roots), **settings})

    def assertRefused(self, **settings):
        with self.assertRaises(SystemExit):
            self.floor(**settings)

    def test_updater_1(self):
        self.assertIsNone(self.floor(UPDATER_VERSION="1", UPDATER_ROOT_KEYS=""))
        self.assertRefused(UPDATER_VERSION="1")
        self.assertRefused(UPDATER_VERSION="3")

    def test_floor_is_newest_keys_document(self):
        self.assertEqual(self.floor(), (self.keys_dir / "1.json").read_bytes())
        # A rotation to new root keys: the floor and the pinned keys move to the new ones
        new_roots = [self.keys["root2"], self.keys["root3"]]
        write_keys(self.keys_dir, 2, new_roots, [self.keys["online1"]], [self.keys["root1"]])
        self.assertRefused()
        pinned = " ".join(formats.public_key(k) for k in new_roots)
        self.assertEqual(self.floor(UPDATER_ROOT_KEYS=pinned), (self.keys_dir / "2.json").read_bytes())

    def test_half_set_up(self):
        self.assertRefused(UPDATER_ROOT_KEYS="")
        self.assertRefused(UPDATER_ROOT="")
        self.assertRefused(UPDATER_ROOT_KEYS=self.roots[0])
        self.assertRefused(UPDATER_ROOT_KEYS=" ".join(self.roots + [self.roots[0]]))

    def test_broken_chain(self):
        # Version 2 signed by a key that isn't one of version 1's root keys
        write_keys(self.keys_dir, 2, [self.keys["root1"], self.keys["root2"]], [self.keys["online1"]], [self.keys["root3"]])
        self.assertRefused()


if __name__ == "__main__":
    unittest.main()
