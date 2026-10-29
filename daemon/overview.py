#!/usr/bin/env python3
"""What hopper and meta are doing on this machine, gathered from their files and systemd.

    daemon/overview.py [--json]

For every setup (each settings file next to this script but defaults.env: production,
files2), from its files: whether its hopper runs (the pid holding HOPPER_STATE's lock) and what it
last saw (HOPPER_STATE.status), its service (hopper@<setup>), each job's last run, what's
live (the published snapshot, the update channels, the website), and its runs, newest
first; a file of it that isn't what hopper writes there is its "error" instead, and the
other setups still show. For the machine: whether a release build runs (the pid holding its lock), the hopper checkout's commit,
free space, and Laminar: each job (meta among them), what starts it (daemon/trigger.py) and
its last run.

It only reads: run it any time, as the user hopper runs as. Without --json, a short text
summary; daemon/top.py shows the same live. The JSON is what other views read.
"""

import argparse
import datetime
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import hopper
import trigger

RUNS = 100  # the newest runs listed per setup

def gather():
    return {
        "time": common.now(),
        "host": socket.gethostname(),
        "code": git_describe(common.HOPPER),
        "release_build": common.lock_holder(common.RELEASE_LOCK),
        "disk": disk(common.STATE),
        "setups": [setup(path) for path in setup_files()],
        "laminar": laminar(),
    }


def setup_files():
    files = [p for p in common.CONFIG.glob("*.env") if p.name != common.DEFAULTS.name]
    return sorted(files, key=lambda p: (p.stem != "production", p.stem))


def setup(path):
    """A setup's hopper and service, and what its files say. A file of it that isn't what hopper
    writes there is its "error", in place of what its files say: the other setups still show."""
    settings = common.load_settings(path)
    state_path = Path(settings["HOPPER_STATE"])
    summary = {
        "name": path.stem,
        "config": str(path),
        "running": common.lock_holder(str(state_path) + ".lock"),
        "service": unit("hopper@%s.service" % path.stem),
        "status": None,
        "log": str(state_path) + ".logs/hopper.log",
        "jobs": [],
        "live": None,
        "runs": [],
        "error": None,
    }
    try:
        summary.update(from_files(settings, state_path))
    except Unreadable as error:
        summary["error"] = "%s: not what hopper writes there" % error
    return summary


class Unreadable(Exception):
    """A file that isn't what hopper writes there (its path)."""


def from_files(settings, state_path):
    entries = read_json(state_path) or {}
    status = read_json(Path(str(state_path) + ".status"))
    try:
        runs = sorted(((name, e) for name, es in entries.items() if name in hopper.JOBS for e in es),
                      key=lambda item: common.parse_time(item[1]["time"]), reverse=True)
        jobs = [job_summary(settings, job, entries.get(job.name, []), (status or {}).get("jobs", {}).get(job.name))
                for job in hopper.JOBS.values()]
        runs = [run_summary(name, e) for name, e in runs[:RUNS]]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Unreadable(state_path)
    return {"status": status, "jobs": jobs, "live": live(settings), "runs": runs}


def job_summary(settings, job, entries, seen):
    stream = job.name in hopper.STREAMS
    summary = {
        "name": job.name,
        "kind": "stream" if stream else "scheduled",
        "enabled": job.enabled(settings),
        "off": job.off(settings),
        "source": ("%s %s" % (settings[job.repo_setting], settings[job.branch_setting]) if stream
                   else settings[job.schedule_setting]),
        "seen": seen,
        "last": run_summary(job.name, entries[-1]) if entries else None,
        "last_published": next((run_summary(job.name, e) for e in reversed(entries) if e["status"] == "published"), None),
    }
    if stream and seen and seen.get("head"):
        summary["head_handled"] = any(e.get("commit") == seen["head"] for e in entries)
    return summary


