#!/usr/bin/env python3
"""Check the new updater's documents and trees against the format spec (format 1, the Design
repo's UpdaterFormats.md).

    updater/validate.py document <file> [--name PATH] (--keys KEYS | --unsigned) [--now TIME]
    updater/validate.py tree <root> --floor KEYS [--now TIME] [--no-hashes]

document   one document and its signature (<file>.sig). What kind it is follows from its path
           in the tree, taken from <file> or given with --name (builds/4312.json, tags.json,
           mirrors.json, keys/3.json). --keys is the keys document to trust: for keys/<N>.json
           the version before it (version 1 is checked against its own root keys and needs
           no --keys), for everything else the one in force, which must not have expired.
           --unsigned checks only the document, before it is signed.
tree       a whole tree: nothing but the layout, the keys chain from version 1, the floor
           among it, the newest keys document unexpired, every document signed, the tags
           pointing at present builds, every listed content file present with its size and
           SHA-256. --floor is the keys document a Launcher would embed. Content files no
           manifest lists are only reported. --no-hashes skips reading the content files.

Exits non-zero on the first problem. --now (YYYY-MM-DDTHH:MM:SSZ) replaces the current time
for expiry checks. Needs the openssl CLI.
"""

import argparse
import datetime
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("validate")


def parse_now(text):
    if text is None:
        return datetime.datetime.now(datetime.timezone.utc)
    try:
        return formats._time(text, "--now")
    except formats.Invalid as e:
        common.die(str(e))


def document(args):
    path = Path(args.file)
    if args.name:
        name = args.name
    elif path.parent.name in ("builds", "keys"):
        name = "%s/%s" % (path.parent.name, path.name)
    else:
        name = path.name
    data = path.read_bytes()
    try:
        doc = formats.check_document(name, data)
    except formats.Invalid as e:
        raise formats.Invalid("%s: %s" % (name, e))
    if args.unsigned:
        log("valid, unsigned: %s as %s" % (path, name))
        return
    keys_doc = None
    if args.keys:
        keys_doc = formats.check_keys(formats.parse_json(Path(args.keys).read_bytes(), formats.MAX_DOCUMENT))
        if formats.kind_of(name)[0] != "keys" and formats.expires(keys_doc) <= parse_now(args.now):
            raise formats.Invalid("%s expired at %s" % (args.keys, keys_doc["expires"]))
    elif formats.kind_of(name) != ("keys", 1):
        common.die("--keys is needed for %s (or --unsigned)" % name)
    signature = Path(str(path) + ".sig").read_bytes()
    formats.verify(data, signature, formats.trusted_keys(name, doc, keys_doc))
    log("valid: %s as %s" % (path, name))


def tree(args):
    summary, warnings = formats.check_tree(args.root, Path(args.floor).read_bytes(), parse_now(args.now),
                                           hashes=not args.no_hashes)
    for warning in warnings:
        log("warning: %s" % warning)
    log("valid: %s: %s" % (args.root, summary))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    doc = commands.add_parser("document")
    doc.add_argument("file")
    doc.add_argument("--name", help="its path in the tree, if <file> isn't there")
    trust = doc.add_mutually_exclusive_group()
    trust.add_argument("--keys", help="the keys document to trust")
    trust.add_argument("--unsigned", action="store_true", help="check the document only")
    doc.add_argument("--now")
    tr = commands.add_parser("tree")
    tr.add_argument("root")
    tr.add_argument("--floor", required=True, help="the keys document a Launcher would embed")
    tr.add_argument("--now")
    tr.add_argument("--no-hashes", action="store_true")
    args = parser.parse_args()
    try:
        {"document": document, "tree": tree}[args.command](args)
    except formats.Invalid as e:
        common.die(str(e))
    except OSError as e:
        common.die(str(e))


if __name__ == "__main__":
    main()
