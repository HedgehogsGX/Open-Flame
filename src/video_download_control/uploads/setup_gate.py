"""Private process-ownership handshake for explicit runtime setup commands."""
import argparse
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    deadline = time.monotonic() + 15
    while not args.gate.is_file():
        if time.monotonic() > deadline:
            return 1
        time.sleep(.02)
    return subprocess.run(args.command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
