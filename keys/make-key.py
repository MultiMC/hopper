#!/usr/bin/env python3
"""Make an Ed25519 key pair for the new updater (a root key or an online key).

    keys/make-key.py <name>

Writes <name>.pem, the private key as openssl genpkey writes it (readable only by you), and
<name>.pub, the public key as 64 hex digits on one line, the way the keys document and the
signature files write it. Refuses to overwrite either. The private key is not encrypted:
encrypt it for storage (age -p) as the design describes, and decrypt it only to sign.

Needs the openssl CLI. Meant to run on the offline machine for root keys.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# The shared code is in lib/, next to this directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import common
import updater_formats as formats

log = common.logger("make-key")


def write_new(path, data, mode):
    """Write a file that must not exist yet, with mode set from the start."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="the files to write, without .pem and .pub")
    args = parser.parse_args()
    private = Path(args.name + ".pem")
    public = Path(args.name + ".pub")
    for path in (private, public):
        if path.exists() or path.is_symlink():
            common.die("%s exists already" % path)

    try:
        result = subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519"], capture_output=True)
    except FileNotFoundError:
        common.die("openssl isn't installed")
    if result.returncode != 0 or not result.stdout.startswith(b"-----BEGIN PRIVATE KEY-----"):
        common.die("openssl genpkey failed: %s" % result.stderr.decode(errors="replace").strip())
    try:
        write_new(private, result.stdout, 0o600)
        key = formats.public_key(private)
        write_new(public, (key + "\n").encode("ascii"), 0o644)
    except (OSError, formats.Invalid) as e:
        common.die(str(e))
    log("wrote %s and %s" % (private, public))
    log("public key: %s" % key)


if __name__ == "__main__":
    main()
