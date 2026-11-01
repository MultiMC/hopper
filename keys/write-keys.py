#!/usr/bin/env python3
"""Write a keys document for the new updater (UpdaterFormats.md in the Design repo).

    keys/write-keys.py <dir>/<version>.json --expires TIME --root KEY... --online KEY...
                       [--previous FILE]

Writes the document, unsigned, as the publisher writes documents, and checks it against the
format. Each KEY is 64 hex digits or a .pub file from make-key.py; give --root and --online
once per key. TIME is YYYY-MM-DDTHH:MM:SSZ, normally about two years ahead. The version is
the number in the file name. --previous is the keys document before it: the version must
then be one higher. Refuses to overwrite a file.

Sign it with keys/sign.py: version 1 with its own root keys, any later version with the
root keys of the version before it.
"""

import argparse
import os
import re
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("write-keys")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", help="<dir>/<version>.json")
    parser.add_argument("--expires", required=True)
    parser.add_argument("--root", action="append", required=True, help="a root key (hex or .pub file)")
    parser.add_argument("--online", action="append", required=True, help="an online key (hex or .pub file)")
    parser.add_argument("--previous", help="the keys document before this one")
    args = parser.parse_args()

    out = Path(args.out)
    m = re.fullmatch(r"([1-9][0-9]*)\.json", out.name)
    if not m:
        common.die("%s: the file name must be <version>.json" % out)
    version = int(m.group(1))
    try:
        if args.previous:
            previous = formats.check_keys(formats.parse_json(Path(args.previous).read_bytes(), formats.MAX_DOCUMENT))
            if version != previous["version"] + 1:
                common.die("%s is version %d, so this one must be %d" % (args.previous, previous["version"], previous["version"] + 1))
        doc = {
            "type": "keys",
            "format": formats.FORMAT,
            "version": version,
            "expires": args.expires,
            "root_keys": [formats.read_public_key(k) for k in args.root],
            "online_keys": [formats.read_public_key(k) for k in args.online],
        }
        data = formats.dump_json(doc)
        formats.check_document("keys/%d.json" % version, data)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except formats.Invalid as e:
        common.die(str(e))
    except OSError as e:
        common.die(str(e))
    log("wrote %s: version %d, expires %s, %d root keys, %d online keys"
        % (out, version, doc["expires"], len(doc["root_keys"]), len(doc["online_keys"])))


if __name__ == "__main__":
    main()
