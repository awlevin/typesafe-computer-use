"""`python -m typesafe_computer_use ...`, for when the installed clicker/clicker-inspect exes are blocked.

`clicker` args pass straight through; prefix with `inspect` to reach clicker-inspect instead.
"""

from __future__ import annotations

import sys

from .cli import inspect, main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "inspect":
        inspect(argv[1:])
    else:
        main(argv)
