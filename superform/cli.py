from __future__ import annotations

import argparse
from pathlib import Path

from .core import iter_supported_files, normalize_structured_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="superform",
        description="Recursively validate and auto-fix structured files (YAML, JSON, XML).",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Root path to scan (default: current directory).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check only, do not rewrite files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each processed file.",
    )
    return parser


def _display_path(path: Path, root: Path) -> str:
    if root.is_file():
        return path.name
    return str(path.relative_to(root))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        raise SystemExit(f"Path not found: {root}")

    files = sorted(iter_supported_files(root))
    if not files:
        print(f"No supported structured files found under: {root}")
        return 0

    changed_count = 0
    error_count = 0

    for file_path in files:
        result = normalize_structured_file(file_path, check_only=args.check)
        rel = _display_path(file_path, root)
        kind = result.file_type.upper()
        if result.error:
            error_count += 1
            print(f"[ERROR][{kind}] {rel}: {result.error}")
            continue
        if result.changed:
            changed_count += 1
            action = "would-fix" if args.check else "fixed"
            print(f"[{action.upper()}][{kind}] {rel}")
        elif args.verbose:
            print(f"[OK][{kind}] {rel}")

    scanned = len(files)
    print(
        f"\nScanned: {scanned} structured files | "
        f"{'Would fix' if args.check else 'Fixed'}: {changed_count} | "
        f"Errors: {error_count}"
    )

    return 1 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
