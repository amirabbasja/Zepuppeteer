"""Move a Brave display-name profile into profiles/<name>/src."""

from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zepuppeteer.profile_transfer import transfer_main


if __name__ == "__main__":
    transfer_main("import")
