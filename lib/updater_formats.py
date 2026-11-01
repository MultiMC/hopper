"""The new updater's formats (format 1), as specified in the Design repo's UpdaterFormats.md:
checking documents, signature files and whole trees, and signing with openssl.

Everything here fails closed: a problem raises Invalid with a message saying where it is.
Signatures are made and checked with the openssl CLI (Python's standard library has no
Ed25519).
"""

import datetime
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

FORMAT = 1

MAX_DOCUMENT = 64 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
MAX_SIGNATURE_LINES = 8
MAX_INT = 2 ** 53 - 1
MAX_MIRRORS = 16
MAX_KEYS = 4
MAX_PATH = 512
MAX_VARIANT_ID = 64
# Following links further than this counts as a loop
MAX_LINK_DEPTH = 40
EXPIRY_WARNING = datetime.timedelta(days=90)

HEX64 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
VERSION = re.compile(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*")
VARIANT_ID = re.compile(r"(windows|macos|linux)-(x86_64|x86|arm64)-[a-z0-9.]+(-[a-z0-9.]+)*")
SIGNATURE_LINE = re.compile(r"([0-9a-f]{64}) ([0-9a-f]{128})\n")
# Printable ASCII without what Windows doesn't allow in names
PATH_CHARS = re.compile(r"[\x20-\x7e]+")
PATH_FORBIDDEN = set('\\:*?"<>|')
BUILD_NAME = re.compile(r"builds/([1-9][0-9]*)\.json")
KEYS_NAME = re.compile(r"keys/([1-9][0-9]*)\.json")
CONTENT_NAME = re.compile(r"files/([0-9a-f]{2})/([0-9a-f]{64})")

OS_FIELDS = {
    "windows": {"os", "arch", "os_version"},
    "macos": {"os", "arch", "os_version"},
    "linux": {"os", "arch", "glibc", "glibcxx", "qt"},
}

# The DER prefix of an Ed25519 SubjectPublicKeyInfo; the raw 32-byte key follows it
ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")


class Invalid(Exception):
    pass


# --- JSON ---------------------------------------------------------------------------------

def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise Invalid("duplicate key %r" % key)
        obj[key] = value
    return obj


def _no_float(text):
    raise Invalid("number %s isn't an integer" % text)


def _no_constant(text):
    raise Invalid("%s isn't allowed" % text)


def parse_json(data, limit):
    """The bytes of a document as a dict: UTF-8 without a BOM, one object, no duplicate keys,
    no fractions or exponents, no NaN or Infinity."""
    if len(data) > limit:
        raise Invalid("%d bytes, more than %d" % (len(data), limit))
    if data.startswith(b"\xef\xbb\xbf"):
        raise Invalid("starts with a byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Invalid("not UTF-8: %s" % e)
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_no_float,
                         parse_constant=_no_constant)
    except json.JSONDecodeError as e:
        raise Invalid("not JSON: %s" % e)
    if not isinstance(obj, dict):
        raise Invalid("not a JSON object")
    return obj


def dump_json(obj):
    """A document's bytes, as the publisher writes them."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


# --- values -------------------------------------------------------------------------------

def _fields(obj, where, names):
    if not isinstance(obj, dict):
        raise Invalid("%s: not an object" % where)
    missing = sorted(set(names) - obj.keys())
    if missing:
        raise Invalid("%s: missing %s" % (where, ", ".join(missing)))
    unknown = sorted(obj.keys() - set(names))
    if unknown:
        raise Invalid("%s: unknown %s" % (where, ", ".join(unknown)))


def _integer(value, where, minimum=0):
    # bool is a subclass of int in Python; JSON's true isn't a number
    if type(value) is not int:
        raise Invalid("%s: not an integer" % where)
    if not minimum <= value <= MAX_INT:
        raise Invalid("%s: %d is outside %d to %d" % (where, value, minimum, MAX_INT))
    return value


def _string(value, where, pattern=None):
    if not isinstance(value, str):
        raise Invalid("%s: not a string" % where)
    if pattern and not pattern.fullmatch(value):
        raise Invalid("%s: %r isn't valid" % (where, value))
    return value


def _list(value, where, minimum, maximum):
    if not isinstance(value, list):
        raise Invalid("%s: not a list" % where)
    if not minimum <= len(value) <= maximum:
        raise Invalid("%s: %d entries, allowed %d to %d" % (where, len(value), minimum, maximum))
    return value


def _time(value, where):
    _string(value, where, TIME)
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise Invalid("%s: %r isn't a valid time" % (where, value))


def _keys(value, where):
    _list(value, where, 1, MAX_KEYS)
    for i, key in enumerate(value):
        _string(key, "%s[%d]" % (where, i), HEX64)
    if len(set(value)) != len(value):
        raise Invalid("%s: a key is listed twice" % where)
    return value


def _header(doc, kind, names):
    # What it is and which format first, so a wrong document says so rather than what it lacks
    if doc.get("type") != kind:
        raise Invalid("type is %r, expected %r" % (doc.get("type"), kind))
    if type(doc.get("format")) is not int or doc["format"] != FORMAT:
        raise Invalid("format %r isn't supported (only %d)" % (doc.get("format"), FORMAT))
    _fields(doc, kind, {"type", "format"} | names)


# --- documents ----------------------------------------------------------------------------

def check_keys(doc, version=None):
    _header(doc, "keys", {"version", "expires", "root_keys", "online_keys"})
    _integer(doc["version"], "version", 1)
    if version is not None and doc["version"] != version:
        raise Invalid("version is %d, but the file is for version %d" % (doc["version"], version))
    _time(doc["expires"], "expires")
    _keys(doc["root_keys"], "root_keys")
    _keys(doc["online_keys"], "online_keys")
    if set(doc["root_keys"]) & set(doc["online_keys"]):
        raise Invalid("a key is both a root key and an online key")
    return doc


def expires(keys_doc):
    return _time(keys_doc["expires"], "expires")


def check_tags(doc):
    _header(doc, "tags", {"serial", "issued", "tags", "withdrawn"})
    _integer(doc["serial"], "serial", 1)
    _time(doc["issued"], "issued")
    tags = doc["tags"]
    _fields(tags, "tags", {"latest", "testing", "stable"})
    for name in ("latest", "testing", "stable"):
        _integer(tags[name], "tags.%s" % name, 1)
    if not tags["stable"] <= tags["testing"] <= tags["latest"]:
        raise Invalid("tags: need stable <= testing <= latest, have %d, %d, %d"
                      % (tags["stable"], tags["testing"], tags["latest"]))
    withdrawn = _list(doc["withdrawn"], "withdrawn", 0, MAX_INT)
    for i, build in enumerate(withdrawn):
        _integer(build, "withdrawn[%d]" % i, 1)
    if any(a >= b for a, b in zip(withdrawn, withdrawn[1:])):
        raise Invalid("withdrawn: not ascending, or a build is listed twice")
    tagged = set(withdrawn) & set(tags.values())
    if tagged:
        raise Invalid("withdrawn: build %d is tagged" % min(tagged))
    return doc


def _mirror_url(url, where):
    _string(url, where)
    if not re.fullmatch(r"https://[\x21-\x7e]+/", url) or set("@?#\\") & set(url):
        raise Invalid("%s: %r isn't an https:// base URL ending in / without user info, query or fragment" % (where, url))
    host = url[len("https://"):].split("/", 1)[0]
    if not host:
        raise Invalid("%s: %r has no host" % (where, url))


def check_mirrors(doc):
    _header(doc, "mirrors", {"issued", "mirrors"})
    _time(doc["issued"], "issued")
    groups = _list(doc["mirrors"], "mirrors", 1, MAX_MIRRORS)
    seen = set()
    for g, group in enumerate(groups):
        _list(group, "mirrors[%d]" % g, 1, MAX_MIRRORS)
        for i, url in enumerate(group):
            where = "mirrors[%d][%d]" % (g, i)
            _mirror_url(url, where)
            if url in seen:
                raise Invalid("%s: %r is listed twice" % (where, url))
            seen.add(url)
    if len(seen) > MAX_MIRRORS:
        raise Invalid("mirrors: %d URLs, at most %d" % (len(seen), MAX_MIRRORS))
    return doc


def _path(value, where, dotdot=False):
    _string(value, where)
    if not PATH_CHARS.fullmatch(value) or PATH_FORBIDDEN & set(value):
        raise Invalid("%s: %r has characters that aren't allowed" % (where, value))
    if len(value) > MAX_PATH:
        raise Invalid("%s: longer than %d characters" % (where, MAX_PATH))
    parts = value.split("/")
    for part in parts:
        if part in ("", ".") or (part == ".." and not dotdot):
            raise Invalid("%s: %r has an empty, . or .. part, or a leading or trailing /" % (where, value))
    return parts


def _requirements(req, where):
    if not isinstance(req, dict) or req.get("os") not in OS_FIELDS:
        raise Invalid("%s.os: must be one of %s" % (where, ", ".join(sorted(OS_FIELDS))))
    _fields(req, where, OS_FIELDS[req["os"]])
    if req["arch"] not in ("x86", "x86_64", "arm64"):
        raise Invalid("%s.arch: %r isn't x86, x86_64 or arm64" % (where, req["arch"]))
    for name in sorted(OS_FIELDS[req["os"]] - {"os", "arch"}):
        _string(req[name], "%s.%s" % (where, name), VERSION)


def _resolve(link_path, links, where):
    """Where a link points, followed through other links: the parts of a path inside the
    installation."""
    current = link_path.split("/")[:-1]
    pending = links[link_path].split("/")
    depth = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            if not current:
                raise Invalid("%s: points outside the installation" % where)
            current.pop()
            continue
        current.append(part)
        here = "/".join(current)
        if here in links:
            depth += 1
            if depth > MAX_LINK_DEPTH:
                raise Invalid("%s: following the links loops" % where)
            current = current[:-1]
            pending = links[here].split("/") + pending
    return "/".join(current)


def _check_variant(variant_id, variant, where):
    _fields(variant, where, {"supersedes", "requires", "files", "links"})
    _requirements(variant["requires"], "%s.requires" % where)
    m = VARIANT_ID.fullmatch(variant_id)
    if m.group(1) != variant["requires"]["os"] or m.group(2) != variant["requires"]["arch"]:
        raise Invalid("%s: the ID doesn't match requires (os %s, arch %s)"
                      % (where, variant["requires"]["os"], variant["requires"]["arch"]))

    supersedes = _list(variant["supersedes"], "%s.supersedes" % where, 0, MAX_INT)
    for i, other in enumerate(supersedes):
        _string(other, "%s.supersedes[%d]" % (where, i), VARIANT_ID)
        if len(other) > MAX_VARIANT_ID:
            raise Invalid("%s.supersedes[%d]: longer than %d characters" % (where, i, MAX_VARIANT_ID))
    if variant_id in supersedes:
        raise Invalid("%s: supersedes itself" % where)
    if len(set(supersedes)) != len(supersedes):
        raise Invalid("%s.supersedes: a variant is listed twice" % where)

    files = _list(variant["files"], "%s.files" % where, 1, MAX_INT)
    paths = []
    for i, entry in enumerate(files):
        w = "%s.files[%d]" % (where, i)
        _fields(entry, w, {"path", "sha256", "size", "executable"})
        _path(entry["path"], w + ".path")
        _string(entry["sha256"], w + ".sha256", HEX64)
        _integer(entry["size"], w + ".size")
        if type(entry["executable"]) is not bool:
            raise Invalid("%s.executable: not true or false" % w)
        paths.append(entry["path"])

    links_list = _list(variant["links"], "%s.links" % where, 0, MAX_INT)
    if links_list and variant["requires"]["os"] == "windows":
        raise Invalid("%s: a Windows variant has no links" % where)
    links = {}
    for i, entry in enumerate(links_list):
        w = "%s.links[%d]" % (where, i)
        _fields(entry, w, {"path", "target"})
        _path(entry["path"], w + ".path")
        _path(entry["target"], w + ".target", dotdot=True)
        links[entry["path"]] = entry["target"]

    for name, listed in (("files", paths), ("links", list(links))):
        if listed != sorted(listed):
            raise Invalid("%s.%s: not sorted by path" % (where, name))

    # One spelling per path and directory, compared case-insensitively; nothing is both a
    # file or link and a directory (so no path goes through a link)
    entries = paths + list(links)
    spelling = {}
    directories = set()
    for path in entries:
        parts = path.split("/")
        for n in range(1, len(parts) + 1):
            prefix = "/".join(parts[:n])
            if n < len(parts):
                directories.add(prefix)
            known = spelling.setdefault(prefix.lower(), prefix)
            if known != prefix:
                raise Invalid("%s: %r and %r differ only in case" % (where, known, prefix))
    if len(set(entries)) != len(entries):
        raise Invalid("%s: a path is listed twice" % where)
    clash = sorted(directories & set(entries))
    if clash:
        raise Invalid("%s: %r is listed, but other paths go through it" % (where, clash[0]))

    file_set = set(paths)
    for path in links:
        target = _resolve(path, links, "%s: link %r" % (where, path))
        if target not in file_set and target not in directories:
            raise Invalid("%s: link %r points to %r, which isn't a listed file or a directory with files"
                          % (where, path, target))


def check_build(doc, build=None):
    _header(doc, "build", {"build", "commit", "built", "variants"})
    _integer(doc["build"], "build", 1)
    if build is not None and doc["build"] != build:
        raise Invalid("build is %d, but the file is for build %d" % (doc["build"], build))
    _string(doc["commit"], "commit", COMMIT)
    _time(doc["built"], "built")
    variants = doc["variants"]
    if not isinstance(variants, dict) or not variants:
        raise Invalid("variants: not an object with at least one variant")
    for variant_id, variant in variants.items():
        if not VARIANT_ID.fullmatch(variant_id) or len(variant_id) > MAX_VARIANT_ID:
            raise Invalid("variants: %r isn't a valid variant ID" % variant_id)
        _check_variant(variant_id, variant, "variants.%s" % variant_id)

    # A single chain of successors from every variant: each superseded at most once, no cycles
    successor = {}
    for variant_id, variant in variants.items():
        for other in variant["supersedes"]:
            if other in successor:
                raise Invalid("variants: %s is superseded by both %s and %s" % (other, successor[other], variant_id))
            successor[other] = variant_id
    for start in successor:
        seen = {start}
        current = start
        while current in successor:
            current = successor[current]
            if current in seen:
                raise Invalid("variants: the supersedes chain from %s loops" % start)
            seen.add(current)
    return doc


def content_files(build_doc):
    """{sha256: size} of every content file a manifest lists."""
    files = {}
    for variant in build_doc["variants"].values():
        for entry in variant["files"]:
            if files.setdefault(entry["sha256"], entry["size"]) != entry["size"]:
                raise Invalid("content %s is listed with two sizes" % entry["sha256"])
    return files


def content_path(sha256):
    return "files/%s/%s" % (sha256[:2], sha256)


KINDS = {"tags.json": "tags", "mirrors.json": "mirrors"}


def kind_of(name):
    """What a file in the tree is, from its path relative to the root: (kind, number)."""
    if name in KINDS:
        return KINDS[name], None
    for pattern, kind in ((BUILD_NAME, "build"), (KEYS_NAME, "keys")):
        m = pattern.fullmatch(name)
        if m:
            return kind, int(m.group(1))
    raise Invalid("%s isn't a document of the tree" % name)


def check_document(name, data):
    """The bytes of the document at `name` (relative to the root), checked for its kind."""
    kind, number = kind_of(name)
    doc = parse_json(data, MAX_MANIFEST if kind == "build" else MAX_DOCUMENT)
    if kind == "tags":
        return check_tags(doc)
    if kind == "mirrors":
        return check_mirrors(doc)
    if kind == "keys":
        return check_keys(doc, number)
    return check_build(doc, number)


# --- signatures ---------------------------------------------------------------------------

def parse_signatures(data):
    """A signature file's lines as [(public key, signature)], both hex."""
    if len(data) > MAX_SIGNATURE_LINES * 194:
        raise Invalid("signature file: too large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Invalid("signature file: not ASCII")
    lines = text.splitlines(keepends=True)
    if not 1 <= len(lines) <= MAX_SIGNATURE_LINES:
        raise Invalid("signature file: %d lines, allowed 1 to %d" % (len(lines), MAX_SIGNATURE_LINES))
    signatures = []
    for i, line in enumerate(lines, 1):
        m = SIGNATURE_LINE.fullmatch(line)
        if not m:
            raise Invalid("signature file: line %d isn't <64 hex digits> <128 hex digits>" % i)
        signatures.append((m.group(1), m.group(2)))
    if len({key for key, _ in signatures}) != len(signatures):
        raise Invalid("signature file: a key is listed twice")
    return signatures


def _openssl(*args):
    try:
        return subprocess.run(["openssl", *args], capture_output=True)
    except FileNotFoundError:
        raise Invalid("openssl isn't installed")


def verify_one(data, key, signature):
    """Whether `signature` (hex) is key's (hex) Ed25519 signature over data."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "key.der").write_bytes(ED25519_SPKI + bytes.fromhex(key))
        (tmp / "data").write_bytes(data)
        (tmp / "sig").write_bytes(bytes.fromhex(signature))
        result = _openssl("pkeyutl", "-verify", "-pubin", "-keyform", "DER", "-inkey", str(tmp / "key.der"),
                          "-rawin", "-in", str(tmp / "data"), "-sigfile", str(tmp / "sig"))
        return result.returncode == 0


def verify(data, signature_file, trusted):
    """Check the signature file for data against the trusted keys (hex): at least one line
    by a trusted key, and every such line verifies. Lines by other keys are ignored."""
    lines = [(key, sig) for key, sig in parse_signatures(signature_file) if key in trusted]
    if not lines:
        raise Invalid("no signature by a trusted key")
    for key, sig in lines:
        if not verify_one(data, key, sig):
            raise Invalid("the signature by %s doesn't verify" % key)


def public_key(private_key):
    """The raw public key (hex) of an Ed25519 private key file (PEM, as openssl genpkey writes it)."""
    result = _openssl("pkey", "-in", str(private_key), "-pubout", "-outform", "DER")
    if result.returncode != 0 or len(result.stdout) != len(ED25519_SPKI) + 32 or not result.stdout.startswith(ED25519_SPKI):
        raise Invalid("%s isn't an Ed25519 private key: %s" % (private_key, result.stderr.decode(errors="replace").strip()))
    return result.stdout[len(ED25519_SPKI):].hex()


def sign(data, private_key):
    """A signature file's line for data, signed with the private key file."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "data").write_bytes(data)
        result = _openssl("pkeyutl", "-sign", "-inkey", str(private_key), "-rawin", "-in", str(Path(tmp) / "data"))
    if result.returncode != 0 or len(result.stdout) != 64:
        raise Invalid("signing with %s failed: %s" % (private_key, result.stderr.decode(errors="replace").strip()))
    return "%s %s\n" % (public_key(private_key), result.stdout.hex())


# --- the tree -----------------------------------------------------------------------------

def trusted_keys(name, doc, keys_doc):
    """The keys trusted for the document at `name`, given the keys document before it (for a
    keys document) or the one in force (for everything else)."""
    kind, number = kind_of(name)
    if kind == "keys":
        if number == 1:
            return set(doc["root_keys"])
        if keys_doc is None or keys_doc["version"] != number - 1:
            raise Invalid("%s is checked against keys version %d" % (name, number - 1))
        return set(keys_doc["root_keys"])
    return set(keys_doc["online_keys"])


def _read(root, name):
    try:
        return (root / name).read_bytes()
    except FileNotFoundError:
        raise Invalid("%s is missing" % name)


def _signed(root, name, keys_doc):
    data = _read(root, name)
    signature = _read(root, name + ".sig")
    try:
        if len(data) > (MAX_MANIFEST if kind_of(name)[0] == "build" else MAX_DOCUMENT):
            raise Invalid("too large")
        # The signature first, then the JSON; a keys document names its own trusted keys
        # only for version 1, which is then checked against them
        if kind_of(name) == ("keys", 1):
            doc = check_document(name, data)
            verify(data, signature, trusted_keys(name, doc, keys_doc))
            return doc
        verify(data, signature, trusted_keys(name, None, keys_doc))
        return check_document(name, data)
    except Invalid as e:
        raise Invalid("%s: %s" % (name, e))


def check_tree(root, floor, now=None, hashes=True):
    """Check the tree at root against the keys document a Launcher would embed (its bytes).
    Returns (summary, warnings); raises Invalid on the first problem."""
    root = Path(root)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    warnings = []
    try:
        floor_doc = check_keys(parse_json(floor, MAX_DOCUMENT))
    except Invalid as e:
        raise Invalid("floor keys document: %s" % e)

    # Nothing but the layout
    names = sorted(str(p.relative_to(root)) for p in root.rglob("*") if not p.is_dir() or p.is_symlink())
    documents, signatures, contents = set(), set(), {}
    for name in names:
        if (root / name).is_symlink():
            raise Invalid("%s is a symbolic link" % name)
        m = CONTENT_NAME.fullmatch(name)
        if m:
            if m.group(1) != m.group(2)[:2]:
                raise Invalid("%s is in the wrong directory" % name)
            contents[m.group(2)] = name
        elif name.endswith(".sig"):
            signatures.add(name[:-4])
        else:
            kind_of(name)
            documents.add(name)
    for name in sorted(signatures - documents):
        raise Invalid("%s.sig has no document" % name)
    for name in sorted(documents - signatures):
        raise Invalid("%s has no signature" % name)

    # The keys chain, from version 1
    versions = sorted(kind_of(n)[1] for n in documents if n.startswith("keys/"))
    if not versions:
        raise Invalid("there are no keys documents")
    if versions != list(range(1, len(versions) + 1)):
        raise Invalid("the keys documents aren't numbered 1 to %d without gaps" % versions[-1])
    keys_doc = None
    for version in versions:
        keys_doc = _signed(root, "keys/%d.json" % version, keys_doc)
        if version == floor_doc["version"] and _read(root, "keys/%d.json" % version) != floor:
            raise Invalid("keys/%d.json isn't the same as the floor keys document" % version)
    if floor_doc["version"] > keys_doc["version"]:
        raise Invalid("the floor is keys version %d, but the tree only has up to %d" % (floor_doc["version"], keys_doc["version"]))
    if expires(keys_doc) <= now:
        raise Invalid("keys/%d.json expired at %s" % (keys_doc["version"], keys_doc["expires"]))
    if expires(keys_doc) - now < EXPIRY_WARNING:
        warnings.append("keys/%d.json expires at %s" % (keys_doc["version"], keys_doc["expires"]))

    tags = _signed(root, "tags.json", keys_doc)
    mirrors = _signed(root, "mirrors.json", keys_doc)
    del mirrors

    builds = {}
    for name in sorted(n for n in documents if n.startswith("builds/")):
        builds[kind_of(name)[1]] = _signed(root, name, keys_doc)
    if not builds:
        raise Invalid("there are no builds")

    for tag, build in sorted(tags["tags"].items()):
        if build not in builds:
            raise Invalid("tags.json: %s is build %d, which isn't in builds/" % (tag, build))
    for build in tags["withdrawn"]:
        if build in builds:
            raise Invalid("tags.json: build %d is withdrawn, but builds/%d.json is there" % (build, build))
    if tags["tags"]["latest"] != max(builds):
        raise Invalid("tags.json: latest is build %d, but the highest build is %d" % (tags["tags"]["latest"], max(builds)))

    listed = {}
    for build, doc in sorted(builds.items()):
        for sha256, size in content_files(doc).items():
            if listed.setdefault(sha256, size) != size:
                raise Invalid("builds/%d.json: content %s has a different size in another build" % (build, sha256))
    for sha256, size in sorted(listed.items()):
        name = content_path(sha256)
        if sha256 not in contents:
            raise Invalid("%s is missing" % name)
        path = root / name
        if path.stat().st_size != size:
            raise Invalid("%s is %d bytes, the manifests say %d" % (name, path.stat().st_size, size))
        if hashes:
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha256:
                raise Invalid("%s has SHA-256 %s" % (name, digest.hexdigest()))
    unlisted = sorted(set(contents) - set(listed))
    for sha256 in unlisted:
        warnings.append("%s isn't listed by any manifest" % content_path(sha256))

    summary = ("keys version %d, tags serial %d (latest %d, testing %d, stable %d), %d builds, %d content files"
               % (keys_doc["version"], tags["serial"], tags["tags"]["latest"], tags["tags"]["testing"],
                  tags["tags"]["stable"], len(builds), len(listed)))
    return summary, warnings
