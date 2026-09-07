"""Predicts where humble-cli will write a file, so the worker can verify
success by checking the filesystem rather than parsing its stdout.

Confirmed against the real binary (2026-09-06, v0.23.2, both a single-item and
a multi-item real bundle): folder structure is exactly
`<sanitized bundle name>/<sanitized item name>/<filename-from-url>`, rooted at
the subprocess's cwd. Sanitization replaces each of `/ \\ ? % * : | " < > ; =`
and newline with a single space — observed producing double spaces where a
character like ": " already had a following space (e.g. "Humble Software
REBundle: VEGAS Pro" -> "Humble Software REBundle  VEGAS Pro"); there is no
whitespace collapsing or trimming, so this must not "clean up" the result.
"""

import os
from pathlib import Path

_INVALID_CHARS = set('/\\?%*:|"<>;=')


class PathTraversalError(ValueError):
    """Raised when a predicted download path would resolve outside its root."""


def sanitize_dir_name(name: str) -> str:
    return "".join(" " if ch in _INVALID_CHARS or ch == "\n" else ch for ch in name)


def predict_download_path(bundle_name: str, item_name: str, filename: str) -> Path:
    return Path(sanitize_dir_name(bundle_name)) / sanitize_dir_name(item_name) / filename


def resolve_within(root: Path, rel_path: Path) -> Path:
    """Join rel_path onto root and confirm the result didn't escape it.

    sanitize_dir_name() strips path separators but never touches "..", which
    isn't a separator itself — a bundle/item name of exactly ".." (only
    plausible if Humble's own data contained it) would otherwise let a
    predicted path climb out of downloads_dir. Defense-in-depth: this is the
    single choke point every predicted path passes through before touching
    the filesystem.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / rel_path).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise PathTraversalError(f"Predicted path '{rel_path}' resolves outside downloads root")
    return candidate


def long_path_safe(path: Path) -> Path:
    """Windows-only, local-dev-only concern: confirmed against a real 3-level
    deep path (bundle/item/file.epub, 270 chars) that Python's pathlib silently
    reports a real, correctly-downloaded file as missing once the absolute path
    exceeds the classic 260-char MAX_PATH — humble-cli (Go) writes it fine, but
    os.stat() here returns FileNotFoundError, which Path.is_file() swallows
    into a plain False. The `\\\\?\\` extended-length prefix opts a single
    absolute path back into Windows' real ~32K-char limit. Irrelevant on Linux
    (the actual Docker deployment target), where no such limit exists — this
    only matters for testing this app directly on Windows before Docker is
    set up. Only ever apply this to paths passed to filesystem calls, never to
    ones shown in the UI or stored in the DB.
    """
    if os.name != "nt":
        return path
    resolved = str(path.resolve())
    return Path(resolved) if resolved.startswith("\\\\?\\") else Path("\\\\?\\" + resolved)
