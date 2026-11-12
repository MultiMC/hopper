#!/usr/bin/env python3
"""Test that the tree's Latest can go back to Stable and up again, with its own updater,
before the tree is published (the Design repo's Updater.md, "Testing the way back to
Stable").

    updater/update-test.py [--config FILE] [--if-configured] [--restore-on-failure] [--platform NAME] [--keep]

Latest is installed from the local tree (UPDATER_ROOT in FILES_ROOT) as its manifest lists it,
and run headless in the platform's toolchain image against that tree, served over HTTPS
under the files site's real host names (the setup's FILES_MIRRORS and the tree's mirror
list, mapped to the container, with a certificate from a CA made for this run and trusted
alone). Nothing is signed or written into the tree. Four steps, each a Launcher run with
--update <tier> (the headless mode, a tier switch as in the settings):

    Latest --update stable    0, and the files are Stable's: Latest's updater works
    Stable --update stable    2, nothing to do
    Stable --update latest    0, and the files are Latest's: the way up works too
    Latest --update latest    2, nothing to do

The first is what matters most: users who pick Latest must always be able to go back, and
a build whose updater doesn't work can't be fixed by a later one. Each step's installation
is checked against the build's manifest: every file with its content and executable bit,
every link, and none of the other build's files left over.

Nothing to test (exit 0, with a log line) when Latest is Stable (a new tree). Stable must
know --update (builds from 2026-11-12 on): otherwise the test fails, since the way up can't
be tested. Linux platforms only (lin64, lin32), in their images; others later.

--if-configured
            for setups without a new updater tree (UPDATER_ROOT empty): say so and exit 0
--restore-on-failure
            when the test fails, restore the snapshot updater/deploy.py took before it wrote
            Latest (the newest before-updater-<Latest>), so the failed build is never
            published; the release job runs it so, right after updater/deploy.py
--platform  the platform to test (default lin64)
--keep      keep the work directory (installation, logs) also when the test passes; it is
            always kept when it fails, and its path is printed
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats
import updater_tree

log = common.logger("update-test")

HERE = Path(__file__).resolve().parent
# Exit statuses of the Launcher's --update (launcher/updater/v2/HeadlessUpdate.h)
UPDATED, NOTHING_TO_DO = 0, 2
LINUX = ("lin64", "lin32")
STEPS = [("latest", "stable", UPDATED, "stable"), ("stable", "stable", NOTHING_TO_DO, "stable"),
         ("stable", "latest", UPDATED, "latest"), ("latest", "latest", NOTHING_TO_DO, "latest")]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--if-configured", action="store_true")
    parser.add_argument("--restore-on-failure", action="store_true")
    parser.add_argument("--platform", default="lin64")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    if args.if_configured and not settings["UPDATER_ROOT"]:
        log("this setup has no new updater tree (UPDATER_ROOT is empty); nothing to do")
        return
    p = common.platform(args.platform)
    if p.name not in LINUX:
        common.die("the update test runs Linux platforms only (%s), not %s" % (" ".join(LINUX), p.name))
    tree = updater_tree.Tree(settings)
    current = tree.tags()
    if not current:
        common.die("%s has no tags document" % tree.root)
    tags = current["tags"]
    if tags["latest"] == tags["stable"]:
        log("Latest is Stable (build %d): nothing to test" % tags["latest"])
        return
    try:
        test(settings, p, tree, tags, args.keep)
    except SystemExit as e:
        if e.code and args.restore_on_failure:
            restore(settings, args.config, tags["latest"])
        raise


def test(settings, p, tree, tags, keep):
    builds = {tag: tree.build(tags[tag]) for tag in ("latest", "stable")}
    variants = {}
    for tag, build in builds.items():
        if p.variant not in build["variants"]:
            common.die("%s (build %d) has no %s variant" % (tag, build["build"], p.variant))
        variants[tag] = build["variants"][p.variant]
    log("Latest is build %d, Stable build %d; testing %s (%s)" % (tags["latest"], tags["stable"], p.name, p.variant))

    work = Path(tempfile.mkdtemp(prefix="update-test-%d-" % tags["latest"], dir=common.STATE))
    passed = False
    try:
        install = work / "t"
        install.mkdir()
        tls = make_tls(work / "tls", hosts(settings, tree))
        # Stable first, only to ask it whether it knows --update
        assemble(tree, variants["stable"], work / "stable" / p.bundle)
        if not knows_update(settings, p, work / "stable", tls):
            common.die("Stable (build %d) doesn't know --update (built before 2026-11-12), so the way back can't be tested; "
                       "move Stable to a newer build first" % tags["stable"])
        assemble(tree, variants["latest"], install / p.bundle)
        for n, (start, tier, expected, files) in enumerate(STEPS, 1):
            log("step %d: %s (build %d) --update %s, expecting %d"
                % (n, start.capitalize(), tags[start], tier, expected))
            status = step(settings, p, work, install, tls, tier)
            keep_logs(install / p.bundle, work / ("step%d" % n))
            if status != expected:
                common.die("step %d: --update %s exited %d, not %d; logs in %s" % (n, tier, status, expected, work / ("step%d" % n)))
            problems = check_installed(tree, install / p.bundle, variants[files], variants["latest" if files == "stable" else "stable"])
            if problems:
                common.die("step %d: the installation isn't %s's (build %d): %s"
                           % (n, files.capitalize(), tags[files], "; ".join(problems[:10])))
        passed = True
        log("passed: build %d goes back to Stable (build %d) and up again on %s" % (tags["latest"], tags["stable"], p.name))
    finally:
        if passed and not keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            log("work directory kept: %s" % work)


def restore(settings, config, build):
    """Put the tree back as it was before updater/deploy.py wrote `build` (its snapshot)."""
    snapshots = sorted(common.snapshots_dir(settings).glob("*-before-updater-%d" % build))
    if not snapshots:
        log("no snapshot from before build %d to restore; the tree still has it as Latest: don't publish it" % build)
        return
    log("the test failed: restoring %s, so build %d isn't published" % (snapshots[-1].name, build))
    command = [sys.executable, str(HERE.parent / "files" / "snapshots.py")]
    if config:
        command += ["--config", config]
    subprocess.run(command + ["restore", snapshots[-1].name], check=False)


def hosts(settings, tree):
    """The host names Launchers fetch the tree from: the setup's FILES_MIRRORS (compiled in)
    and the tree's mirror list. Each must serve the site at its root, as the test serves it."""
    urls = set(settings["FILES_MIRRORS"].split())
    data = tree.read("mirrors.json")
    if data is not None:
        for group in formats.check_document("mirrors.json", data)["mirrors"]:
            urls.update(group)
    names = set()
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.path not in ("", "/"):
            common.die("the update test serves the files site at the root of an https host, not %s" % url)
        names.add(parts.hostname)
    return sorted(names)


