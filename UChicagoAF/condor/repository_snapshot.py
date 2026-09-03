#!/usr/bin/env python3
"""Fingerprint the exact non-ignored repository state used by a campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import struct
import subprocess
from pathlib import Path


SNAPSHOT_CONTRACT = "oap-git-working-tree-v1"


class SnapshotError(RuntimeError):
    """Raised when a repository cannot be fingerprinted unambiguously."""


def _git(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise SnapshotError(f"could not execute git: {error}") from error
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(diagnostic or "git command failed")
    return completed.stdout


def inspect_repository(repository: str | Path) -> dict[str, object]:
    """Return the HEAD revision and a digest of every non-ignored file.

    The content digest intentionally includes tracked modifications and
    untracked, non-ignored files. A campaign may therefore be prepared from a
    development checkout, but execute nodes must see those exact bytes.
    """

    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise SnapshotError(f"repository is not a directory: {root}")
    top_level = Path(
        _git(root, "rev-parse", "--show-toplevel")
        .decode("utf-8", errors="strict")
        .strip()
    ).resolve()
    if top_level != root:
        raise SnapshotError(
            f"repository path {root} is not the Git top level {top_level}"
        )
    revision = (
        _git(root, "rev-parse", "--verify", "HEAD^{commit}")
        .decode("ascii", errors="strict")
        .strip()
    )
    if len(revision) != 40 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise SnapshotError("Git returned an invalid HEAD revision")

    names_raw = _git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    names = sorted(name for name in names_raw.split(b"\0") if name)
    if not names:
        raise SnapshotError("repository contains no tracked or non-ignored files")

    digest = hashlib.sha256()
    digest.update(SNAPSHOT_CONTRACT.encode("ascii") + b"\0")
    for encoded_name in names:
        try:
            name = encoded_name.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise SnapshotError("repository path is not valid UTF-8") from error
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError(f"unsafe repository path returned by Git: {name!r}")
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise SnapshotError(f"repository file is unavailable: {name}") from error
        if stat.S_ISLNK(metadata.st_mode):
            mode = b"120000"
            payload = os.readlink(path).encode("utf-8")
        elif stat.S_ISREG(metadata.st_mode):
            mode = b"100755" if metadata.st_mode & stat.S_IXUSR else b"100644"
            try:
                payload = path.read_bytes()
            except OSError as error:
                raise SnapshotError(f"cannot read repository file: {name}") from error
        else:
            raise SnapshotError(f"unsupported repository file type: {name}")
        digest.update(struct.pack(">I", len(encoded_name)))
        digest.update(encoded_name)
        digest.update(mode)
        digest.update(struct.pack(">Q", len(payload)))
        digest.update(payload)

    return {
        "contract": SNAPSHOT_CONTRACT,
        "revision": revision,
        "sha256": digest.hexdigest(),
        "file_count": len(names),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = inspect_repository(args.repository)
    except SnapshotError as error:
        raise SystemExit(f"repository snapshot error: {error}") from error
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
