#!/usr/bin/env python3
"""Once: convert a setup's files to the times hopper writes now, RFC 3339 in local time with
its offset ("2026-10-28T12:14:09+01:00"). Before, they were local time without an offset
("2026-10-28 12:14:09", a schedule's "2026-10-28 12:00") and a snapshot's "created" had
"+0100". Delete this script once every setup has been converted.

    daemon/convert-times.py [--config FILE] [--dry-run]

HOPPER_STATE    each run's times. A run from before step times were recorded, which has no
                "finished", gets its log's last write (what the overview showed as its end).
HOPPER_STATE.status    removed: `hopper watch` writes it again at its next look.
<FILES_ROOT>.snapshots/*.json    each snapshot's "created".

Stop the setup's hopper first: this takes its lock and the files tree's, and stops if either
is held. What's already converted stays as it is, so it can be run again. --dry-run: show
what would change, change nothing (without the locks: it can run while hopper does).
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common

log = common.logger("convert-times")

OLD_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def converted(text):
    """`text` as hopper writes times now."""
    for old in OLD_FORMATS:
        try:
            return common.timestamp(datetime.datetime.strptime(text, old))
        except ValueError:
            pass
    # With an offset already ("+0100" or "+01:00")
    return common.timestamp(datetime.datetime.fromisoformat(text))


def convert_entry(entry):
    """Convert one run's times; True if it got its log's last write as "finished"."""
    for key in ("time", "scheduled", "finished"):
        if key in entry:
            entry[key] = converted(entry[key])
    for step in entry.get("steps", []):
        for key in ("start", "end"):
            if key in step:
                step[key] = converted(step[key])
    if (entry["status"] != "running" and "finished" not in entry and not entry.get("steps")
            and entry.get("log") and os.path.isfile(entry["log"])):
        entry["finished"] = common.timestamp(datetime.datetime.fromtimestamp(os.path.getmtime(entry["log"])))
        return True
    return False


def write(path, data, dry_run):
    if dry_run:
        return
    new = Path(str(path) + ".new")
    new.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(new, path)


def convert_state(path, dry_run):
    if not path.exists():
        log("%s: none" % path)
        return
    data = json.loads(path.read_text())
    if "commits" in data:
        common.die("%s is from before translations ({\"commits\": [...]}); convert it by hand" % path)
    before = json.dumps(data)
    finished = 0
    for entries in data.values():
        for entry in entries:
            finished += convert_entry(entry)
    if json.dumps(data) == before:
        log("%s: already converted" % path)
        return
    log("%s: %d runs converted, %d of them finished when their log was last written"
        % (path, sum(len(entries) for entries in data.values()), finished))
    write(path, data, dry_run)


def convert_snapshots(settings, dry_run):
    infos = sorted(common.snapshots_dir(settings).glob("*.json"))
    changed = 0
    for path in infos:
        info = json.loads(path.read_text())
        created = converted(info["created"])
        if created != info["created"]:
            info["created"] = created
            write(path, info, dry_run)
            changed += 1
    log("%s: %d of %d snapshot infos converted" % (common.snapshots_dir(settings), changed, len(infos)))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = common.load_settings(args.config.resolve() if args.config else None)
    state = Path(settings["HOPPER_STATE"])

    if args.dry_run:
        convert(settings, state, dry_run=True)
        log("dry run: nothing changed")
        return
    with common.locked(str(state) + ".lock", "a hopper uses %s: stop it first" % state):
        with common.lock_files(settings):
            convert(settings, state, dry_run=False)


def convert(settings, state, dry_run):
    convert_state(state, dry_run)
    status = Path(str(state) + ".status")
    if status.exists():
        log("%s: removed" % status)
        if not dry_run:
            status.unlink()
    convert_snapshots(settings, dry_run)


if __name__ == "__main__":
    main()
