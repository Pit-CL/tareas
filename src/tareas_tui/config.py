"""Configuración local y resolución de los identificadores del GitHub Project.

La app no trae nada codificado: owner, número de project y nombre del campo de fecha
salen de `~/.config/tareas/config.toml`. Los node IDs internos del Project (que son
largos y opacos) se resuelven con `gh` la primera vez y quedan cacheados al lado.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]


class ErrorConfig(Exception):
    """Falta configuración o no se pudo resolver el Project."""


def dir_config() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tareas"


def ruta_config() -> Path:
    return dir_config() / "config.toml"


def ruta_cache() -> Path:
    return dir_config() / "ids-cache.json"


EJEMPLO = """\
# ~/.config/tareas/config.toml

# GitHub user or organization that owns the Project (v2).
owner = "my-user"

# Project number; it's the one at the end of its URL.
project = 1

# Name of the Date-type field that marks the due date.
campo_fecha = "Due date"

# Name of the Status option that counts as done.
estado_hecho = "Done"

# Body text set on the issue when you create it from the TUI.
cuerpo_nuevo = "Created from the tareas TUI."
"""


@dataclass(frozen=True)
class Config:
    owner: str
    project: str
    campo_fecha: str
    estado_hecho: str
    cuerpo_nuevo: str
    project_id: str
    campo_fecha_id: str
    project_title: str


def _leer_toml() -> dict:
    ruta = ruta_config()
    if tomllib is None:
        raise ErrorConfig("tareas needs Python 3.11 or higher (uses tomllib).")
    if not ruta.is_file():
        raise ErrorConfig(
            f"I couldn't find {ruta}.\n\n"
            "Create that file with this content and adjust the values:\n\n"
            f"{EJEMPLO}"
        )
    try:
        with ruta.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as err:
        raise ErrorConfig(f"{ruta} is not valid TOML: {err}") from err


def _gh(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError as err:
        raise ErrorConfig("I couldn't find the `gh` command (install GitHub CLI).") from err
    except subprocess.TimeoutExpired as err:
        raise ErrorConfig("`gh` didn't respond in time.") from err
    if proc.returncode != 0:
        detalle = (proc.stderr or "").strip().splitlines()
        raise ErrorConfig(detalle[-1] if detalle else f"`gh` failed ({proc.returncode}).")
    return proc.stdout


def _resolver_ids(owner: str, project: str, campo_fecha: str) -> tuple[str, str, str]:
    """Pregunta a gh por el id del Project, el del campo de fecha y el título del Project."""
    vista = json.loads(_gh("project", "view", project, "--owner", owner, "--format", "json"))
    project_id = vista.get("id")
    if not project_id:
        raise ErrorConfig(f"the Project {owner}/{project} didn't return an id.")
    titulo = str(vista.get("title") or "tasks")

    campos = json.loads(
        _gh("project", "field-list", project, "--owner", owner, "--format", "json", "--limit", "50")
    ).get("fields", [])
    for campo in campos:
        if campo.get("name", "").casefold() == campo_fecha.casefold():
            return project_id, campo["id"], titulo

    nombres = ", ".join(c.get("name", "?") for c in campos) or "(none)"
    raise ErrorConfig(
        f'the Project has no field named "{campo_fecha}".\nAvailable fields: {nombres}'
    )


def cargar(refrescar: bool = False) -> Config:
    """Config lista para usar; resuelve y cachea los IDs la primera vez."""
    datos = _leer_toml()
    faltan = [clave for clave in ("owner", "project") if not datos.get(clave)]
    if faltan:
        raise ErrorConfig(f"{ruta_config()} is missing keys: {', '.join(faltan)}")

    owner = str(datos["owner"])
    project = str(datos["project"])
    campo_fecha = str(datos.get("campo_fecha", "Due date"))
    clave = f"{owner}/{project}/{campo_fecha}"

    cache: dict = {}
    ruta = ruta_cache()
    if not refrescar and ruta.is_file():
        try:
            cache = json.loads(ruta.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    guardado = cache.get(clave)
    if guardado:
        project_id, campo_id = guardado["project_id"], guardado["campo_fecha_id"]
        titulo_proyecto = guardado.get("project_title", "tasks")
    else:
        project_id, campo_id, titulo_proyecto = _resolver_ids(owner, project, campo_fecha)
        cache[clave] = {
            "project_id": project_id,
            "campo_fecha_id": campo_id,
            "project_title": titulo_proyecto,
        }
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(json.dumps(cache, indent=2), "utf-8")
        except OSError:
            pass  # sin cache anda igual, solo cuesta dos llamadas más al arrancar

    return Config(
        owner=owner,
        project=project,
        campo_fecha=campo_fecha,
        estado_hecho=str(datos.get("estado_hecho", "Done")),
        cuerpo_nuevo=str(datos.get("cuerpo_nuevo", "Created from the tareas TUI.")),
        project_id=project_id,
        campo_fecha_id=campo_id,
        project_title=titulo_proyecto,
    )
