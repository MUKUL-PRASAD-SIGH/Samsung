"""PyInstaller entry point for the desktop app. Kept outside the `agent` package so the spec has a plain script to freeze."""
import sys

from agent.desktop import main

if __name__ == "__main__":
    sys.exit(main())
