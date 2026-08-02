"""Punto de entrada. Códigos de salida: 0 ok · 2 configuración (no reintentar)."""

from __future__ import annotations

import os
import shutil
import sys


def main() -> int:
    # Sin terminal de verdad textual no dibuja: se queda escribiendo secuencias ANSI
    # a un pipe para siempre (medido: 22 MB de stderr en 20 s) y ni ctrl-c la saca,
    # porque tampoco hay quien mande teclas. Pasa con `ssh host tareas` sin `-t`.
    # El código 2 es el que `bin/tareas` NO relanza: reintentar no arregla un pipe.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "tareas: needs an interactive terminal (with ssh, use `ssh -t host tareas`)",
            file=sys.stderr,
        )
        return 2

    from .app import TareasApp
    from .datos import Backend, BackendDemo

    if os.environ.get("TAREAS_DEMO"):
        TareasApp(BackendDemo()).run()
        return 0

    if shutil.which("gh") is None:
        print("tareas: needs GitHub CLI (https://cli.github.com)", file=sys.stderr)
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
