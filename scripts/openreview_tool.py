"""Keep the checkout CLI path working after moving the skill to skills/."""

from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    scripts = Path(__file__).resolve().parents[1] / "skills" / "openreview-author" / "scripts"
    sys.path.insert(0, str(scripts))
    runpy.run_path(str(scripts / "openreview_tool.py"), run_name="__main__")
