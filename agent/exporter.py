"""Exporting generated code to disk and into a code editor (the `export_artifact` tool).

The agent can write code (a spawned worker's artifact, or code the model puts in the call itself). This module turns that
into a real file a person can open: it picks a safe name, writes it under one export directory, never overwrites an
existing file, and -- when a desktop editor is available -- opens it there (VS Code by default).

Safety rules, because the filename and content come from an LLM that read user (and possibly image) text:
  * only the base name is used: no directories, no traversal, no hidden files;
  * the extension must be on an allow-list of source/text types (no .sh/.bat/.exe: a file the user might double-click);
  * size is capped, and the file lands inside EXPORT_DIR and nowhere else;
  * the editor is launched with an argument LIST (no shell), non-blocking, and a failure to launch never fails the export.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

MAX_BYTES = 200_000

LANGUAGE_EXTENSIONS = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts", "tsx": "tsx", "jsx": "jsx",
    "html": "html", "css": "css", "json": "json", "markdown": "md", "md": "md", "text": "txt", "txt": "txt", "svg": "svg",
    "java": "java", "kotlin": "kt", "kt": "kt", "go": "go", "rust": "rs", "rs": "rs", "c": "c", "cpp": "cpp", "c++": "cpp",
    "sql": "sql", "yaml": "yml", "yml": "yml", "swift": "swift", "ruby": "rb", "php": "php", "toml": "toml", "xml": "xml",
}
ALLOWED_EXTENSIONS = set(LANGUAGE_EXTENSIONS.values())
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class ExportError(ValueError):
    """The export was refused; the message is safe to show the user."""


@dataclass
class ExportResult:
    filename: str
    path: str
    bytes: int
    language: str
    editor_uri: str                # vscode://file/... (valid when this machine is the one with the editor)
    download_path: str             # /exports/<filename> on the server (valid from anywhere the user can reach the server)
    opened_with: Optional[str]     # "code" if an editor was launched here, else None


def export_dir() -> Path:
    d = Path(os.getenv("EXPORT_DIR") or (Path.home() / "kairos-exports")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_filename(name: Optional[str], language: Optional[str]) -> str:
    """A base name that is safe to create: letters, digits, dot, dash, underscore; an allow-listed extension."""
    base = Path(str(name or "").replace("\\", "/")).name          # drop any directory part
    base = _SAFE_CHARS.sub("_", base).lstrip(".")                 # no hidden files, no odd characters
    ext_from_language = LANGUAGE_EXTENSIONS.get(str(language or "").strip().lower())
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = ext.lower()
    if not stem:
        stem = "kairos_export"
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ExportError(f"I won't write a .{ext} file; I only export source and text files ({', '.join(sorted(ALLOWED_EXTENSIONS))}).")
    ext = ext or ext_from_language or "txt"
    return f"{stem[:80]}.{ext}"


def _unique(directory: Path, filename: str) -> Path:
    path = directory / filename
    if not path.exists():
        return path
    stem, _, ext = filename.rpartition(".")
    for i in range(1, 1000):
        candidate = directory / f"{stem}-{i}.{ext}"
        if not candidate.exists():
            return candidate
    raise ExportError("Too many files with that name already exist.")


def editor_uri(path: Path) -> str:
    return "vscode://file/" + quote(str(path.resolve()), safe="/:")


def _display_available() -> bool:
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


def try_open_in_editor(path: Path) -> Optional[str]:
    """Launch the editor on `path` if one is installed and a desktop is present. Returns the command used, or None.
    EXPORT_OPEN=0 disables it, =1 forces an attempt, default "auto" requires the editor on PATH and a display."""
    mode = os.getenv("EXPORT_OPEN", "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return None
    cmd = os.getenv("EXPORT_EDITOR_CMD", "code")
    exe = shutil.which(cmd)
    if exe is None or (mode == "auto" and not _display_available()):
        return None
    try:
        subprocess.Popen([exe, "--reuse-window", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    return Path(exe).name


def export_file(content: str, filename: Optional[str] = None, language: Optional[str] = None, open_in: str = "vscode") -> ExportResult:
    if not isinstance(content, str) or not content.strip():
        raise ExportError("There is no code to export yet.")
    data = content.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ExportError(f"That is too large to export ({len(data) // 1000} KB; the limit is {MAX_BYTES // 1000} KB).")
    name = safe_filename(filename, language)
    directory = export_dir()
    path = _unique(directory, name)
    if directory.resolve() not in path.resolve().parents:        # defence in depth: never trust the checks above alone
        raise ExportError("Refused to write outside the export folder.")
    path.write_bytes(data)
    opened = try_open_in_editor(path) if open_in == "vscode" else None
    return ExportResult(
        filename=path.name, path=str(path), bytes=len(data), language=language or path.suffix.lstrip("."),
        editor_uri=editor_uri(path), download_path=f"/exports/{quote(path.name)}", opened_with=opened,
    )


def resolve_export(name: str) -> Optional[Path]:
    """The exported file called `name`, or None. Used by the download endpoint, so it must be airtight."""
    if not name or name != Path(name).name or name.startswith("."):
        return None
    path = export_dir() / name
    try:
        if path.is_file() and export_dir().resolve() in path.resolve().parents:
            return path
    except OSError:
        return None
    return None
