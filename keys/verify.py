#!/usr/bin/env python3
"""Verify keys documents and signed documents of the new updater, the way a Launcher would.

    keys/verify.py keys <N>.json... (--roots KEY... | --known FILE) [--now TIME]
    keys/verify.py file <file> [--name PATH] (--keys FILE | --key KEY...) [--now TIME]

keys   keys documents, given in order, each with its <N>.json.sig, the version taken from
       the file name. --roots are the pinned root keys (hex or .pub files): the first
       document must then be version 1, list exactly those root keys and be signed by one.
       --known is a keys document trusted as it is, without a signature (a Launcher's floor,
       or the newest it has stored): the first document must then be the version after it.
       Each later one must be the version after the one before, signed by that one's root
       keys. A lower or repeated version is refused. Only the last has to be unexpired; it
       warns when that expires within 90 days.
file   one document and its <file>.sig; its kind comes from its path in the tree
       (builds/4312.json, tags.json, mirrors.json, keys/3.json), or --name. --keys is the
       keys document in force: one of its online keys must have signed, and it must not have
       expired. --key trusts the keys given instead (for example, to check that a root key
       has signed a keys document before it goes to the next root key).

Exits non-zero on the first problem. --now (YYYY-MM-DDTHH:MM:SSZ) replaces the current time.
Needs the openssl CLI. updater/validate.py checks whole trees.
"""

import argparse
import datetime
import re
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("verify")


def parse_now(text):
    if text is None:
        return datetime.datetime.now(datetime.timezone.utc)
    return formats._time(text, "--now")


def read_keys_document(path):
    try:
        return formats.check_keys(formats.parse_json(Path(path).read_bytes(), formats.MAX_DOCUMENT))
    except formats.Invalid as e:
        raise formats.Invalid("%s: %s" % (path, e))


def keys(args):
    now = parse_now(args.now)
    documents = []
    for file in args.files:
        m = re.fullmatch(r"([1-9][0-9]*)\.json", Path(file).name)
        if not m:
            raise formats.Invalid("%s: the file name must be <version>.json" % file)
        documents.append((int(m.group(1)), Path(file).read_bytes(), Path(file + ".sig").read_bytes()))
    if args.known:
        known, roots = read_keys_document(args.known), None
        start = "known version %d" % known["version"]
    else:
        known, roots = None, [formats.read_public_key(k) for k in args.roots]
        start = "%d pinned root keys" % len(roots)
    newest = formats.verify_keys_chain(known, documents, now, roots)
    if formats.expires(newest) - now < formats.EXPIRY_WARNING:
        log("warning: keys/%d.json expires at %s" % (newest["version"], newest["expires"]))
    log("valid: keys version %d from %s, expires %s, online keys %s"
        % (newest["version"], start, newest["expires"], " ".join(newest["online_keys"])))


def file(args):
    path = Path(args.file)
    if args.name:
        name = args.name
    elif path.parent.name in ("builds", "keys"):
        name = "%s/%s" % (path.parent.name, path.name)
    else:
        name = path.name
    kind, _ = formats.kind_of(name)
    if args.keys:
        keys_doc = read_keys_document(args.keys)
        if kind == "keys":
            raise formats.Invalid("%s is a keys document: use `verify.py keys`, or --key" % name)
        if formats.expires(keys_doc) <= parse_now(args.now):
            raise formats.Invalid("%s expired at %s" % (args.keys, keys_doc["expires"]))
        trusted = set(keys_doc["online_keys"])
    else:
        trusted = {formats.read_public_key(k) for k in args.key}
    data = path.read_bytes()
    try:
        if len(data) > (formats.MAX_MANIFEST if kind == "build" else formats.MAX_DOCUMENT):
            raise formats.Invalid("too large")
        formats.verify(data, Path(str(path) + ".sig").read_bytes(), trusted)
        formats.check_document(name, data)
    except formats.Invalid as e:
        raise formats.Invalid("%s: %s" % (name, e))
    log("valid: %s as %s" % (path, name))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    k = commands.add_parser("keys")
    k.add_argument("files", nargs="+", metavar="<N>.json")
    start = k.add_mutually_exclusive_group(required=True)
    start.add_argument("--roots", nargs="+", metavar="KEY", help="the pinned root keys (hex or .pub files)")
    start.add_argument("--known", help="the keys document trusted as it is")
    k.add_argument("--now")
    f = commands.add_parser("file")
    f.add_argument("file")
    f.add_argument("--name", help="its path in the tree, if <file> isn't there")
    trust = f.add_mutually_exclusive_group(required=True)
    trust.add_argument("--keys", help="the keys document in force")
    trust.add_argument("--key", nargs="+", metavar="KEY", help="keys to trust (hex or .pub files)")
    f.add_argument("--now")
    args = parser.parse_args()
    try:
        {"keys": keys, "file": file}[args.command](args)
    except formats.Invalid as e:
        common.die(str(e))
    except OSError as e:
        common.die(str(e))


if __name__ == "__main__":
    main()
