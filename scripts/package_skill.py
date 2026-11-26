"""Build an uploadable Agent Skills ZIP from the repository's runtime files."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = "openreview-author"
SKILL_SOURCE = ROOT / "skills" / SKILL_NAME
SKILL_FILES = (
    "SKILL.md",
    "requirements.txt",
    "agents/openai.yaml",
    "assets/icon.svg",
    "references/openreview-api.md",
    "scripts/openreview_tool.py",
    "scripts/or_core.py",
    "scripts/or_replies.py",
)


def main():
    for name in SKILL_FILES:
        source = SKILL_SOURCE / name
        if source.resolve() != source or not source.is_file():
            raise SystemExit(f"Expected a regular skill file at {name}")

    output = ROOT / "dist" / f"{SKILL_NAME}.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in SKILL_FILES:
            archive.write(SKILL_SOURCE / name, f"{SKILL_NAME}/{name}")
    print(output)


if __name__ == "__main__":
    main()
