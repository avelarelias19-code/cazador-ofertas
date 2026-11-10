"""Punto de entrada para `python -m ofertas_monitor`."""
from __future__ import annotations

import sys

from ofertas_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

