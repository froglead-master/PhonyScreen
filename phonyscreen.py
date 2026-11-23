"""PhonyScreen entry point (also the PyInstaller entry for the .exe)."""

import sys

from phonyscreen.launcher import main

if __name__ == "__main__":
    sys.exit(main())
