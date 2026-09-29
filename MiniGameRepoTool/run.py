"""Source checkout entry point; install dependencies with Install.bat first."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from repo_tool.app import main

if __name__ == "__main__":
    raise SystemExit(main())
