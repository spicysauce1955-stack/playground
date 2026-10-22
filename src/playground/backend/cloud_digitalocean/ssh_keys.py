"""Pure helpers for the DigitalOcean-registered SSH key of a lab.

DigitalOcean refuses to create a Droplet from a custom image or a non-Ubuntu
stock image without ``ssh_keys`` (HTTP 422 "The image for this droplet does
not use root passwords, please use an SSH key").  The apply/resume preflight
therefore makes sure the lab's SSH public key -- the same file the backend
already injects through cloud-init -- is registered in the account, and the
tfvars attach it (by fingerprint) to exactly the Droplets that need it.

DigitalOcean identifies a key by its fingerprint: the colon-separated MD5 hex
digest of the base64-decoded key blob (``8e:5b:f8:...``), i.e. the legacy
OpenSSH ``ssh-keygen -l -E md5`` form.  No I/O, no network.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct

DO_KEY_NAME_PREFIX = "playground-"
"""Name prefix of a key the preflight registers: ``playground-<fp8>``."""


def _key_parts(public_key: str) -> tuple[str, bytes] | None:
    """``(key_type, blob)`` of an OpenSSH public key line, or ``None``.

    The blob must be valid base64 and start with the length-prefixed key
    type that the line declares (``ssh-ed25519 AAAAC3...``), which rejects a
    truncated or hand-mangled ``.pub`` file before DigitalOcean does.
    """
    fields = public_key.split()
    if len(fields) < 2:
        return None
    key_type, encoded = fields[0], fields[1]
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) < 4:
        return None
    (type_len,) = struct.unpack(">I", blob[:4])
    if blob[4 : 4 + type_len] != key_type.encode("ascii", "replace"):
        return None
    return key_type, blob


def md5_fingerprint(public_key: str) -> str | None:
    """DigitalOcean fingerprint of ``public_key``, or ``None`` if unparseable.

    :param public_key: One OpenSSH public key line (``<type> <base64>
        [comment]``), e.g. the stripped content of ``~/.ssh/id_rsa.pub``.
    """
    parts = _key_parts(public_key)
    if parts is None:
        return None
    digest = hashlib.md5(parts[1], usedforsecurity=False).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def do_key_name(fingerprint: str) -> str:
    """Name of the key the preflight registers: ``playground-<first 8 hex>``.

    The 8 hex characters are the start of the fingerprint without colons, so
    the same public key always gets the same name in every lab.
    """
    return f"{DO_KEY_NAME_PREFIX}{fingerprint.replace(':', '')[:8]}"


def key_for_upload(public_key: str) -> str | None:
    """``"<type> <base64>"`` of ``public_key`` -- the comment is dropped.

    The comment (usually ``user@host``) is not part of the fingerprint and
    need not leave this machine.
    """
    parts = public_key.split()
    if _key_parts(public_key) is None:
        return None
    return f"{parts[0]} {parts[1]}"


__all__ = [
    "DO_KEY_NAME_PREFIX",
    "do_key_name",
    "key_for_upload",
    "md5_fingerprint",
]
