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

    Acepta exactamente lo mismo que los campos de fecha de la TUI (`YYYY-MM-DD`,
    `today`, `tom`, `fri`, `+10d`, `aug 20`: ver `datos.interpretar_fecha`) más
    `DD-MM-YYYY`, el formato local que se escribe de memoria y que la TUI no necesita
    porque ahí se elige con los quick-picks.

    Va como `type=` de argparse a propósito: así una fecha mal escrita sale por stderr
    con código 2 ANTES de la primera llamada a `gh`, igual que el título vacío.

    `datos.interpretar_fecha` y no `datos.parsear_fecha`: esta última es tolerante a
    propósito -un valor corrupto en el caché no puede tumbar la app- y acá hace falta
    lo contrario, que lo que está mal escrito falle con un mensaje claro.
    """
    # Import local, como el resto del módulo: los caminos que salen temprano (sin
    # terminal, sin `gh`, sin config) no tienen por qué pagar el arranque de `rich`.
    from .datos import interpretar_fecha

    fecha = interpretar_fecha(valor, date.today())
    if fecha is not None:
        return fecha.isoformat()
    try:
        dia, mes, ano = valor.split("-")
        return date(int(ano), int(mes), int(dia)).isoformat()
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(
            f"invalid date {valor!r} (use YYYY-MM-DD, DD-MM-YYYY, or plain English "
            "like today, fri, +10d, aug 20)"
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
        "--due",
        type=_analizar_fecha,
        help="due date: YYYY-MM-DD, DD-MM-YYYY, or plain English "
        "(today, tom, fri, +10d, +2w, aug 20)",
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

    # Mismo criterio que `NuevaScreen._crear`: un título vacío (o solo espacios, típico
    # de una plantilla mal armada) no debe llegar a crear un issue en GitHub. Se corta
    # acá, antes de cualquier llamada a `gh`.
    titulo = args.titulo.strip()
    if not titulo:
        print("tareas: title is required", file=sys.stderr)
        return 2

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
        return await backend.crear(repo, titulo, args.due, notas.strip())

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
