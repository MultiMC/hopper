#!/usr/bin/env python3
"""Sign a document of the new updater's tree with one or more private keys.

    keys/sign.py <file> <private key>... [--name PATH]

Checks the document against the format first; its kind comes from its path in the tree
(keys/3.json, builds/4312.json, tags.json, mirrors.json), or --name for a file elsewhere.
Then adds one line per key to the detached signature file <file>.sig, creating it if needed.
A key that already has a line is skipped. If <file>.sig has a line that doesn't verify (the
document changed after it was signed), nothing is written: remove the old .sig first.

This doesn't decide which keys a document needs: a keys document is signed with root keys
(version 1 its own, later versions those of the version before), everything else with an
online key. keys/verify.py checks that. Several root keys can sign one at a time, in
different places: carry the document and its .sig between them.

The private keys are PEM files as make-key.py writes them; openssl asks for a passphrase if
one is encrypted. Needs the openssl CLI.
"""

import argparse
import os
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("sign")


def document_name(path, name):
    if name:
        return name
    if path.parent.name in ("builds", "keys"):
        return "%s/%s" % (path.parent.name, path.name)
    return path.name


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file")
    parser.add_argument("keys", nargs="+", metavar="private key")
    parser.add_argument("--name", help="its path in the tree, if <file> isn't there")
    args = parser.parse_args()

    path = Path(args.file)
    signature_path = Path(str(path) + ".sig")
    name = document_name(path, args.name)
    try:
        data = path.read_bytes()
        try:
            formats.check_document(name, data)
        except formats.Invalid as e:
            raise formats.Invalid("%s: %s (--name gives its path in the tree)" % (name, e))

        lines = []
        if signature_path.exists():
            lines = formats.parse_signatures(signature_path.read_bytes())
            for key, signature in lines:
                if not formats.verify_one(data, key, signature):
                    raise formats.Invalid("%s: the signature by %s doesn't verify; the document changed after it was signed?"
                                          % (signature_path, key))
        added = 0
        for private in args.keys:
            key = formats.public_key(private)
            if key in (k for k, _ in lines):
                log("%s has a signature by %s already" % (signature_path, key))
                continue
            line = formats.sign(data, private)
            ((_, signature),) = formats.parse_signatures(line.encode("ascii"))
            if not formats.verify_one(data, key, signature):
                raise formats.Invalid("the signature just made with %s doesn't verify" % private)
            lines.append((key, signature))
            added += 1
            log("signed %s with %s" % (path, key))
        if not added:
            return
        text = "".join("%s %s\n" % line for line in lines).encode("ascii")
        formats.parse_signatures(text)

        temporary = Path(str(signature_path) + ".tmp")
        temporary.write_bytes(text)
        os.replace(temporary, signature_path)
    except formats.Invalid as e:
        common.die(str(e))
    except OSError as e:
        common.die(str(e))
    log("wrote %s: %d signatures" % (signature_path, len(lines)))


if __name__ == "__main__":
    main()
