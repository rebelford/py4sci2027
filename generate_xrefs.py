#!/usr/bin/env python3
"""Check or add MyST cross-reference targets in a Jupyter Book.

Scan <book root>/content recursively, including Markdown cells in .ipynb files.
Automatically label numbered headings matching the lesson number in the filename
(e.g., 01-03_Installing_Miniconda.ipynb -> 1.3, 1.3.1, etc.) and
'Activity N' headings nested beneath those sections. Ignore checkpoint
directories and skip auto-labeling overview files such as README.md. Leave other unnumbered headings alone.

Examples:
    python3 generate_xrefs.py --check
    python3 generate_xrefs.py --update
    python3 generate_xrefs.py --check --index
    python3 generate_xrefs.py --check --root ~/jupyterbooks/py4sci2027

No Jupyter Book build integration; run manually. --check never changes sources.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)(?:\.)?[ \t]+(.+)$")
ACTIVITY = re.compile(r"^Activity[ \t]+(\d+)(?:[ \t]*[:.\-][ \t]*|[ \t]+|$)", re.IGNORECASE)
LABEL = re.compile(r"^\s*\(([-\w.:]+)\)=\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
LESSON_FILE = re.compile(r"^(\d+)-(\d+)(?:[_-]|$)")
EXCLUDED_DIRS = {".ipynb_checkpoints", "_build", ".jupyter_cache", ".git", "__pycache__"}


def markdown_sources(path):
    """Read Markdown units and optionally the notebook JSON object."""
    if path.suffix.lower() == ".md":
        return [(None, path.read_text(encoding="utf-8"))], None
    notebook = json.loads(path.read_text(encoding="utf-8"))
    units = []
    for i, cell in enumerate(notebook.get("cells", [])):
        if cell.get("cell_type") == "markdown":
            source = cell.get("source", "")
            units.append((i, "".join(source) if isinstance(source, list) else source))
    return units, notebook


def markdown_lines(source):
    """Yield unfenced lines, recognizing both backtick and tilde fences."""
    marker_char, marker_len = None, 0
    for i, line in enumerate(source.splitlines(keepends=True)):
        match = FENCE.match(line)
        if match:
            marker, tail = match.groups()
            if marker_char is None:
                marker_char, marker_len = marker[0], len(marker)
            elif marker[0] == marker_char and len(marker) >= marker_len and not tail.strip():
                marker_char, marker_len = None, 0
            continue
        if marker_char is None:
            yield i, line


def insert_missing(source, additions):
    lines = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    for index, label in sorted(additions, reverse=True):
        lines.insert(index, f"({label})={newline}")
    return "".join(lines)


def collect(path, root):
    """Return entries, label occurrences, and planned changes for one file.

    Maintain a numbered-heading stack *across Markdown cells*. An activity's
    parent is the nearest numbered ancestor at a strictly smaller heading depth.
    Unnumbered headings at the same or higher level clear the parent, so labels
    cannot accidentally be assigned from an unrelated preceding section.
    """
    units, notebook = markdown_sources(path)
    entries, occurrences, planned = [], [], []
    parents = []  # (heading level, section number)
    filename_match = LESSON_FILE.match(path.stem)
    expected_section = (f"{int(filename_match.group(1))}.{int(filename_match.group(2))}"
                        if filename_match else None)
    relative = str(path.relative_to(root))

    for cell_idx, source in units:
        lines = source.splitlines(keepends=True)
        location = relative + (f" [cell {cell_idx + 1}]" if cell_idx is not None else "")
        active = dict(markdown_lines(source))
        for line in active.values():
            match = LABEL.match(line)
            if match:
                occurrences.append((match.group(1), location, "existing"))

        additions = []
        for i, line in active.items():
            h = HEADING.match(line.rstrip("\r\n"))
            if not h:
                continue
            level, title = len(h.group(1)), h.group(2).strip()
            # Remove headings that can no longer be ancestors of this heading.
            while parents and parents[-1][0] >= level:
                parents.pop()
            num_match = NUMBERED.match(title)
            activity_match = ACTIVITY.match(title)
            if num_match and expected_section and (
                num_match.group(1) == expected_section
                or num_match.group(1).startswith(expected_section + ".")
            ):
                number = num_match.group(1)
                label = "sec-" + number.replace(".", "-")
                kind = "section"
                parents.append((level, number))
            elif activity_match and parents and expected_section:
                number = parents[-1][1]
                label = "sec-" + number.replace(".", "-") + "-activity-" + activity_match.group(1)
                kind = "activity"
            else:
                continue

            # Existing label immediately before this heading (allow blank lines).
            previous = i - 1
            while previous >= 0 and not lines[previous].strip():
                previous -= 1
            match = LABEL.match(lines[previous]) if previous >= 0 and previous in active else None
            existing = match.group(1) if match else None
            actual = existing or label
            entries.append((actual, title, location, bool(existing), kind))
            if not existing:
                additions.append((i, label))
                occurrences.append((label, location, "proposed"))
        if additions:
            planned.append((cell_idx, additions))
    return notebook, units, entries, occurrences, planned


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Report missing or duplicate labels; don't change files")
    mode.add_argument("--update", action="store_true", help="Insert missing targets into Markdown and notebook cells")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Book root (default: current working directory)")
    parser.add_argument("--index", action="store_true", help="Write xref_index.md in the book root")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    content = root / "content"
    if not content.is_dir():
        parser.error(f"Cannot find {content}. Run from the book root or provide --root.")

    paths = sorted(
        p for p in content.rglob("*")
        if p.is_file() and p.suffix.lower() in (".md", ".ipynb")
        and not any(part in EXCLUDED_DIRS or part.startswith(".") for part in p.relative_to(content).parts[:-1])
    )
    entries, occurrences, changes = [], defaultdict(list), {}
    for path in paths:
        try:
            notebook, units, found, labels, planned = collect(path, root)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"ERROR reading {path}: {exc}", file=sys.stderr)
            return 2
        entries.extend(found)
        for label, location, status in labels:
            occurrences[label].append(f"{location} ({status})")
        if planned:
            changes[path] = (notebook, units, planned)

    duplicates = {name: locs for name, locs in occurrences.items() if len(locs) > 1}
    sections = sum(e[4] == "section" for e in entries)
    activities = sum(e[4] == "activity" for e in entries)
    missing = sum(not e[3] for e in entries)
    print(f"Scanned {len(paths)} files under {content}")
    print(f"Found {sections} numbered headings and {activities} labeled-candidate activities; {missing} need labels.")
    if duplicates:
        print("\nDUPLICATE LABELS (source files not modified):", file=sys.stderr)
        for label, locs in sorted(duplicates.items()):
            print(f"  {label}: {'; '.join(locs)}", file=sys.stderr)
        return 1

    if args.update:
        for path, (notebook, units, planned) in changes.items():
            if notebook is None:
                path.write_text(insert_missing(units[0][1], planned[0][1]), encoding="utf-8")
            else:
                for cell_idx, additions in planned:
                    cell = notebook["cells"][cell_idx]
                    original = cell.get("source", "")
                    source = "".join(original) if isinstance(original, list) else original
                    updated = insert_missing(source, additions)
                    cell["source"] = updated.splitlines(keepends=True) if isinstance(original, list) else updated
                path.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"Updated {path.relative_to(root)}")
    else:
        for label, title, location, exists, kind in entries:
            if not exists:
                print(f"  Missing {label}: {location} — {title}")

    if args.index:
        index = root / "xref_index.md"
        body = ["# Cross-reference index", "", "Generated by `generate_xrefs.py`. Do not edit manually.", "", "| Label | Heading | Source |", "| --- | --- | --- |"]
        for label, title, location, exists, kind in entries:
            body.append(f"| `{label}` | {title.replace('|', chr(92) + '|')} | `{location}` |")
        index.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"Wrote {index}")
    print("Done." if args.update else "Check complete. No source files changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
