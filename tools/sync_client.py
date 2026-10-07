#!/usr/bin/env python3
"""Keep `client/` in step with the skill bundle it is vendored from.

`client/bootbench.py` is a copy. That is deliberate -- the client directory
has to be deployable by copying it to a bench host, with nothing installed
and no sibling repo required -- but a copy of a 3000-line parser is exactly
the kind of duplication that goes wrong quietly. If the two drift, the
coordinator parses boot times with one vintage of the regexes while the
bench host produced them with another, and the symptom is a number on a
dashboard that is wrong by a plausible-looking amount. Nothing crashes.

So the duplication is enforced rather than trusted:

    python tools/sync_client.py --check    # exit 1 if anything differs
    python tools/sync_client.py            # copy the skill over the client

`server/tests/test_client_sync.py` runs --check as a test, and skips when
the skill bundle is absent -- which is the normal state of an extracted
standalone checkout, where `client/` is simply the truth.

Direction of truth: the skill bundle. `boot-skills/skills/bootbench` is
hand-run by engineers and documented by its own SKILL.md; this repo
automates it. Edit the skill, then sync.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Where the skill bundle sits relative to this repo, as checked out beside
# it in skills-lab. Overridable, because a standalone clone has no fixed
# relationship to it.
DEFAULT_SKILL_ROOT = ROOT.parent / "boot-skills" / "skills" / "bootbench"

# (path within the skill bundle, path within this repo)
VENDORED = (
    ("scripts/bootbench.py", "client/bootbench.py"),
    ("references/COMMAND_REFERENCE.md", "client/docs/COMMAND_REFERENCE.md"),
    ("references/DATA_MODEL.md", "client/docs/DATA_MODEL.md"),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def compare(skill_root: Path):
    """[(source, destination, state)] for every vendored file.

    State is one of 'same', 'differs', 'missing-source', 'missing-dest'.
    """
    rows = []
    for rel_src, rel_dst in VENDORED:
        src, dst = skill_root / rel_src, ROOT / rel_dst
        if not src.is_file():
            state = "missing-source"
        elif not dst.is_file():
            state = "missing-dest"
        else:
            # Compared as bytes, not as text: a line-ending change is a real
            # difference to `git diff` and to the golden-file tests, so it
            # should be a real difference here too.
            state = "same" if src.read_bytes() == dst.read_bytes() else "differs"
        rows.append((src, dst, state))
    return rows


def show_diff(src: Path, dst: Path, limit=40) -> None:
    diff = difflib.unified_diff(
        dst.read_text(encoding="utf-8", errors="replace").splitlines(),
        src.read_text(encoding="utf-8", errors="replace").splitlines(),
        fromfile=f"client/{dst.name} (vendored)",
        tofile=f"skill/{src.name} (source of truth)",
        lineterm="",
    )
    for index, line in enumerate(diff):
        if index >= limit:
            print("  ... (truncated; run git diff for the rest)")
            break
        print(f"  {line}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Edit the skill bundle, then sync. Never the reverse.",
    )
    parser.add_argument("--check", action="store_true",
                        help="report drift and exit 1; change nothing")
    parser.add_argument("--skill-root", default=None,
                        help=f"the bootbench skill bundle "
                             f"(default: {DEFAULT_SKILL_ROOT})")
    parser.add_argument("--diff", action="store_true",
                        help="with --check, print what differs")
    args = parser.parse_args(argv)

    skill_root = Path(args.skill_root or DEFAULT_SKILL_ROOT).resolve()
    if not skill_root.is_dir():
        print(f"no skill bundle at {skill_root}", file=sys.stderr)
        print("This is expected in a standalone checkout: client/ is then "
              "the only copy and there is nothing to sync.", file=sys.stderr)
        return 2

    rows = compare(skill_root)
    drifted = [r for r in rows if r[2] != "same"]

    for src, dst, state in rows:
        rel = dst.relative_to(ROOT).as_posix()
        if state == "same":
            print(f"  ok       {rel}  ({digest(dst)})")
        elif state == "differs":
            print(f"  DIFFERS  {rel}  "
                  f"(client {digest(dst)} vs skill {digest(src)})")
        elif state == "missing-dest":
            print(f"  MISSING  {rel}  (not vendored yet)")
        else:
            print(f"  NO SRC   {rel}  (not in {skill_root})")

    if not drifted:
        print("\nclient/ matches the skill bundle.")
        return 0

    if args.check:
        print(f"\n{len(drifted)} file(s) out of step. The coordinator would "
              "parse with a different vintage of the skill than the bench "
              "host runs.")
        print("Fix with: python tools/sync_client.py")
        if args.diff:
            for src, dst, state in drifted:
                if state == "differs":
                    print()
                    show_diff(src, dst)
        return 1

    for src, dst, state in drifted:
        if state == "no-src" or state == "missing-source":
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"  copied   {dst.relative_to(ROOT).as_posix()}")
    print("\nSynced. Re-run the server test suite: the golden-file tests "
          "parse through this copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