def run_summary(job, entry):
    """A state entry, with its job, a label, and how long it took (so far)."""
    steps = entry.get("steps", [])
    end = entry.get("finished") or (steps[-1].get("end") if steps else None)
    if entry["status"] == "running":
        end = common.now()
    return {**entry, "job": job, "label": hopper.label(entry),
            "release": os.path.basename(entry["release"]) if entry.get("release") else None,
            "seconds": seconds_between(entry["time"], end) if end else None}


def live(settings):
    """What this setup has published: its files tree's last published snapshot, the newest
    version in each update channel ({channel: {version id: [platforms]}}), the website."""
    snapshot = common.published_marker(settings)
    name = snapshot.read_text().strip() if snapshot.is_file() else None
    info_path = common.snapshots_dir(settings) / (name + ".json") if name else None
    info = read_json(info_path) if name else None
    try:
        if info and "created" in info:
            common.parse_time(info["created"])
    except (ValueError, TypeError):
        raise Unreadable(info_path)
    channels = {}
    for index in sorted(Path(settings["FILES_ROOT"], "update").glob("*/*/index.json")):
        try:
            versions = common.channel_versions(index.read_text())
        except (ValueError, KeyError):
            continue
        if versions:
            newest = max(versions, key=lambda v: int(v["Id"]) if v["Id"].isdigit() else 0)
            channels.setdefault(index.parent.name, {}).setdefault(newest["Id"], []).append(index.parent.parent.name)
    releases = Path(settings["RELEASES_DIR"])
    return {
        "published": {"snapshot": name, **(info or {})} if name else None,
        "channels": channels,
        "website": {m.stem.split("-", 1)[1]: m.read_text().strip() for m in sorted(releases.glob("website-*.live"))},
    }


LAMINAR_RESULTS = ["unknown", "queued", "running", "aborted", "failed", "success"]  # Laminar's RunState


def laminar():
    """Laminar (LAMINAR_HOME, ~/laminar) and the trigger that starts its jobs: for each job,
    what starts it, what the trigger last handled, Laminar's last finished run, and its runs
    going now. None where there is no Laminar."""
    home = trigger.LAMINAR_HOME
    jobs_dir = home / "cfg" / "jobs"
    if not jobs_dir.is_dir():
        return None
    status = read_json(home / "trigger.status") or {}
    handled = read_json(home / "trigger.json") or {}
    found = trigger.triggers()
    last_runs = laminar_last_runs(home / "laminar.sqlite")
    running = laminar_running()
    jobs = []
    for run_file in sorted(jobs_dir.glob("*.run")):
        name = run_file.stem
        found_trigger = found.get(name)
        seen = status.get("jobs", {}).get(name) or {}
        entries = handled.get(name) or []
        jobs.append({
            "name": name,
            "trigger": found_trigger.describe() if isinstance(found_trigger, trigger.Trigger) else None,
            "problem": found_trigger if isinstance(found_trigger, str) else seen.get("problem"),
            "seen": seen,
            "handled": entries[-1] if entries else None,
            "last_run": last_runs.get(name),
            "running": [r for r in running if r.split(":")[0] == name] if running is not None else None,
        })
    return {
        "home": str(home),
        "laminard": unit("laminar.service"),
        "service": unit("trigger.service"),
        "trigger": common.lock_holder(home / "trigger.lock"),
        "status": status or None,
        "jobs": jobs,
    }


def laminar_last_runs(database):
    """{job: its newest finished run}, from Laminar's database (read only)"""
    if not database.exists():
        return {}
    try:
        with sqlite3.connect("file:%s?mode=ro" % database, uri=True, timeout=2) as db:
            rows = db.execute("SELECT name, number, result, startedAt, completedAt, reason FROM builds b "
                              "WHERE number = (SELECT MAX(number) FROM builds WHERE name = b.name)").fetchall()
    except sqlite3.Error:
        return {}
    runs = {}
    for name, number, result, started, completed, reason in rows:
        runs[name] = {
            "run": "%s:%d" % (name, number),
            "result": LAMINAR_RESULTS[result] if 0 <= result < len(LAMINAR_RESULTS) else str(result),
            "started": common.timestamp(datetime.datetime.fromtimestamp(started)) if started else None,
            "seconds": completed - started if started and completed else None,
            "reason": reason or None,
        }
    return runs


