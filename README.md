# superyml

`superyml` is a small CLI tool that scans YAML files from the current directory (or a provided path), validates them, and rewrites them with consistent formatting.

## Install

```bash
cd superyml
pip install -e .
```

## Usage

```bash
# scan from current directory and auto-fix files
superyml

# scan another directory
superyml /path/to/repo

# check only (no writes)
superyml --check

# show each processed file
superyml --verbose
```

## Exit codes

- `0`: all scanned files are valid YAML (and fixed if not in check mode)
- `1`: at least one YAML file has a parse error
