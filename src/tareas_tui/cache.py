"""Caché en disco de la última lectura buena, para pintar en el primer frame.

Arrancar costaba ~1,3 s mirando «loading tasks…»: las tres llamadas a `gh` del
arranque (`project item-list`, `repo list`, `repo view`) son red pura y ninguna
vuelve antes de ~0,3 s. Acá se guarda lo último que sí llegó, así la lista se
pinta con datos apenas monta la pantalla y el refresco real corre por detrás.

El archivo vive junto a `ids-cache.json` (mismo directorio de config) y es
**desechable**: cualquier error de lectura o escritura se traga y la app vuelve a
comportarse como antes, pidiéndole todo a `gh`. Borrarlo nunca rompe nada.

La escritura es atómica (archivo temporal + `os.replace`) para que un Ctrl-C en el
momento justo no deje un JSON a medias que después haya que descartar.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from .config import dir_config

#: Sube cuando cambia la forma del archivo: un caché de otra versión se ignora
#: entero en vez de intentar migrarlo (es desechable, no hay nada que perder).
VERSION = 1


def ruta() -> Path:
    return dir_config() / "datos-cache.json"


def _leer_archivo() -> dict:
    try:
        datos = json.loads(ruta().read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(datos, dict) or datos.get("version") != VERSION:
        return {}
    return datos


def leer(clave: str) -> dict:
    """Lo cacheado para ese Project, o `{}` si no hay nada utilizable."""
    entrada = _leer_archivo().get(clave)
    return entrada if isinstance(entrada, dict) else {}


def escribir(clave: str, campos: dict) -> None:
    """Mezcla `campos` en la entrada del Project y reescribe el archivo.

    Nunca levanta: el caché es una optimización, no un requisito de la app.
    """
    datos = _leer_archivo()
    datos["version"] = VERSION
    entrada = datos.get(clave)
    if not isinstance(entrada, dict):
        entrada = {}
    entrada.update(campos)
    datos[clave] = entrada

    destino = ruta()
    temporal = destino.with_suffix(".tmp")
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal.write_text(json.dumps(datos), "utf-8")
        os.replace(temporal, destino)
    except OSError:
        with contextlib.suppress(OSError):
            temporal.unlink()
