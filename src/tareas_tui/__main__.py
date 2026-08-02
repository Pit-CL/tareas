"""Punto de entrada. Códigos de salida: 0 ok · 2 configuración (no reintentar)."""

from __future__ import annotations

import os
import shutil
import sys


def main() -> int:
    from .app import TareasApp
    from .datos import Backend, BackendDemo

    if os.environ.get("TAREAS_DEMO"):
        TareasApp(BackendDemo()).run()
        return 0

    if shutil.which("gh") is None:
        print("tareas: necesita GitHub CLI (https://cli.github.com)", file=sys.stderr)
        return 2

    from .config import ErrorConfig, cargar

    try:
        config = cargar()
    except ErrorConfig as err:
        print(f"tareas: {err}", file=sys.stderr)
        return 2

    TareasApp(Backend(config)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
