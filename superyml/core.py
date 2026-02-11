from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


DEFAULT_EXTENSIONS = (".yaml", ".yml")
SKIP_DIRS = {".git", ".hg", ".svn", ".tox", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
_MAPPING_KEY_RE = re.compile(r"^[A-Za-z0-9_.\"'/-]+\s*:")
_ERROR_LINE_RE = re.compile(r"line\s+(\d+),\s+column\s+\d+")


@dataclass(frozen=True)
class ScanResult:
    path: Path
    changed: bool
    error: str | None = None


def iter_yaml_files(root: Path, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in extensions:
            yield path


def _build_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 100
    yaml.indent(mapping=2, sequence=2, offset=0)
    return yaml


def _opens_block(stripped_line: str) -> bool:
    # Simple YAML block opener: "key:" or "- key:" with no inline value.
    return bool(re.match(r"^(?:-\s+)?[A-Za-z0-9_.\"'/-]+\s*:\s*$", stripped_line))


def _recover_common_indent_errors(raw_text: str) -> str:
    lines = raw_text.splitlines(keepends=True)
    recovered: list[str] = []
    parent_indent = 0
    prev_significant = ""

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            recovered.append(line)
            continue

        indent = len(line) - len(line.lstrip(" "))
        content = line.lstrip(" ")

        # Recovery case: accidental indentation for a top-level mapping key
        # where previous significant line does not open a nested block.
        if (
            parent_indent == 0
            and indent > 0
            and not content.startswith("-")
            and _MAPPING_KEY_RE.match(content)
            and not _opens_block(prev_significant)
        ):
            line = content
            indent = 0

        recovered.append(line)
        if _opens_block(content.rstrip("\n")):
            parent_indent = indent + 2
        elif indent < parent_indent:
            parent_indent = indent
        prev_significant = content.rstrip("\n")

    return "".join(recovered)


def _extract_error_line(error_text: str) -> int | None:
    match = _ERROR_LINE_RE.search(error_text)
    if not match:
        return None
    return int(match.group(1))


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _previous_significant(lines: list[str], idx: int) -> tuple[int, str] | None:
    for i in range(idx - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            return i, lines[i]
    return None


def _parse_docs(yaml: YAML, text: str):
    return list(yaml.load_all(text))


def _try_indent_recovery(yaml: YAML, raw_text: str) -> tuple[str | None, list | None]:
    lines = raw_text.splitlines(keepends=True)
    if not lines:
        return None, None

    current_text = raw_text
    best_lines = lines[:]
    seen = {current_text}

    for _ in range(40):
        try:
            docs = _parse_docs(yaml, current_text)
            return current_text, docs
        except YAMLError as exc:
            error_line = _extract_error_line(str(exc))
            if error_line is None or error_line < 1 or error_line > len(best_lines):
                return None, None

            idx = error_line - 1
            original_line = best_lines[idx]
            content = original_line.lstrip(" \t")
            if not content.strip() or content.lstrip().startswith("#"):
                return None, None

            prev = _previous_significant(best_lines, idx)
            if prev is None:
                prev_indent = 0
                prev_content = ""
            else:
                _, prev_line = prev
                prev_indent = len(prev_line) - len(prev_line.lstrip(" "))
                prev_content = prev_line.lstrip(" ").rstrip("\r\n")

            current_indent = len(original_line) - len(original_line.lstrip(" "))
            newline = _line_ending(original_line)
            content_no_newline = content.rstrip("\r\n")

            candidate_indents: list[int] = []
            candidate_indents.append(0)
            candidate_indents.append(prev_indent)
            if _opens_block(prev_content):
                candidate_indents.append(prev_indent + 2)
            candidate_indents.append(max(prev_indent - 2, 0))
            candidate_indents.append((current_indent // 2) * 2)
            candidate_indents.append(max(((current_indent - 2) // 2) * 2, 0))
            candidate_indents.append(min(((current_indent + 2) // 2) * 2, prev_indent + 4))

            deduped_indents: list[int] = []
            for indent in candidate_indents:
                indent = max(indent, 0)
                if indent not in deduped_indents:
                    deduped_indents.append(indent)

            progressed = False
            for indent in deduped_indents:
                candidate_line = (" " * indent) + content_no_newline + newline
                if candidate_line == original_line:
                    continue
                candidate_lines = best_lines[:]
                candidate_lines[idx] = candidate_line
                candidate_text = "".join(candidate_lines)
                if candidate_text in seen:
                    continue
                seen.add(candidate_text)

                try:
                    docs = _parse_docs(yaml, candidate_text)
                    return candidate_text, docs
                except YAMLError as candidate_exc:
                    next_error_line = _extract_error_line(str(candidate_exc))
                    if next_error_line is not None and next_error_line >= error_line:
                        best_lines = candidate_lines
                        current_text = candidate_text
                        progressed = True
                        break

            if not progressed:
                return None, None

    return None, None


def normalize_yaml_file(path: Path, *, check_only: bool = False) -> ScanResult:
    yaml = _build_yaml()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return ScanResult(path=path, changed=False, error=f"Unicode decode error: {exc}")

    recovered_text = raw_text
    recovered_from_error = False
    try:
        docs = _parse_docs(yaml, recovered_text)
    except YAMLError as exc:
        candidate = _recover_common_indent_errors(raw_text)
        try:
            docs = _parse_docs(yaml, candidate)
            recovered_text = candidate
            recovered_from_error = True
        except YAMLError:
            recovered_candidate, recovered_docs = _try_indent_recovery(yaml, candidate)
            if recovered_candidate is None or recovered_docs is None:
                return ScanResult(path=path, changed=False, error=str(exc).strip())
            recovered_text = recovered_candidate
            docs = recovered_docs
            recovered_from_error = True

    from io import StringIO

    buffer = StringIO()
    yaml.dump_all(docs, buffer)
    fixed_text = buffer.getvalue()
    if fixed_text and not fixed_text.endswith("\n"):
        fixed_text += "\n"

    changed = fixed_text != raw_text or recovered_from_error
    if changed and not check_only:
        path.write_text(fixed_text, encoding="utf-8")

    return ScanResult(path=path, changed=changed, error=None)
