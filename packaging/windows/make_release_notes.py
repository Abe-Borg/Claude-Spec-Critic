"""Write GitHub release notes from the matching version section in README.md.

Runs in the read-only build job. The publish job consumes only the resulting
text artifact, without checking out or executing repository code.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def release_notes(readme: str, version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:rc\d+)?", version):
        raise ValueError(f"Invalid release version: {version!r}")
    pattern = rf"^### v{re.escape(version)}[ \t]*\r?\n(.*?)(?=^### |\Z)"
    sections = re.findall(pattern, readme, flags=re.MULTILINE | re.DOTALL)
    if len(sections) != 1 or not sections[0].strip():
        raise ValueError(f"Expected one nonempty README changelog section for v{version}")
    return (
        f"Windows installer for Spec Critic v{version}.\n\n"
        "Download **SpecCriticSetup.exe** below and run it. The app is not "
        "code-signed, so Windows SmartScreen may warn on first run - choose "
        "**More info** then **Run anyway**.\n\n"
        "Existing installs will offer this update automatically.\n\n"
        + sections[0].strip()
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    notes = release_notes(args.readme.read_text(encoding="utf-8"), args.version)
    args.out.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