def laminar_running():
    """The runs going now ("job:number"), or None if laminard can't be asked"""
    command, env = trigger.laminarc_command("show-running")
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.split() if result.returncode == 0 else None


def unit(name, *properties):
    """A systemd user unit's state, times as hopper writes them (null: never)."""
    properties = properties or ("ActiveState", "SubState", "Result", "UnitFileState", "ActiveEnterTimestamp",
                                "InactiveEnterTimestamp", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
                                "ExecMainStatus", "NRestarts", "MainPID")
    result = subprocess.run(["systemctl", "--user", "show", name, "--timestamp=unix", "-p", ",".join(properties)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if value.startswith("@") and value[1:].isdigit():
            value = common.timestamp(datetime.datetime.fromtimestamp(int(value[1:])))
        elif value.isdigit():
            value = int(value)
        values[key] = value if value != "" else None
    return values


def disk(path):
    usage = shutil.disk_usage(path)
    return {"path": str(path), "free": usage.free, "total": usage.total}


def git_describe(path):
    return subprocess.run(["git", "-C", str(path), "describe", "--always", "--dirty"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip() or None


def read_json(path):
    """A file's JSON; None if it doesn't exist."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise Unreadable(path)


def seconds_between(start, end):
    return int((common.parse_time(end) - common.parse_time(start)).total_seconds())


def duration(seconds):
    if seconds is None:
        return ""
    if seconds < 3600:
        return "%dm%02ds" % divmod(seconds, 60)
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)


def short_time(text):
    """ "2026-10-27T12:00:06+01:00" -> "10-27 12:00" """
    return text[5:16].replace("T", " ") if text else ""


def print_text(data):
    print("%s  %s  code %s%s" % (data["host"], data["time"], data["code"],
                                 "  (a release build is running)" if data["release_build"] else ""))
    for s in data["setups"]:
        service = s["service"]
        print("\n%s: hopper %s, service %s/%s, %s restarts" % (
            s["name"], "running" if s["running"] else "not running", service.get("ActiveState"),
            service.get("UnitFileState"), service.get("NRestarts")))
        if s["error"]:
            print("  can't read its files: %s" % s["error"])
        for job in s["jobs"]:
            last = job["last"]
            print("  %-13s %-4s %-26s %s" % (job["name"], "" if job["enabled"] else "off", job["source"].split()[-1] if job["kind"] == "stream" else job["source"],
                                             "%s %s %s %s" % (short_time(last["time"]), last["label"], last["status"], duration(last["seconds"])) if last else "-"))
    lam = data["laminar"]
    if lam:
        print("\nlaminar: laminard %s, trigger %s%s" % (
            lam["laminard"].get("ActiveState"), "running" if lam["trigger"] else "not running",
            ", last look failed: %s" % lam["status"]["problem"] if (lam["status"] or {}).get("problem") else ""))
        for job in lam["jobs"]:
            last = job["last_run"]
            print("  %-13s %-40s %s%s%s" % (
                job["name"], job["trigger"] or ("problem: %s" % job["problem"] if job["problem"] else "by hand"),
                "%s %s %s %s" % (last["run"], last["result"], short_time(last["started"]), duration(last["seconds"])) if last else "-",
                "  running %s" % " ".join(job["running"]) if job["running"] else "",
                "  next %s" % short_time(job["seen"]["next"]) if job["seen"].get("next") else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="everything, as JSON")
    args = parser.parse_args()
    data = gather()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_text(data)


if __name__ == "__main__":
    main()
