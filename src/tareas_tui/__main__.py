"""Punto de entrada. Códigos de salida: 0 ok · 2 configuración (no reintentar)."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import date


def _analizar_fecha(valor: str) -> str:
    """Valida `--due` y lo devuelve en ISO, que es lo que pide la API.

    Acepta `YYYY-MM-DD` (igual que la TUI) y `DD-MM-YYYY` (formato local), a
    diferencia de `datos.parsear_fecha`: esa es tolerante a propósito -un valor
    corrupto en el caché no puede tumbar la app-, y acá el CLI necesita lo
    contrario, que un valor mal escrito falle con un mensaje claro.
    """
    try:
        return date.fromisoformat(valor).isoformat()
    except ValueError:
        pass
    try:
        dia, mes, ano = valor.split("-")
        return date(int(ano), int(mes), int(dia)).isoformat()
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(
            f"invalid date {valor!r} (use YYYY-MM-DD or DD-MM-YYYY)"
        ) from None


def _cmd_add(argv: list[str]) -> int:
    """`tareas add`: crea una tarea sin abrir la TUI, para scripts y agentes.

    Reusa `Backend.crear` tal cual la usa la TUI -mismas mutaciones GraphQL, mismo
    caché de node IDs-, así que un alta por acá y una por la app dejan el Project
    exactamente igual. Cualquier falla sale con el código 2: el bucle de reinicio
    de `bin/tareas` relanza todo lo que no sea 0, 2, 130 o 143, y reintentar un
    alta que ya escribió en GitHub duplicaría el issue en vez de arreglar nada.
    """
    analizador = argparse.ArgumentParser(
        prog="tareas add", description="Create a task without opening the TUI."
    )
    analizador.add_argument("titulo", metavar="title", help="task title")
    analizador.add_argument(
        "--repo", help="owner/repo, or just repo (uses the configured owner)"
    )
    analizador.add_argument(
        "--due", type=_analizar_fecha, help="due date: YYYY-MM-DD or DD-MM-YYYY"
    )
    analizador.add_argument(
        "--notes",
        default="",
        help='task notes/description; pass "-" to read them from stdin '
        "(for long or multiline notes, e.g. a heredoc)",
    )
    args = analizador.parse_args(argv)  # argparse sale con 2 ante argumentos inválidos

    # Un agente que arma specs completas en markdown no puede confiar el quoting de
    # comillas, backticks o saltos de línea a un solo argumento de shell; leerlas de
    # stdin (heredoc típico) es la vía robusta para notas largas.
    notas = sys.stdin.read() if args.notes == "-" else args.notes

    if shutil.which("gh") is None:
        print("tareas: needs GitHub CLI (https://cli.github.com)", file=sys.stderr)
        return 2

    from .config import ErrorConfig, cargar
    from .datos import Backend, ErrorGh, ErrorParcial

    try:
        config = cargar()
    except ErrorConfig as err:
        print(f"tareas: {err}", file=sys.stderr)
        return 2

    async def _crear():
        backend = Backend(config)
        repo = args.repo
        if repo:
            if "/" not in repo:
                repo = f"{config.owner}/{repo}"
        else:
            # Mismo default contextual que la TUI: el repo del cwd si `gh` lo resuelve.
            repo = await backend.repo_actual()
            if not repo:
                raise ErrorConfig(
                    "couldn't detect a repo from the current directory — pass --repo"
                )
        return await backend.crear(repo, args.titulo, args.due, notas.strip())

    try:
        creada = asyncio.run(_crear())
    except ErrorParcial as err:
        # El issue puede haber quedado creado a medias: el mensaje ya trae su URL,
        # así que decir "couldn't create" acá encima sería mentir.
        print(f"tareas: {err}", file=sys.stderr)
        return 2
    except (ErrorConfig, ErrorGh, OSError) as err:
        print(f"tareas: {err}", file=sys.stderr)
        return 2

    vencimiento = f" · due {creada.vence.isoformat()}" if creada.vence else ""
    print(f"created {creada.cliente}{vencimiento} · {creada.url}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "add":
        return _cmd_add(argv[1:])

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
