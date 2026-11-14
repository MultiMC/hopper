"""Ed25519 keys in a YubiKey's PIV slots, for keys/sign.py: the root keys of the new updater
can live on YubiKeys (the Design repo's Updater.md, Open questions, The root keys).

A key on a YubiKey is named `yubikey:<slot>` (9c, the signature slot, or another PIV slot
in hex), or `yubikey:<serial>:<slot>` to pick one of several plugged-in YubiKeys. The
private key never leaves the device; signing needs its PIN, which the caller asks for, and
a touch when the slot's touch policy says so (the root keys are imported with
`--touch-policy always`): this asks for it while the YubiKey waits.

Talks to the YubiKey with ykman's Python library (yubikit, part of yubikey-manager),
imported only when a YubiKey is used, so nothing else in hopper needs it. It doesn't go
through openssl: its PKCS#11 provider crashed on OpenSSL 4.1.0 (2026-11-10).
"""

import contextlib
import re
import sys

import updater_formats as formats

SPEC = re.compile(r"yubikey:(?:([0-9]+):)?([0-9a-fA-F]{2})")


def is_key(arg):
    """Whether a key argument names a YubiKey slot rather than a file."""
    return str(arg).startswith("yubikey:")


def _parse(arg):
    match = SPEC.fullmatch(str(arg))
    if not match:
        raise formats.Invalid("%s isn't a YubiKey key: yubikey:<slot> or yubikey:<serial>:<slot>, the slot in hex (9c)"
                              % arg)
    serial, slot = match.groups()
    return (int(serial) if serial else None), int(slot, 16)


def _yubikit():
    try:
        from ykman.device import list_all_devices
        from yubikit.core.smartcard import SmartCardConnection
        from yubikit import piv
    except ImportError as e:
        raise formats.Invalid("signing with a YubiKey needs ykman's Python library (yubikey-manager): %s" % e)
    return list_all_devices, SmartCardConnection, piv


@contextlib.contextmanager
def _slot(arg):
    """The PIV session and slot a key argument names, checked to hold an Ed25519 key."""
    serial, number = _parse(arg)
    list_all_devices, SmartCardConnection, piv = _yubikit()
    try:
        slot = piv.SLOT(number)
    except ValueError:
        raise formats.Invalid("%s: %02x isn't a PIV slot" % (arg, number))
    devices = [(device, info) for device, info in list_all_devices() if serial is None or info.serial == serial]
    if not devices:
        raise formats.Invalid("%s: no such YubiKey plugged in" % arg)
    if len(devices) > 1:
        raise formats.Invalid("%s: several YubiKeys plugged in (serials %s); name one: yubikey:<serial>:<slot>"
                              % (arg, ", ".join(str(info.serial) for _, info in devices)))
    device, _ = devices[0]
    with device.open_connection(SmartCardConnection) as connection:
        session = piv.PivSession(connection)
        try:
            metadata = session.get_slot_metadata(slot)
        except Exception as e:
            raise formats.Invalid("%s: no key in the slot (%s)" % (arg, e))
        if metadata.key_type != piv.KEY_TYPE.ED25519:
            raise formats.Invalid("%s: the key in the slot is %s, not Ed25519" % (arg, metadata.key_type.name))
        yield session, slot, metadata


def needs_touch(policy):
    """Whether a slot's touch policy (yubikit's TOUCH_POLICY name) asks for a touch to sign:
    ALWAYS, or CACHED (a touch then counts for 15 seconds)."""
    return policy in ("ALWAYS", "CACHED")


def _raw(public_key):
    from cryptography.hazmat.primitives import serialization
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key(arg):
    """The public key (hex) of the YubiKey slot; needs no PIN."""
    with _slot(arg) as (_, _, metadata):
        return _raw(metadata.public_key).hex()


def sign(data, arg, pin):
    """A signature file's line for data, signed in the YubiKey slot with the PIN given. A
    wrong PIN stops with the tries left; it isn't retried."""
    piv = _yubikit()[2]
    with _slot(arg) as (session, slot, metadata):
        try:
            session.verify_pin(pin)
        except piv.InvalidPinError as e:
            raise formats.Invalid("%s: wrong PIN, %d tries left" % (arg, e.attempts_remaining))
        touch = needs_touch(metadata.touch_policy.name)
        if touch:
            # the YubiKey waits about 15 seconds for a finger on its gold contact, blinking
            sys.stderr.write("touch the YubiKey (its light blinks)\n")
            sys.stderr.flush()
        try:
            # Ed25519 signs the message itself; yubikit ignores the hash for it
            signature = session.sign(slot, piv.KEY_TYPE.ED25519, data, None)
        except Exception as e:
            if touch:
                raise formats.Invalid("%s: not signed; the YubiKey wasn't touched in time? (%s)" % (arg, e))
            raise
        return "%s %s\n" % (_raw(metadata.public_key).hex(), signature.hex())
