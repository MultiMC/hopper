#!/usr/bin/env python3
"""Laminar's runs, for web/ and for anyone with a shell here.

    daemon/runs.py cancel <job> <number>

cancel  a queued run: it's marked cancelled, and when its turn comes it stops before its job
        starts and ends as aborted (Laminar can only abort runs that have started). Marks are
        files, LAMINAR_HOME/cancelled/<job>:<number>, read by LAMINAR_HOME/cfg/before.
        Prints {"cancelled": "<job>:<number>"}.

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
    parser.add_argument("command", choices=("cancel",))
    parser.add_argument("job")
    parser.add_argument("number", type=int)
    args = parser.parse_args()
    print(json.dumps(cancel(args.job, args.number), indent=2))


if __name__ == "__main__":
    main()
