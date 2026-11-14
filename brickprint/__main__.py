import sys

try:
    import numpy  # noqa: F401  (every module needs it; fail with a clear message)
except ImportError:
    # same "label<2+ spaces>value" lines as `doctor`
    print(f"python      {sys.version.split()[0]}")
    print(f"python bin  {sys.executable}")
    print("numpy       (missing)")
    print("ERROR: numpy is not importable by this Python. Run through bin/brickprint "
          "(it picks Blender's bundled Python, which has numpy) or set BRICKPRINT_PYTHON "
          "to an interpreter with numpy.")
    raise SystemExit(1)

from .cli import main

raise SystemExit(main())
