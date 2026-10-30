#!/usr/bin/env python3
"""What's released and what Laminar is doing on this machine, gathered from files, Laminar
and systemd.

    daemon/overview.py [--json]

For every setup (each settings file in config/ but defaults.env: production, files2), what's
live: the published snapshot of its files tree, the newest version in each update channel,
the website; a file of it that isn't what the scripts write there is its "error" instead,
and the other setups still show. For the machine: whether a release build runs (the pid
holding its lock), the hopper checkout's commit, free space, and Laminar: each job, what
starts it (daemon/trigger.py) and its last run, and the runs, newest first, each with the
URL of its log.

It only reads: run it any time, as the user the jobs run as (hopper). Without --json, a short text
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
import time
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import trigger

RUNS = 100  # the newest runs listed
# Laminar's web UI and logs: laminar.service's LAMINAR_BIND_HTTP
LAMINAR_HTTP = os.environ.get("LAMINAR_HTTP") or "http://127.0.0.1:8091"

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
    """What a setup has live. A file of it that isn't what the scripts write there is its
    "error", in place of that: the other setups still show."""
    summary = {"name": path.stem, "config": str(path), "live": None, "error": None}
    try:
        summary["live"] = live(common.load_settings(path))
    except Unreadable as error:
        summary["error"] = "%s: not what the scripts write there" % error
    return summary


class Unreadable(Exception):
    """A file that isn't what the scripts write there (its path)."""


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
    database = home / "laminar.sqlite"
    last_runs = laminar_last_runs(database)
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
        "runs": laminar_runs(database),
    }


def log_url(run):
    job, number = run.split(":")
    return "%s/log/%s/%s" % (LAMINAR_HTTP, job, number)


def laminar_last_runs(database):
    """{job: its newest finished run}, from Laminar's database (read only)"""
    return {run["job"]: run for run in laminar_query(
        database, "WHERE number = (SELECT MAX(number) FROM builds WHERE name = b.name AND result IS NOT NULL)")}


def laminar_runs(database):
    """The newest RUNS runs (those going now among them), newest first, from Laminar's
    database (read only)"""
    return laminar_query(database, "ORDER BY COALESCE(startedAt, queuedAt) DESC LIMIT %d" % RUNS)


def laminar_query(database, where):
    if not database.exists():
        return []
    try:
        with sqlite3.connect("file:%s?mode=ro" % database, uri=True, timeout=2) as db:
            rows = db.execute("SELECT name, number, result, startedAt, completedAt, reason FROM builds b " + where).fetchall()
    except sqlite3.Error:
        return []
    return [{
        "run": "%s:%d" % (name, number),
        "job": name,
        # no result yet: going now
        "result": (("running" if started else "queued") if result is None else
                   LAMINAR_RESULTS[result] if 0 <= result < len(LAMINAR_RESULTS) else str(result)),
        "started": common.timestamp(datetime.datetime.fromtimestamp(started)) if started else None,
        "seconds": (completed or int(time.time())) - started if started else None,  # so far, if going
        "reason": reason or None,
        "log": log_url("%s:%d" % (name, number)),
    } for name, number, result, started, completed, reason in rows]


def laminar_running():
    """The runs going now ("job:number"), or None if laminard can't be asked"""
    command, env = trigger.laminarc_command("show-running")
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.split() if result.returncode == 0 else None


def unit(name, *properties):
    """A systemd user unit's state, times as common.timestamp writes them (null: never)."""
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
        if s["error"]:
            print("\n%s: can't read its files: %s" % (s["name"], s["error"]))
            continue
        live = s["live"]
        published = live["published"]
        print("\n%s: published %s" % (s["name"], published["snapshot"] if published else "-"))
        for channel, versions in sorted(live["channels"].items()):
            print("  %-13s %s" % (channel, " ".join(sorted(versions, key=lambda v: int(v) if v.isdigit() else 0, reverse=True))))
        if live["website"]:
            print("  %-13s %s" % ("website", "  ".join("%s %s" % item for item in sorted(live["website"].items()))))
    lam = data["laminar"]
    if lam:
        print("\nlaminar: laminard %s, trigger %s%s" % (
            lam["laminard"].get("ActiveState"), "running" if lam["trigger"] else "not running",
            ", last look failed: %s" % lam["status"]["problem"] if (lam["status"] or {}).get("problem") else ""))
        for job in lam["jobs"]:
            last = job["last_run"]
            print("  %-23s %-40s %s%s%s" % (
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
