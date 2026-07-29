"""无需启动 Qt、网络或数据库的版本构建信息。"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ARCHIVE_SHA = "$Format:%H$"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _valid_sha(value: object) -> str | None:
    text = str(value or "").strip()
    return text.lower() if _SHA_RE.fullmatch(text) else None


def _source_checkout_sha() -> str | None:
    root = Path(__file__).resolve().parents[1]
    dot_git = root / ".git"
    if dot_git.is_file():
        try:
            line = dot_git.read_text(encoding="utf-8").strip()
            prefix = "gitdir:"
            if not line.lower().startswith(prefix):
                return None
            git_dir = Path(line[len(prefix):].strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
        except OSError:
            return None
    elif dot_git.is_dir():
        git_dir = dot_git
    else:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    except OSError:
        return None
    direct = _valid_sha(head)
    if direct is not None:
        return direct
    prefix = "ref:"
    if not head.startswith(prefix):
        return None
    ref_name = head[len(prefix):].strip().replace("\\", "/")
    if (
        not ref_name.startswith("refs/")
        or ".." in ref_name.split("/")
    ):
        return None
    try:
        ref_path = (git_dir / Path(*ref_name.split("/"))).resolve()
        git_root = git_dir.resolve()
        ref_path.relative_to(git_root)
        return _valid_sha(ref_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def runtime_sha() -> str:
    """返回 40 位完整 SHA；无法证明时明确返回 unavailable。"""

    return (
        _valid_sha(os.environ.get("PA_AGENT_BUILD_SHA"))
        or _valid_sha(_ARCHIVE_SHA)
        or _source_checkout_sha()
        or "unavailable"
    )
