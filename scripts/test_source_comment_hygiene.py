"""Keep implementation comments about behavior rather than development history."""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = frozenset({".js", ".py", ".rs", ".sh", ".toml", ".ts", ".yaml", ".yml"})
PROCESS_HISTORY = re.compile(
    r"(?i)(?:\btask\s+\d+\b|\bcard\s+\d+\b|\breview[- ]revision(?:[- ]\d+)?\b|"
    r"\brevision\s+\d+\b|\bearlier revision\b|\bmilestone\b|\bhandoff\b)"
)


def _tracked_sources() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    paths = [Path(raw.decode("utf-8")) for raw in completed.stdout.split(b"\0") if raw]
    return [
        REPO_ROOT / path
        for path in paths
        if path.suffix in SOURCE_SUFFIXES and path.parts[0] != "research"
    ]


def _python_comments(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    comments = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            comments.append((body[0].lineno, body[0].value.value))
    return comments


def _line_comments(path: Path) -> list[tuple[int, str]]:
    markers = ("#", "//", "/*", "*", "<!--")
    return [
        (line_number, line)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if line.lstrip().startswith(markers)
    ]


def test_source_comments_do_not_embed_workflow_history():
    violations: list[str] = []
    for path in _tracked_sources():
        comments = _python_comments(path) if path.suffix == ".py" else _line_comments(path)
        for line_number, comment in comments:
            match = PROCESS_HISTORY.search(comment)
            if match:
                relative = path.relative_to(REPO_ROOT)
                violations.append(f"{relative}:{line_number}: {match.group(0)}")
    assert not violations, "workflow history belongs in research records, not source comments:\n" + "\n".join(violations)
