#!/usr/bin/env python3
"""A setup's builds and what can be done to them, for web/ and for anyone with a shell here.

    daemon/builds.py list <setup>
    daemon/builds.py check <setup> --serial N <action>
    daemon/builds.py queue <setup> --serial N <action>

where <action> is one of

    tag [testing <build>] [stable <build>]   move the tags (updater/tag.py)
    remove <build>                           withdraw a build (updater/remove.py)
    prune [<MB>]                             purge what's over the cap now, or this cap this
                                             once (updater/prune.py)

list   the builds, newest first, as JSON: number, commit and its subject, when built,
       platforms and their downloads, the tags naming it, whether it has a kept archive; and
       the tags, their serial, the withdrawn builds and the cap. A setup with a new updater tree lists
       the tree's builds; one without, the Launcher releases in RELEASES_DIR, and has no tags.
check  what the action would do, as JSON: the script's --dry-run report and log, and the
       builds involved. Changes nothing.
queue  checks again and queues the Laminar job <action>-<setup>, which does it and publishes.
       Prints {"run": "<job>:<number>"}, without the number if laminarc doesn't say.

--serial is the serial `list` gave: if the tags have changed since, it's refused. Reasons
for a refusal go to stderr, with exit status 1.
"""

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import overview
import trigger
import updater_downloads as downloads
import updater_formats as formats

ACTIONS = ("tag", "remove", "prune")  # updater/<action>.py, Laminar's <action>-<setup>


def settings_of(setup):
    path = common.CONFIG / (setup + ".env")
    if path not in overview.setup_files():
        common.die("no setup %s" % setup)
    return path, common.load_settings(path)


def local_time(text):
    """A manifest's "2026-11-06T16:46:46Z", in the local time overview.py writes."""
    return common.timestamp(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone())


def job_of(action, setup):
    return "%s-%s" % (action, setup)


def subjects(commits):
    """{commit: its subject} from the Launcher's checkout mirror; {} if it can't tell, as a
    subject only helps to recognize a build."""
    result = subprocess.run(["git", "--git-dir", str(common.LAUNCHER_MIRROR), "log", "--no-walk=unsorted", "--format=%H %s", *commits],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode:
        return {}
    return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)


def commit_url(settings, commit):
    return "%s/commit/%s" % (settings["LAUNCHER_REPO"].removesuffix(".git"), commit)


def tree_builds(settings, tags):
    """The builds in the setup's new updater tree."""
    root = Path(settings["FILES_ROOT"], settings["UPDATER_ROOT"])
    names = {p.variant: p.name for p in common.PLATFORMS.values()}
    archived = downloads.archived_builds(settings)
    named = {}
    for tag, build in tags.items():
        named.setdefault(build, []).append(tag)
    builds = []
    for path in (root / "builds").glob("*.json"):
        doc = formats.check_document("builds/" + path.name, path.read_bytes())
        number = doc["build"]
        platforms = []
        for platform in sorted(names.get(variant, variant) for variant in doc["variants"]):
            url = overview.download_url(settings, "downloads/updater/archive/%d/mmc-%d-%s" % (number, number, platform))
            for tag in named.get(number, []):
                url = url or overview.download_url(settings, "downloads/updater/mmc-%s-%s" % (tag, platform))
            platforms.append({"platform": platform, "url": url})
        builds.append({"build": number, "commit": doc["commit"], "built": local_time(doc["built"]), "platforms": platforms,
                       "archived": number in archived,
                       "tags": [tag for tag in overview.TAGS if tag in named.get(number, [])]})
    return builds


