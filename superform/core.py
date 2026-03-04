from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import re
from typing import Callable, Iterable
import xml.etree.ElementTree as ET

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError


FILE_TYPE_BY_EXTENSION = {
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".xml": "xml",
    ".xsd": "xml",
    ".xsl": "xml",
    ".svg": "xml",
    ".wsdl": "xml",
    ".plist": "xml",
    ".xhtml": "xml",
    ".config": "xml",
    ".csproj": "xml",
    ".vbproj": "xml",
    ".fsproj": "xml",
    ".props": "xml",
    ".targets": "xml",
    ".resx": "xml",
}
DEFAULT_EXTENSIONS = tuple(FILE_TYPE_BY_EXTENSION.keys())
YAML_EXTENSIONS = (".yaml", ".yml")
SKIP_DIRS = {".git", ".hg", ".svn", ".tox", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
_MAPPING_KEY_RE = re.compile(r"^[A-Za-z0-9_.\"'/-]+\s*:")
_STRUCTURAL_LINE_RE = re.compile(r"^(?:-\s+)?[A-Za-z0-9_.\"'/-]+\s*:")
_ERROR_LINE_RE = re.compile(r"line\s+(\d+),\s+column\s+\d+")
_XML_DECL_RE = re.compile(r"^\ufeff?\s*<\?xml(?:\s|\?>)", re.IGNORECASE)
_XML_BAD_PUBLIC_DOCTYPE_RE = re.compile(
    r"<!DOCTYPE\s+(?P<root>[A-Za-z_][\w:.-]*)\s+PUBLIC\s+\"[^\"]+\"\s*>",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScanResult:
    path: Path
    changed: bool
    error: str | None = None
    file_type: str = "unknown"


def _is_skipped_path(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    return any(part in SKIP_DIRS for part in rel_parts)


def detect_file_type(path: Path) -> str | None:
    return FILE_TYPE_BY_EXTENSION.get(path.suffix.lower())


def iter_supported_files(root: Path, extensions: tuple[str, ...] = DEFAULT_EXTENSIONS) -> Iterable[Path]:
    normalized_extensions = {ext.lower() for ext in extensions}

    if root.is_file():
        if root.suffix.lower() in normalized_extensions:
            yield root
        return

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_skipped_path(path, root):
            continue
        if path.suffix.lower() in normalized_extensions:
            yield path


def iter_yaml_files(root: Path, extensions: tuple[str, ...] = YAML_EXTENSIONS) -> Iterable[Path]:
    yield from iter_supported_files(root, extensions=extensions)


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


def _next_significant(lines: list[str], idx: int) -> tuple[int, str] | None:
    for i in range(idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            return i, lines[i]
    return None


def _parse_docs(yaml: YAML, text: str):
    return list(yaml.load_all(text))


def _with_trailing_newline(text: str) -> str:
    if text and not text.endswith("\n"):
        return f"{text}\n"
    return text


def _replace_leading_tabs(raw_text: str, *, spaces_per_tab: int = 2) -> tuple[str, bool]:
    lines = raw_text.splitlines(keepends=True)
    converted: list[str] = []
    changed = False
    replacement = " " * spaces_per_tab

    for line in lines:
        idx = 0
        while idx < len(line) and line[idx] in {" ", "\t"}:
            idx += 1
        prefix = line[:idx]
        if "\t" in prefix:
            prefix = prefix.replace("\t", replacement)
            line = f"{prefix}{line[idx:]}"
            changed = True
        converted.append(line)

    return "".join(converted), changed


def _is_structural_yaml_line(content: str) -> bool:
    stripped = content.lstrip()
    if stripped.startswith("- "):
        return True
    return bool(_STRUCTURAL_LINE_RE.match(stripped))


def _recover_aggressive_indent_errors(raw_text: str) -> str:
    lines = raw_text.splitlines(keepends=True)
    if not lines:
        return raw_text

    recovered: list[str] = []
    prev_indent = 0
    prev_content = ""
    has_prev_significant = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            recovered.append(line)
            continue

        indent = len(line) - len(line.lstrip(" "))
        content = line.lstrip(" ")
        structural = _is_structural_yaml_line(content)

        next_info = _next_significant(lines, idx)
        next_indent = None
        if next_info is not None:
            _, next_line = next_info
            next_indent = len(next_line) - len(next_line.lstrip(" "))

        if has_prev_significant and structural:
            # Large one-line right-shifts are usually accidental copy/paste whitespace.
            if indent - prev_indent >= 8 and not _opens_block(prev_content):
                target_indent = prev_indent
                if next_indent is not None:
                    target_indent = min(target_indent, next_indent)
                indent = max(target_indent, 0)
                line = (" " * indent) + content

            # If a key/list line suddenly shifts too far left after a list entry,
            # keep it aligned with the current list item block.
            if indent > 0 and indent < prev_indent and prev_content.lstrip().startswith("- "):
                indent = prev_indent
                line = (" " * indent) + content

            # Structural lines should use even indentation.
            if indent % 2 == 1:
                indent = max((indent // 2) * 2, 0)
                line = (" " * indent) + content

        recovered.append(line)
        prev_indent = indent
        prev_content = content.rstrip("\r\n")
        has_prev_significant = True

    return "".join(recovered)


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

            nxt = _next_significant(best_lines, idx)
            next_indent = None
            if nxt is not None:
                _, next_line = nxt
                next_indent = len(next_line) - len(next_line.lstrip(" "))

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
            if next_indent is not None:
                candidate_indents.append(next_indent)
                if next_indent > 0:
                    candidate_indents.append(max(next_indent - 2, 0))

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
                    if (
                        next_error_line is not None
                        and (
                            next_error_line > error_line
                            or (next_error_line == error_line and indent < current_indent)
                        )
                    ):
                        best_lines = candidate_lines
                        current_text = candidate_text
                        progressed = True
                        break

            if not progressed:
                return None, None

    return None, None


def _normalize_yaml_text(raw_text: str) -> tuple[str, bool, str | None]:
    yaml = _build_yaml()
    recovered_text, had_tab_indentation = _replace_leading_tabs(raw_text)

    recovered_from_error = had_tab_indentation
    try:
        docs = _parse_docs(yaml, recovered_text)
    except YAMLError as exc:
        candidate = _recover_common_indent_errors(recovered_text)
        try:
            docs = _parse_docs(yaml, candidate)
            recovered_text = candidate
            recovered_from_error = True
        except YAMLError:
            aggressive = _recover_aggressive_indent_errors(candidate)
            if aggressive != candidate:
                try:
                    docs = _parse_docs(yaml, aggressive)
                    recovered_text = aggressive
                    recovered_from_error = True
                except YAMLError:
                    recovered_candidate, recovered_docs = _try_indent_recovery(yaml, aggressive)
                    if recovered_candidate is None or recovered_docs is None:
                        return "", False, str(exc).strip()
                    recovered_text = recovered_candidate
                    docs = recovered_docs
                    recovered_from_error = True
            else:
                recovered_candidate, recovered_docs = _try_indent_recovery(yaml, candidate)
                if recovered_candidate is None or recovered_docs is None:
                    return "", False, str(exc).strip()
                recovered_text = recovered_candidate
                docs = recovered_docs
                recovered_from_error = True

    buffer = StringIO()
    yaml.dump_all(docs, buffer)
    fixed_text = _with_trailing_newline(buffer.getvalue())
    return fixed_text, recovered_from_error, None


def _normalize_json_text(raw_text: str) -> tuple[str, bool, str | None]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return "", False, f"JSON parse error: {exc.msg} (line {exc.lineno}, column {exc.colno})"

    fixed_text = json.dumps(parsed, indent=2, ensure_ascii=False)
    return _with_trailing_newline(fixed_text), False, None


def _build_xml_parser() -> ET.XMLParser:
    # Preserve comments and processing instructions where ElementTree supports it.
    try:
        target = ET.TreeBuilder(insert_comments=True, insert_pis=True)
    except TypeError:
        target = ET.TreeBuilder(insert_comments=True)
    return ET.XMLParser(target=target)


def _format_xml_error(exc: ET.ParseError) -> str:
    message = str(exc)
    if hasattr(exc, "position") and exc.position and "line" not in message.lower():
        line, col = exc.position
        message = f"{message} (line {line}, column {col})"
    return f"XML parse error: {message}"


def _find_xml_tag_end(text: str, start: int) -> int:
    quote: str | None = None
    i = start + 1

    while i < len(text):
        ch = text[i]
        if quote is None:
            if ch in {'"', "'"}:
                quote = ch
            elif ch == ">":
                return i
        elif ch == quote:
            quote = None
        i += 1

    return -1


def _extract_xml_doctype(text: str) -> str | None:
    match = re.search(r"<!DOCTYPE[^>]*>", text, re.IGNORECASE)
    if match is None:
        return None
    return match.group(0).strip()


def _inject_xml_doctype(serialized_xml: str, doctype: str | None) -> str:
    if not doctype:
        return serialized_xml
    if doctype in serialized_xml:
        return serialized_xml

    if serialized_xml.startswith("<?xml"):
        first_line, sep, rest = serialized_xml.partition("\n")
        if sep:
            return f"{first_line}\n{doctype}\n{rest}"
        return f"{first_line}\n{doctype}\n"

    return f"{doctype}\n{serialized_xml}"


def _recover_xml_structure(raw_text: str) -> str:
    if not raw_text:
        return raw_text

    out: list[str] = []
    stack: list[str] = []
    i = 0

    while i < len(raw_text):
        ch = raw_text[i]
        if ch != "<":
            out.append(ch)
            i += 1
            continue

        if raw_text.startswith("<!--", i):
            end = raw_text.find("-->", i + 4)
            if end == -1:
                out.append(raw_text[i:])
                break
            out.append(raw_text[i : end + 3])
            i = end + 3
            continue

        if raw_text.startswith("<![CDATA[", i):
            end = raw_text.find("]]>", i + 9)
            if end == -1:
                out.append(raw_text[i:])
                break
            out.append(raw_text[i : end + 3])
            i = end + 3
            continue

        if raw_text.startswith("<?", i):
            end = raw_text.find("?>", i + 2)
            if end == -1:
                out.append(raw_text[i:])
                break
            out.append(raw_text[i : end + 2])
            i = end + 2
            continue

        if raw_text[i + 1 : i + 2] == "!":
            end = _find_xml_tag_end(raw_text, i)
            if end == -1:
                out.append(raw_text[i:])
                break
            out.append(raw_text[i : end + 1])
            i = end + 1
            continue

        end = _find_xml_tag_end(raw_text, i)
        if end == -1:
            out.append("&lt;")
            i += 1
            continue

        raw_tag = raw_text[i : end + 1]
        stripped_tag = raw_tag.strip()

        if stripped_tag.startswith("</"):
            match = re.match(r"</\s*([A-Za-z_][\w:.-]*)", stripped_tag)
            if not match:
                i = end + 1
                continue

            name = match.group(1)
            if name in stack:
                while stack and stack[-1] != name:
                    out.append(f"</{stack.pop()}>")
                if stack and stack[-1] == name:
                    stack.pop()
                    out.append(raw_tag)
            i = end + 1
            continue

        if stripped_tag.endswith("/>"):
            out.append(raw_tag)
            i = end + 1
            continue

        start_match = re.match(r"<\s*([A-Za-z_][\w:.-]*)", stripped_tag)
        if start_match:
            name = start_match.group(1)
            stack.append(name)
            out.append(raw_tag)
            i = end + 1
            continue

        out.append("&lt;")
        i += 1

    while stack:
        out.append(f"</{stack.pop()}>")

    return "".join(out)


def _prepare_xml_text(raw_text: str) -> tuple[str, bool]:
    text, changed = _replace_leading_tabs(raw_text)

    if text.startswith("\ufeff"):
        text = text[1:]
        changed = True

    sanitized = _XML_BAD_PUBLIC_DOCTYPE_RE.sub(lambda m: f"<!DOCTYPE {m.group('root')}>", text)
    if sanitized != text:
        text = sanitized
        changed = True

    decl_idx = text.lower().find("<?xml")
    if decl_idx > 0 and not text[:decl_idx].strip():
        text = text[decl_idx:]
        changed = True

    return text, changed


def _normalize_xml_text(raw_text: str) -> tuple[str, bool, str | None]:
    normalized_text, pre_changed = _prepare_xml_text(raw_text)
    doc_type = _extract_xml_doctype(normalized_text)

    try:
        root = ET.fromstring(normalized_text, parser=_build_xml_parser())
    except ET.ParseError as exc:
        recovered = _recover_xml_structure(normalized_text)
        if recovered != normalized_text:
            try:
                root = ET.fromstring(recovered, parser=_build_xml_parser())
                normalized_text = recovered
                pre_changed = True
            except ET.ParseError:
                return "", False, _format_xml_error(exc)
        else:
            return "", False, _format_xml_error(exc)

    for elem in root.iter():
        if elem.text is not None and not elem.text.strip():
            elem.text = None
        if elem.tail is not None and not elem.tail.strip():
            elem.tail = None

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    has_declaration = bool(_XML_DECL_RE.match(raw_text))
    buffer = StringIO()
    tree.write(buffer, encoding="unicode", xml_declaration=has_declaration)
    serialized = _inject_xml_doctype(buffer.getvalue(), doc_type)
    return _with_trailing_newline(serialized), pre_changed, None


_NORMALIZERS: dict[str, Callable[[str], tuple[str, bool, str | None]]] = {
    "yaml": _normalize_yaml_text,
    "json": _normalize_json_text,
    "xml": _normalize_xml_text,
}


def _normalize_file(path: Path, *, file_type: str, check_only: bool = False) -> ScanResult:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return ScanResult(path=path, changed=False, error=f"Unicode decode error: {exc}", file_type=file_type)

    normalizer = _NORMALIZERS[file_type]
    fixed_text, forced_change, error = normalizer(raw_text)
    if error is not None:
        return ScanResult(path=path, changed=False, error=error, file_type=file_type)

    changed = fixed_text != raw_text or forced_change
    if changed and not check_only:
        path.write_text(fixed_text, encoding="utf-8")

    return ScanResult(path=path, changed=changed, error=None, file_type=file_type)


def normalize_structured_file(path: Path, *, check_only: bool = False) -> ScanResult:
    file_type = detect_file_type(path)
    if file_type is None:
        suffix = path.suffix.lower() or "(no extension)"
        return ScanResult(
            path=path,
            changed=False,
            error=f"Unsupported file extension: {suffix}",
            file_type="unsupported",
        )

    return _normalize_file(path, file_type=file_type, check_only=check_only)


def normalize_yaml_file(path: Path, *, check_only: bool = False) -> ScanResult:
    return _normalize_file(path, file_type="yaml", check_only=check_only)
