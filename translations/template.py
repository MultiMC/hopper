#!/usr/bin/env python3
"""Update the translations template (template.pot) from the Launcher source, and push it.

    translations/template.py [--config FILE] [--dry-run] [launcher commit]

1. Updates the local mirrors of LAUNCHER_REPO and TRANSLATIONS_REPO, and checks out
   TRANSLATIONS_BRANCH's head, with the Launcher at the commit (default: the commit of the
   new updater's Testing build, which is Stable's or newer, or the head of LAUNCHER_BRANCH
   for a setup without a tree)
   in its src/, with submodules. All in RELEASES_DIR/template-<time>/.
2. Runs the Translations repo's update.py there, in IMAGE_TRANSLATIONS, without network.
   It prints how many strings were added and removed.
3. If template.pot didn't change: nothing to do. Otherwise commits it ("Update translation
   template for latest MultiMC code", as TeamCity's job did) and pushes it to
   TRANSLATIONS_BRANCH at TRANSLATIONS_PUSH_URL with the deploy key TRANSLATIONS_DEPLOY_KEY
   (outside this repo; GitHub's host key must be known). Crowdin picks it up from there.
   The push fails, changing nothing, if the branch moved meanwhile; run it again.

--dry-run: everything but the push (the commit stays in the work directory).
One at a time (shares the release lock). Laminar's job template-production runs it when
production's Testing or Stable moves (queued by the tag job, laminar/cfg/scripts/tag), so
translators get new strings ahead of Stable, but not with every build.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("template")


def testing_commit(settings):
    """The Launcher commit of the new updater tree's Testing build (Stable's or newer), or
    None for a setup without a tree."""
    if not settings["UPDATER_ROOT"]:
        return None
    root = Path(settings["FILES_ROOT"], settings["UPDATER_ROOT"])
    tags_path = root / "tags.json"
    if not tags_path.is_file():
        common.die("%s has no tags document: no Testing build to take the template from" % root)
    testing = formats.check_document("tags.json", tags_path.read_bytes())["tags"]["testing"]
    name = "builds/%d.json" % testing
    commit = formats.check_document(name, (root / name).read_bytes())["commit"]
    log("Testing is build %d, commit %s" % (testing, commit[:10]))
    return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="settings file overriding defaults.env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("commit", nargs="?", help="Launcher commit (default: Testing's, or the head of LAUNCHER_BRANCH)")
    args = parser.parse_args()
    common.stop_on_sigterm()

    settings = common.load_settings(args.config)
    key = Path(settings["TRANSLATIONS_DEPLOY_KEY"])
    if not args.dry_run and not key.is_file():
        common.die("no deploy key at %s (or use --dry-run)" % key)

    with common.lock_release():
        log("updating mirrors")
        common.update_mirror(settings["LAUNCHER_REPO"], common.LAUNCHER_MIRROR)
        common.update_mirror(settings["TRANSLATIONS_REPO"], common.TRANSLATIONS_MIRROR)
        launcher = common.resolve_on_branch(common.LAUNCHER_MIRROR, settings["LAUNCHER_BRANCH"],
                                            args.commit or testing_commit(settings))
        translations_branch = settings["TRANSLATIONS_BRANCH"]
        translations = common.resolve_on_branch(common.TRANSLATIONS_MIRROR, translations_branch)

        work = Path(settings["RELEASES_DIR"]) / ("template-%s" % time.strftime("%Y%m%d-%H%M%S"))
        repo = work / "translations"
        log("work directory %s" % work)
        common.run("git", "clone", "-q", "--no-hardlinks", "-b", translations_branch, common.TRANSLATIONS_MIRROR, repo)
        common.run("git", "-C", repo, "reset", "-q", "--hard", translations)
        log("Launcher %s at %s, Translations %s at %s"
            % (settings["LAUNCHER_BRANCH"], launcher[:10], translations_branch, translations[:10]))
        common.run("git", "clone", "-q", "--no-hardlinks", common.LAUNCHER_MIRROR, repo / "src")
        common.run("git", "-C", repo / "src", "checkout", "-q", launcher)
        common.run("git", "-C", repo / "src", "submodule", "-q", "update", "--init", "--recursive")

        log("running update.py")
        result = subprocess.run(common.container_run(settings) + ["--network", "none",
                                 "-e", "PYTHONDONTWRITEBYTECODE=1",
                                 "-v", "%s:/work" % repo, "-w", "/work", settings["IMAGE_TRANSLATIONS"],
                                 "python3", "update.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (work / "update.log").write_text(result.stdout)
        for line in result.stdout.splitlines():
            if line.startswith("    ") and ("strings," in line or line.startswith(("    added", "    removed"))):
                print(line)
        if result.returncode:
            common.die("update.py failed (see %s)" % (work / "update.log"))

        common.run("git", "-C", repo, "add", "template.pot")
        if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", "template.pot"]).returncode == 0:
            log("Nothing to do, updated template is identical")
            return

        common.run("git", "-C", repo, "-c", "user.name=Automation", "-c", "user.email=automation@multimc.org",
                   "commit", "-q", "-m", "Update translation template for latest MultiMC code\n\n"
                   "From Launcher %s %s." % (settings["LAUNCHER_BRANCH"], launcher))
        commit = common.output("git", "-C", repo, "rev-parse", "HEAD")
        if args.dry_run:
            log("dry run: committed %s in %s, not pushed" % (commit[:10], repo))
            return

        log("pushing %s to %s %s" % (commit[:10], settings["TRANSLATIONS_PUSH_URL"], translations_branch))
        ssh = "ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes" % key
        common.run("git", "-C", repo, "push", "-q", settings["TRANSLATIONS_PUSH_URL"],
                   "HEAD:refs/heads/" + translations_branch, env={**os.environ, "GIT_SSH_COMMAND": ssh})
        log("pushed")


if __name__ == "__main__":
    main()