def release_builds(settings):
    """The Launcher releases in RELEASES_DIR, each with its archive if it's the newest of its
    platform on the current layout's develop channel."""
    live = overview.live(settings)
    develop = live["channels"].get("develop", {})
    builds = []
    for path in Path(settings["RELEASES_DIR"]).glob("[0-9]*/manifest.json"):
        doc = json.loads(path.read_text())
        current = develop.get(str(doc["build_number"]), [])
        builds.append({
            "build": doc["build_number"], "commit": doc["commit"], "built": local_time(doc["built_at"]),
            "platforms": [{"platform": platform,
                           "url": live["channel_downloads"].get("develop", {}).get(platform) if platform in current else None}
                          for platform in sorted(doc["platforms"])],
            "tags": ["develop"] if current else [],
        })
    return builds


def gather(setup):
    _, settings = settings_of(setup)
    data = overview.live(settings)["updater"]
    has_tree = data is not None
    document = {}
    if has_tree:
        document = json.loads(Path(settings["FILES_ROOT"], settings["UPDATER_ROOT"], "tags.json").read_text())
    builds = tree_builds(settings, document["tags"]) if has_tree else release_builds(settings)
    builds.sort(key=lambda build: build["build"], reverse=True)
    titles = subjects([build["commit"] for build in builds])
    for build in builds:
        build["subject"] = titles.get(build["commit"])
        build["commit_url"] = commit_url(settings, build["commit"])
    return {
        "setup": setup,
        "has_tree": has_tree,
        # The jobs are what do it: without them, a setup's builds can only be looked at
        "can_act": has_tree and all((trigger.LAMINAR_HOME / "cfg" / "jobs" / (job_of(action, setup) + ".run")).exists()
                                    for action in ACTIONS),
        "serial": document.get("serial"),
        "tags": document.get("tags"),
        "withdrawn": document.get("withdrawn", []),
        "cap_mb": int(settings["UPDATER_MAX_SIZE_MB"]) if has_tree else None,
        "builds": builds,
    }


def check(setup, serial, action, arguments):
    """updater/<action>.py --dry-run's report, with its log and the builds it names. Stops with
    the script's reason if it refuses."""
    path, _ = settings_of(setup)
    result = subprocess.run([sys.executable, str(common.HOPPER / "updater" / (action + ".py")), "--config", str(path), "--dry-run", "--json",
                             "--serial", str(serial), *script_arguments(action, arguments)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        errors = [line for line in result.stderr.splitlines() if line.startswith("error: ")]
        common.die(errors[-1].removeprefix("error: ") if errors else result.stderr.strip() or "updater/%s.py failed" % action)
    report = json.loads(result.stdout)
    report["action"] = action
    report["log"] = result.stderr
    involved = {*report.get("before", {}).values(), *report.get("after", {}).values(), report.get("build"), *report.get("purged", [])}
    report["builds"] = {str(b["build"]): b for b in gather(setup)["builds"] if b["build"] in involved}
    return report


def script_arguments(action, arguments):
    if action == "prune" and arguments:
        return ["--cap-mb", arguments[0]]
    return arguments


def parameters_of(action, arguments):
    """What Laminar's job takes for the arguments, which check() has accepted."""
    if action == "tag":
        return ["%s=%s" % (tag.upper(), build) for tag, build in zip(arguments[::2], arguments[1::2])]
    if action == "remove":
        return ["BUILD=%s" % arguments[0]]
    return ["CAP_MB=%s" % argument for argument in arguments]


def queue(setup, serial, action, arguments):
    check(setup, serial, action, arguments)
    job = job_of(action, setup)
    parameters = parameters_of(action, arguments)
    reason = "asked for: %s" % (", ".join(parameters) or action)
    output = trigger.laminarc("queue", job, "SERIAL=%d" % serial, *parameters, reason=reason).strip()
    return {"run": output if output.startswith(job + ":") else job}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("list", "check", "queue"))
    parser.add_argument("setup")
    parser.add_argument("--serial", type=int)
    parser.add_argument("action", nargs="?", choices=ACTIONS)
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    if args.command == "list":
        output = gather(args.setup)
    else:
        if args.serial is None or args.action is None:
            common.die("%s needs --serial and an action" % args.command)
        output = (check if args.command == "check" else queue)(args.setup, args.serial, args.action, args.arguments)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
