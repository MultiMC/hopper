#!/usr/bin/env python3
"""Compare two Launcher releases of the same commit and build number, file by file.

    launcher/compare-releases.py [--json FILE] <release dir> <other release dir>

Compares each platform's bundle (deploy/<platform>.tar.gz): the same files, links and
permissions, and the same content. Differences the toolchains are known to make between
two builds of the same source are accepted, and listed as such:

- jars (.jar): the same entries with the same content; only the zip timestamps differ
- static and import libraries (.a, .dll.a): the same members with the same content; only
  the ar headers (timestamps) differ

Anything else that differs is a difference. Exit status 0 when there are none, 1 otherwise.
The report goes to standard output, and with --json as JSON to FILE.
"""

import argparse
import hashlib
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common


def bundle(release, platform):
    """{path: (kind, mode, link target or content)} of a platform's deploy archive."""
    entries = {}
    with tarfile.open(Path(release) / "deploy" / ("%s.tar.gz" % platform)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                entries[member.name] = ("file", member.mode, tar.extractfile(member).read())
            elif member.issym():
                entries[member.name] = ("link", member.mode, member.linkname)
            elif member.isdir():
                entries[member.name] = ("dir", member.mode, None)
            else:
                entries[member.name] = ("other", member.mode, None)
    return entries


def zip_entries(data):
    """A zip's entries and their content, without timestamps; None if it isn't a zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [(info.filename, z.read(info)) for info in z.infolist()]
    except zipfile.BadZipFile:
        return None


def ar_members(data):
    """An ar archive's members (name and content), without the rest of their headers
    (timestamps, owners, modes); None if it isn't an ar archive."""
    if not data.startswith(b"!<arch>\n"):
        return None
    members, pos = [], 8
    while pos < len(data):
        header = data[pos:pos + 60]
        if len(header) < 60 or header[58:60] != b"`\n":
            return None
        size = int(header[48:58].decode("ascii").strip())
        members.append((header[:16], data[pos + 60:pos + 60 + size]))
        pos += 60 + size + (size % 2)
    return members


def known_difference(path, a, b):
    """Why two different contents of `path` are the same apart from a known toolchain
    difference, or None."""
    if path.endswith(".jar"):
        entries = zip_entries(a)
        if entries is not None and entries == zip_entries(b):
            return "zip timestamps"
    if path.endswith(".a"):
        members = ar_members(a)
        if members is not None and members == ar_members(b):
            return "ar headers"
    return None


def compare_platform(a_release, b_release, platform):
    a, b = bundle(a_release, platform), bundle(b_release, platform)
    result = {"identical": 0, "known": {}, "different": [], "only_in_first": sorted(a.keys() - b.keys()),
              "only_in_second": sorted(b.keys() - a.keys())}
    for path in sorted(a.keys() & b.keys()):
        (kind_a, mode_a, value_a), (kind_b, mode_b, value_b) = a[path], b[path]
        if (kind_a, mode_a, value_a) == (kind_b, mode_b, value_b):
            result["identical"] += 1
        elif kind_a == kind_b == "file" and mode_a == mode_b and known_difference(path, value_a, value_b):
            result["known"][path] = known_difference(path, value_a, value_b)
        else:
            what = []
            if kind_a != kind_b:
                what.append("%s, %s" % (kind_a, kind_b))
            if mode_a != mode_b:
                what.append("mode %o, %o" % (mode_a, mode_b))
            if kind_a == kind_b and value_a != value_b:
                what.append("content" if kind_a == "file" else "target %s, %s" % (value_a, value_b))
                if kind_a == "file":
                    what.append("sha256 %s, %s" % (hashlib.sha256(value_a).hexdigest()[:12], hashlib.sha256(value_b).hexdigest()[:12]))
            result["different"].append({"path": path, "what": "; ".join(what)})
    result["ok"] = not (result["different"] or result["only_in_first"] or result["only_in_second"])
    return result


def compare(a_release, b_release):
    """The report for two releases: {"ok": bool, "problems": [...], "platforms": {...}}."""
    manifests = [json.loads((Path(r) / "manifest.json").read_text()) for r in (a_release, b_release)]
    report = {"first": str(a_release), "second": str(b_release), "problems": [], "platforms": {}}
    for key in ("commit", "build_number", "branch"):
        if manifests[0][key] != manifests[1][key]:
            report["problems"].append("%s differs: %s, %s" % (key, manifests[0][key], manifests[1][key]))
    platforms = [set(m["platforms"]) for m in manifests]
    if platforms[0] != platforms[1]:
        report["problems"].append("platforms differ: %s, %s" % (" ".join(sorted(platforms[0])), " ".join(sorted(platforms[1]))))
    for platform in sorted(platforms[0] & platforms[1]):
        report["platforms"][platform] = compare_platform(a_release, b_release, platform)
    report["ok"] = not report["problems"] and all(p["ok"] for p in report["platforms"].values())
    return report


def describe(report):
    lines = ["%s against %s: %s" % (report["first"], report["second"], "the same" if report["ok"] else "DIFFERENT")]
    lines += ["  " + problem for problem in report["problems"]]
    for platform, result in report["platforms"].items():
        lines.append("  %s: %d identical, %d with known differences, %d different%s" % (
            platform, result["identical"], len(result["known"]), len(result["different"]),
            "" if result["ok"] else " (NOT THE SAME)"))
        for path, why in result["known"].items():
            lines.append("    known: %s (%s)" % (path, why))
        for entry in result["different"]:
            lines.append("    different: %s (%s)" % (entry["path"], entry["what"]))
        for path in result["only_in_first"]:
            lines.append("    only in the first: %s" % path)
        for path in result["only_in_second"]:
            lines.append("    only in the second: %s" % path)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()
    for release in (args.first, args.second):
        if not (release / "manifest.json").is_file():
            common.die("%s isn't a release directory (no manifest.json)" % release)
    report = compare(args.first, args.second)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(describe(report))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