def make_tls(directory, names):
    """A CA for this run, and a certificate from it for the host names. The CA alone is
    what the Launcher trusts in the test (as the image's /etc/ssl/certs)."""
    certs = directory / "certs"
    certs.mkdir(parents=True)
    openssl = ["openssl"]
    common.run(*openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
               "-keyout", directory / "ca.key", "-out", directory / "ca.pem", "-subj", "/CN=hopper update test CA",
               "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign",
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    common.run(*openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", directory / "server.key",
               "-out", directory / "server.csr", "-subj", "/CN=%s" % names[0],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (directory / "server.ext").write_text("subjectAltName=%s\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
                                          % ",".join("DNS:" + name for name in names))
    common.run(*openssl, "x509", "-req", "-in", directory / "server.csr", "-CA", directory / "ca.pem",
               "-CAkey", directory / "ca.key", "-CAcreateserial", "-days", "1", "-out", directory / "server.pem",
               "-extfile", directory / "server.ext", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # OpenSSL looks CAs up by hash in the directory, Qt reads the bundle: both
    shutil.copy(directory / "ca.pem", certs / "ca-certificates.crt")
    name = common.run(*openssl, "x509", "-hash", "-noout", "-in", directory / "ca.pem",
                      capture_output=True, text=True).stdout.strip()
    shutil.copy(directory / "ca.pem", certs / ("%s.0" % name))
    return {"directory": directory, "hosts": names}


def assemble(tree, variant, root):
    """Install a variant from the tree's content files, as its manifest lists it."""
    for entry in variant["files"]:
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tree.root / formats.content_path(entry["sha256"]), target)
        target.chmod(0o755 if entry["executable"] else 0o644)
    for entry in variant.get("links", []):
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(entry["target"], target)


def container(settings, p, install, tls, command):
    """The command that runs `command` in the platform's image, with the tree served under the
    site's host names and only the test CA trusted."""
    cmd = common.container_run(settings)
    for name in tls["hosts"]:
        cmd += ["--add-host", "%s:127.0.0.1" % name]
    cmd += ["-v", "%s:/tree:ro" % settings["FILES_ROOT"], "-v", "%s:/tls:ro" % tls["directory"],
            "-v", "%s:/etc/ssl/certs:ro" % (tls["directory"] / "certs"), "-v", "%s:/t" % install,
            "-v", "%s:/run:ro" % HERE, settings[p.image_setting]]
    return cmd + command


def knows_update(settings, p, install, tls):
    """Whether the build installed in `install` knows --update: its --help says."""
    command = ["bash", "-c", "QT_QPA_PLATFORM=offscreen LAUNCHER_LIBRARY_PATH=/opt/qt56/lib /t/MultiMC/MultiMC --help"]
    result = subprocess.run(container(settings, p, install, tls, command), capture_output=True, text=True)
    return "--update" in result.stdout


def step(settings, p, work, install, tls, tier):
    result = subprocess.run(container(settings, p, install, tls, ["bash", "/run/update-test-run.sh", tier]),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in result.stdout.splitlines():
        # the Launcher's own summary line (the log has it too)
        if line.startswith("Update (headless)"):
            log("    " + line.strip())
    return result.returncode


def keep_logs(root, directory):
    directory.mkdir()
    for log_file in root.glob("MultiMC-*.log"):
        shutil.copy(log_file, directory)


def check_installed(tree, root, variant, other):
    """What differs between the installation and the variant: missing or changed files,
    links, and files of the other build that should be gone."""
    problems, listed = [], set()
    for entry in variant["files"]:
        path = root / entry["path"]
        listed.add(entry["path"])
        if path.is_symlink() or not path.is_file():
            problems.append("%s is missing" % entry["path"])
        elif common.sha256_file(path) != entry["sha256"]:
            problems.append("%s has other content" % entry["path"])
        elif bool(path.stat().st_mode & 0o111) != entry["executable"]:
            problems.append("%s has the wrong executable bit" % entry["path"])
    for entry in variant.get("links", []):
        path = root / entry["path"]
        listed.add(entry["path"])
        if not path.is_symlink() or os.readlink(path) != entry["target"]:
            problems.append("the link %s isn't to %s" % (entry["path"], entry["target"]))
    for entry in other["files"] + other.get("links", []):
        if entry["path"] not in listed and (root / entry["path"]).exists():
            problems.append("%s (only in the other build) is still there" % entry["path"])
    return problems


if __name__ == "__main__":
    main()
