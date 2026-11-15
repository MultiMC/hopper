#!/usr/bin/env python3
"""Laminar's runs, for web/ and for anyone with a shell here.

    daemon/runs.py cancel <job> <number>
    daemon/runs.py queue <job> --reason TEXT [--commit COMMIT]

cancel  a queued run: it's marked cancelled, and when its turn comes it stops before its job
        starts and ends as aborted (Laminar can only abort runs that have started). Marks are
        files, LAMINAR_HOME/cancelled/<job>:<number>, read by LAMINAR_HOME/cfg/before.
        Prints {"cancelled": "<job>:<number>"}.

queue   a run of a job that can be started by hand (startable()): the jobs that take no
        parameters, or only COMMIT. With --commit, those that take it build that commit
        (the release scripts check that it's on the setup's branch); without, their default.
        The reason is required and shows as the run's reason ("by hand: <reason>").
        Prints {"run": "<job>:<number>"}. Not the tag, remove and prune jobs: those are queued
        by daemon/builds.py, which checks them first.

A run that isn't queued is refused, and so is one that started while it was being cancelled:
it may have read the mark or not, and its log says which. Reasons for a refusal go to stderr,
with exit status 1.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import trigger

CANCELLED = trigger.LAMINAR_HOME / "cancelled"
JOB_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
# The jobs that can be started by hand, by their name before the setup: whether they take COMMIT
STARTABLE = {"launcher": True, "translations": True, "website": True, "packages": True, "template": True,
             "patrons": False, "verify": False, "meta": False}


def startable(job):
    """None if the job can't be started by hand here; otherwise {"commit": whether it takes one}."""
    if not JOB_NAME.match(job) or not (trigger.LAMINAR_HOME / "cfg" / "jobs" / (job + ".run")).exists():
        return None
    kind = job.rsplit("-", 1)[0] if "-" in job else job
    if kind not in STARTABLE:
        return None
    return {"commit": STARTABLE[kind]}


def queue(job, reason, commit=None):
    how = startable(job)
    if how is None:
        common.die("%s can't be started from here" % job)
    reason = " ".join(reason.split())
    if not reason:
        common.die("say why: a reason is required")
    parameters = []
    if commit:
        if not how["commit"]:
            common.die("%s doesn't take a commit" % job)
        if not COMMIT.match(commit):
            common.die("not a commit: %r (7 to 40 hex digits)" % commit)
        parameters.append("COMMIT=%s" % commit)
    output = trigger.laminarc("queue", job, *parameters, reason="by hand: %s" % reason).strip()
    return {"run": output if output.startswith(job + ":") else job}


def mark(run):
    return CANCELLED / run


def is_cancelled(run):
    return mark(run).exists()


def cancel(job, number):
    if not JOB_NAME.match(job):
        common.die("no job %s" % job)
    run = "%s:%d" % (job, number)
    if run not in trigger.laminarc("show-queued").split():
        if run in trigger.laminarc("show-running").split():
            common.die("%s has started already" % run)
        common.die("%s isn't queued" % run)
    CANCELLED.mkdir(exist_ok=True)
    mark(run).write_text("cancelled at %s\n" % common.now())
    # The mark counts only if the run was still waiting once it was written
    if run not in trigger.laminarc("show-queued").split():
        common.die("%s started while it was being cancelled: its log says whether it stopped" % run)
    return {"cancelled": run}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("cancel", "queue"))
    parser.add_argument("job")
    parser.add_argument("number", type=int, nargs="?")
    parser.add_argument("--reason")
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.command == "cancel":
        if args.number is None:
            common.die("cancel needs the run's number")
        print(json.dumps(cancel(args.job, args.number), indent=2))
    else:
        print(json.dumps(queue(args.job, args.reason or "", args.commit), indent=2))


if __name__ == "__main__":
    main()
