#!/usr/bin/env python3
"""trigger: starts Laminar's jobs when they're due: on a new commit, or on a schedule.
Laminar runs jobs, keeps their logs and results; it doesn't watch repositories or keep
schedules itself. This does, and only queues jobs.

    daemon/trigger.py watch [--once]
    daemon/trigger.py status

Each job says what starts it in its own config, LAMINAR_HOME/cfg/jobs/<job>.conf (Laminar
only reads the keys it knows from there):

    TRIGGER=git <repository> <branch>    a new commit on the branch: laminarc queue <job> COMMIT=<commit>
    TRIGGER=cron <schedule>              the schedule is due ("minute hour day-of-month month
                                         day-of-week", this machine's time, see daemon/cron.py):
                                         laminarc queue <job> SCHEDULED=<time>

A job without TRIGGER is only started by hand (laminarc queue <job> ...). The configs are
read again at every look, so a trigger can be added or changed without restarting this.

watch    every INTERVAL seconds (60), look at each trigger. A new commit, or the most recent
         scheduled time not handled yet, queues the job once: after a time off it runs
         once, not once per missed commit or time. --once: look once, then exit.
status   each trigger, what it last handled and the Laminar run it queued

On the first look at a trigger, its current commit or most recent scheduled time is
adopted, not queued: it is assumed to be handled already (as hopper adopts a stream's
head). A job that is running or queued already isn't queued again; it's looked at again
next time, and then gets the newest commit. If laminarc fails (laminard down), nothing is
recorded and the next look tries again: a commit or a scheduled time is never skipped.

Kept in LAMINAR_HOME (default ~/laminar), next to Laminar's own files:
    trigger.json    what each trigger handled ({job: [entry, ...]}, oldest first: commit or
                    scheduled, time, status "queued" with Laminar's run "job:number", or "adopted")
    trigger.status  what the running `watch` saw at its last look, for overviews
    trigger.log     what `watch` printed (the journal has it too, but hopper can't read that)
One `watch` per LAMINAR_HOME (trigger.lock). laminarc reaches laminard by LAMINAR_HOST, by
default LAMINAR_HOME/rpc.sock (laminar.service's).
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import cron

log = common.logger("trigger")

LAMINAR_HOME = Path(os.environ.get("LAMINAR_HOME") or Path.home() / "laminar")
INTERVAL = 60
KEEP = 1000  # entries kept per job in trigger.json, the newest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("watch").add_argument("--once", action="store_true")
    commands.add_parser("status")
    args = parser.parse_args()

    common.stop_on_sigterm()
    state = State(LAMINAR_HOME / "trigger.json")
    if args.command == "status":
        state.print(triggers())
        return
    with common.locked(LAMINAR_HOME / "trigger.lock", "another trigger uses %s" % LAMINAR_HOME):
        if args.once:
            look(state)
        else:
            with common.log_also_to(LAMINAR_HOME / "trigger.log"):
                watch(state)


@dataclass(frozen=True)
class Trigger:
    job: str
    kind: str        # "git" or "cron"
    repo: str = None
    branch: str = None
    schedule: str = None

    def describe(self):
        return "%s %s" % (self.repo, self.branch) if self.kind == "git" else "cron %s" % self.schedule


def triggers():
    """{job: Trigger, or the reason its TRIGGER can't be used}, for every job with one"""
    found = {}
    for conf in sorted((LAMINAR_HOME / "cfg" / "jobs").glob("*.conf")):
        job = conf.name[:-len(".conf")]
        for line in conf.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() != "TRIGGER":
                continue
            kind, _, rest = value.strip().partition(" ")
            rest = rest.strip()
            if kind == "git" and len(rest.split()) == 2:
                found[job] = Trigger(job, "git", *rest.split())
            elif kind == "cron":
                try:
                    cron.Schedule(rest)
                    found[job] = Trigger(job, "cron", schedule=rest)
                except ValueError as e:
                    found[job] = "%s: %s" % (conf, e)
            else:
                found[job] = "%s: TRIGGER is `git <repository> <branch>` or `cron <schedule>`, not %r" % (conf, value)
    return found


class State:
    """trigger.json: {job: [entry, ...]}, each oldest first. An entry is a commit or a
    scheduled time handled: {"commit" or "scheduled", "time", "status", "run"}."""

    def __init__(self, path):
        self.path = path
        self.jobs = json.loads(path.read_text()) if path.exists() else {}

    def last(self, job):
        entries = self.jobs.get(job)
        return entries[-1] if entries else None

    def handled(self, job, key, value):
        return any(entry.get(key) == value for entry in self.jobs.get(job, []))

    def add(self, job, entry):
        self.jobs[job] = (self.jobs.get(job, []) + [entry])[-KEEP:]
        new = Path(str(self.path) + ".new")
        new.write_text(json.dumps(self.jobs, indent=2) + "\n")
        os.replace(new, self.path)

    def print(self, found):
        for job in sorted(set(found) | set(self.jobs)):
            trigger = found.get(job)
            what = trigger.describe() if isinstance(trigger, Trigger) else "problem: %s" % trigger if trigger else "no TRIGGER now"
            entry = self.last(job)
            last = ("%s %s %s %s" % (entry["time"], label(entry), entry["status"], entry.get("run") or "")
                    if entry else "nothing handled yet")
            print("%-12s %s\n%-12s last: %s" % (job, what, "", last))


class Status:
    """trigger.status (JSON), what the running `watch` saw, for overviews:

        {"pid", "started", "code" (the hopper commit it runs), "interval",
         "looked" (the end of its last look: a heartbeat), "problem" (why the last look
         failed, or null),
         "jobs": {job: {"trigger", "head" or "latest" and "next", "looked", "problem"}}}"""

    def __init__(self):
        self.path = LAMINAR_HOME / "trigger.status"
        self.data = {"pid": os.getpid(), "started": common.now(),
                     "code": subprocess.run(["git", "-C", str(common.HOPPER), "describe", "--always", "--dirty"],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip(),
                     "interval": INTERVAL, "looked": None, "problem": None, "jobs": {}}

    def saw(self, job, **values):
        self.data["jobs"][job] = {**values, "looked": common.now()}

    def save(self, **values):
        self.data.update(values)
        new = Path(str(self.path) + ".new")
        new.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(new, self.path)


def label(entry):
    """What an entry is about: a commit, or a scheduled time ("2026-10-29T22:00")"""
    return entry["commit"][:10] if "commit" in entry else entry["scheduled"][:16]


def watch(state):
    log("watching the triggers in %s every %d s" % (LAMINAR_HOME / "cfg" / "jobs", INTERVAL))
    status = Status()
    status.save()
    while True:
        try:
            look(state, status)
            status.save(looked=common.now(), problem=None)
        except common.Stopped:
            raise
        except SystemExit as e:
            log("this look failed (see above); trying again in %d s" % INTERVAL)
            status.save(looked=common.now(), problem=common.last_error or "exit %s" % e.code)
        time.sleep(INTERVAL)


reported = {}  # job: the problem last logged for it, so a lasting one is logged once


def look(state, status=None):
    """One look at every trigger; queues each job at most once. A trigger that fails (its
    repository can't be reached, laminarc fails) doesn't keep the others from their look;
    the look fails at the end."""
    found = triggers()
    failed = []
    busy = []  # the jobs Laminar has running or queued, asked for once per look when needed
    for job, trigger in found.items():
        if not isinstance(trigger, Trigger):
            if reported.get(job) != trigger:
                log("%s: %s; not triggered" % (job, trigger))
                reported[job] = trigger
            if status:
                status.saw(job, problem=trigger)
            continue
        reported.pop(job, None)
        try:
            look_at(state, status, trigger, busy)
        except common.Stopped:
            raise
        except SystemExit:
            failed.append(job)
            if status:
                status.saw(job, trigger=trigger.describe(), problem=common.last_error)
    if failed:
        common.die("the look failed for %s" % " ".join(failed))


def look_at(state, status, trigger, busy):
    job = trigger.job
    if trigger.kind == "git":
        commit = head(trigger)
        if status:
            status.saw(job, trigger=trigger.describe(), head=commit)
        if state.handled(job, "commit", commit):
            return
        key, value, reason = "commit", commit, "new commit %s on %s" % (commit[:10], trigger.branch)
    else:
        moment = datetime.datetime.now()
        schedule = cron.Schedule(trigger.schedule)
        latest, upcoming = schedule.latest(moment), schedule.next(moment)
        scheduled = common.timestamp(latest) if latest else None
        if status:
            status.saw(job, trigger=trigger.describe(), latest=scheduled,
                       next=common.timestamp(upcoming) if upcoming else None)
        if scheduled is None or state.handled(job, "scheduled", scheduled):
            return
        key, value, reason = "scheduled", scheduled, "scheduled %s" % scheduled[:16]

    if not state.last(job):
        state.add(job, {key: value, "time": common.now(), "status": "adopted"})
        log("%s: first look, %s adopted as already handled (not queued)" % (job, label({key: value})))
        return
    if not busy:
        busy.append(laminar_busy())
    if job in busy[0]:
        log("%s: %s, but it's running or queued already; looking again later" % (job, reason))
        return
    run = queue(job, "%s=%s" % (key.upper(), value), reason)
    state.add(job, {key: value, "time": common.now(), "status": "queued", "run": run})
    log("%s: %s, queued %s" % (job, reason, run))
    busy[0].add(job)


def head(trigger):
    """The commit at the head of the trigger's branch"""
    line = common.output("git", "ls-remote", trigger.repo, "refs/heads/" + trigger.branch)
    if not line:
        common.die("%s has no branch %s" % (trigger.repo, trigger.branch))
    return line.split()[0]


def laminarc_command(*args):
    """laminarc (LAMINAR_HOME/bin's, or the one on PATH) with args, and its environment"""
    program = LAMINAR_HOME / "bin" / "laminarc"
    env = {"LAMINAR_HOST": "unix:%s" % (LAMINAR_HOME / "rpc.sock"), **os.environ}
    return [str(program) if program.exists() else "laminarc", *args], env


def laminarc(*args, reason=None):
    """Run laminarc and return its output; stop with an error if it fails"""
    command, env = laminarc_command(*args)
    if reason:
        env["LAMINAR_REASON"] = reason
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    if result.returncode:
        # Without laminard, laminarc aborts with an exception and a stack dump: its what() is the message
        lines = result.stderr.strip().splitlines()
        message = next((line.split("what():", 1)[1].strip() for line in lines if "what():" in line),
                       lines[0] if lines else "")
        common.die("laminarc %s failed (exit %d): %s" % (" ".join(args), result.returncode, message))
    return result.stdout


def laminar_busy():
    """The jobs Laminar has running ("job:number" lines) or queued ("job" lines)"""
    lines = (laminarc("show-running") + laminarc("show-queued")).split()
    return {line.split(":")[0] for line in lines}


def queue(job, param, reason):
    """Queue the job with the parameter; returns Laminar's run, "job:number" (Laminar shows
    `reason` with the run)"""
    return laminarc("queue", job, param, reason="trigger: %s" % reason).strip()


if __name__ == "__main__":
    main()
