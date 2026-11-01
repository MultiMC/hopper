#!/usr/bin/env python3
"""Run a command in a platform's toolchain container, for working on the Launcher in an IDE.

    launcher/dev-run.py [--config FILE] <platform> <command> [args...]

Unlike run-build.py, which builds a release-like tree into its own output directory, this
runs any command (cmake, make, ctest, a shell) in the current directory, with your home
directory mounted at the same path. Source and build trees, compiler messages and the
compile database then have the same paths inside and outside, so an IDE can configure and
build in the container while it edits, runs and debugs on the host (see launcher/dev/).

platform   lin64 or lin32: Qt 5.6.3, g++-5, cmake 3.10.0, glibc 2.19, like the releases
command    run as you, no network, HOME=/tmp; CC/CXX are the image's gcc-5/g++-5, and
           LD_LIBRARY_PATH has the image's Qt, so tests run in the container;
           HOPPER_DEV_CONTAINER=1 tells the launcher/dev/ wrappers they are already inside

The image's Qt (/opt/qt56) is copied to $STATE/dev/<platform>/qt56 on first use, and again
whenever the image changes, so the host sees its headers too; configure with
-DCMAKE_PREFIX_PATH=<that> so the IDE's code model uses Qt 5.6 like the compiler does,
and -DCMAKE_PLATFORM_IMPLICIT_LINK_DIRECTORIES=<that>/lib, which keeps the copy out of the
binaries' RPATH: run on the host, the Launcher uses the host's Qt (as the releases do), not
the copy (which needs Ubuntu 14.04's libraries); in the container, LD_LIBRARY_PATH finds it.
`launcher/dev-run.py --qt-path <platform>` prints it.
"""

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common

# The Linux images keep Qt in /opt/qt56 and have gcc-5/g++-5 next to the older default gcc.
SUPPORTED = {"lin64", "lin32"}
QT_IN_IMAGE = "/opt/qt56"


def image_id(settings, image):
    return common.output(settings["CONTAINER_ENGINE"], "image", "inspect", "--format", "{{.Id}}", image).strip()


def host_qt(settings, plat):
    """The image's Qt, copied to where the host sees it; copied again when the image changed."""
    engine = settings["CONTAINER_ENGINE"]
    image = settings[plat.image_setting]
    if not common.image_exists(settings, image):
        common.die("image %s not found (%s)" % (image, plat.image_setting))
    base = common.STATE / "dev" / plat.name
    base.mkdir(parents=True, exist_ok=True)
    qt, stamp = base / "qt56", base / "image-id"
    wanted = image_id(settings, image)
    with open(base / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if qt.is_dir() and stamp.is_file() and stamp.read_text().strip() == wanted:
            return qt
        common.emit("copying %s:%s to %s" % (image, QT_IN_IMAGE, qt))
        new = base / "qt56.new"
        shutil.rmtree(new, ignore_errors=True)
        container = common.output(engine, "create", image, "true").strip()
        try:
            common.run(engine, "cp", "%s:%s" % (container, QT_IN_IMAGE), str(new))
        finally:
            subprocess.run([engine, "rm", container], stdout=subprocess.DEVNULL)
        shutil.rmtree(qt, ignore_errors=True)
        new.rename(qt)
        stamp.write_text(wanted + "\n")
    return qt


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("platform")
    parser.add_argument("--qt-path", action="store_true", help="print the host copy of the image's Qt and stop")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    settings = common.load_settings(args.config)
    plat = common.platform(args.platform)
    if plat.name not in SUPPORTED:
        common.die("dev-run.py supports %s, not %s" % (", ".join(sorted(SUPPORTED)), plat.name))
    qt = host_qt(settings, plat)
    if args.qt_path:
        print(qt)
        return
    if not args.command:
        parser.error("no command")

    home, cwd = str(Path.home()), os.getcwd()
    if not (cwd == home or cwd.startswith(home + "/")):
        common.die("run it from inside your home directory (%s), which is what the container sees" % home)
    command = common.container_run(settings) + [
        "--network", "none", "-v", "%s:%s" % (home, home), "-w", cwd,
        "-e", "CC=gcc-5", "-e", "CXX=g++-5", "-e", "LD_LIBRARY_PATH=%s/lib" % QT_IN_IMAGE,
        "-e", "HOPPER_DEV_CONTAINER=1",
        settings[plat.image_setting], *args.command]
    # exec: an IDE stopping the build stops the container with it
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
