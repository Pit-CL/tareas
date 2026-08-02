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

# Usuario u organización dueña del GitHub Project (v2).
owner = "mi-usuario"

# Número del Project; es el que aparece al final de su URL.
project = 1

# Nombre del campo de tipo Date que marca el vencimiento.
campo_fecha = "Vencimiento"

# Nombre de la opción de Status que cuenta como terminada.
estado_hecho = "Done"

# Cuerpo que se le pone al issue cuando lo creas desde la TUI.
cuerpo_nuevo = "Creada desde la TUI de tareas."
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


def _leer_toml() -> dict:
    ruta = ruta_config()
    if tomllib is None:
        raise ErrorConfig("tareas necesita Python 3.11 o superior (usa tomllib).")
    if not ruta.is_file():
        raise ErrorConfig(
            f"no encontré {ruta}.\n\n"
            "Crea ese archivo con este contenido y ajusta los valores:\n\n"
            f"{EJEMPLO}"
        )
    try:
        with ruta.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as err:
        raise ErrorConfig(f"{ruta} no es TOML válido: {err}") from err


def _gh(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError as err:
        raise ErrorConfig("no encontré el comando `gh` (instala GitHub CLI).") from err
    except subprocess.TimeoutExpired as err:
        raise ErrorConfig("`gh` no respondió a tiempo.") from err
    if proc.returncode != 0:
        detalle = (proc.stderr or "").strip().splitlines()
        raise ErrorConfig(detalle[-1] if detalle else f"`gh` falló ({proc.returncode}).")
    return proc.stdout


def _resolver_ids(owner: str, project: str, campo_fecha: str) -> tuple[str, str]:
    """Pregunta a gh por el id del Project y el del campo de fecha."""
    vista = json.loads(_gh("project", "view", project, "--owner", owner, "--format", "json"))
    project_id = vista.get("id")
    if not project_id:
        raise ErrorConfig(f"el Project {owner}/{project} no devolvió un id.")

    campos = json.loads(
        _gh("project", "field-list", project, "--owner", owner, "--format", "json", "--limit", "50")
    ).get("fields", [])
    for campo in campos:
        if campo.get("name", "").casefold() == campo_fecha.casefold():
            return project_id, campo["id"]

    nombres = ", ".join(c.get("name", "?") for c in campos) or "(ninguno)"
    raise ErrorConfig(
        f"el Project no tiene un campo llamado «{campo_fecha}».\nCampos disponibles: {nombres}"
    )


def cargar(refrescar: bool = False) -> Config:
    """Config lista para usar; resuelve y cachea los IDs la primera vez."""
    datos = _leer_toml()
    faltan = [clave for clave in ("owner", "project") if not datos.get(clave)]
    if faltan:
        raise ErrorConfig(f"a {ruta_config()} le faltan claves: {', '.join(faltan)}")

    owner = str(datos["owner"])
    project = str(datos["project"])
    campo_fecha = str(datos.get("campo_fecha", "Vencimiento"))
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
    else:
        project_id, campo_id = _resolver_ids(owner, project, campo_fecha)
        cache[clave] = {"project_id": project_id, "campo_fecha_id": campo_id}
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
        cuerpo_nuevo=str(datos.get("cuerpo_nuevo", "Creada desde la TUI de tareas.")),
        project_id=project_id,
        campo_fecha_id=campo_id,
    )
