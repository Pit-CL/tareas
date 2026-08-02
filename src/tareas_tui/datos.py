"""Capa de datos: el GitHub Project se lee y escribe con `gh`, siempre asíncrono.

Todas las llamadas usan `asyncio.create_subprocess_exec`, así que la UI nunca se
congela esperando a la red. `BackendDemo` sirve datos ficticios para las capturas
del README y para las pruebas, sin tocar GitHub.
"""

from __future__ import annotations

import asyncio
import calendar
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .config import Config

ANCHO_VENCE = 9  # "hace 120d" es el string más largo que generamos


class ErrorGh(Exception):
    """`gh` terminó con error; el mensaje trae la última línea de stderr."""


async def gh(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    salida, error = await proc.communicate()
    if proc.returncode != 0:
        detalle = error.decode("utf-8", "replace").strip().splitlines()
        raise ErrorGh(detalle[-1][:200] if detalle else f"gh salió con {proc.returncode}")
    return salida.decode("utf-8", "replace")


@dataclass(frozen=True)
class Tarea:
    item_id: str
    repo: str  # "owner/nombre"
    numero: int
    titulo: str
    url: str
    cuerpo: str
    vence: date | None

    @property
    def cliente(self) -> str:
        """Etiqueta corta para la lista: sin el owner, que siempre es el mismo."""
        corto = self.repo.split("/", 1)[-1]
        return f"{corto}#{self.numero}"


def parsear_fecha(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------ formato (es-419)
def etiqueta_vencimiento(vence: date | None, hoy: date) -> tuple[str, str]:
    """Devuelve (texto, estilo rich). Máximo ANCHO_VENCE caracteres."""
    if vence is None:
        return "—", "dim"
    dias = (vence - hoy).days
    if dias < 0:
        return f"hace {-dias}d"[:ANCHO_VENCE], "bold red"
    if dias == 0:
        return "hoy", "bold yellow"
    if dias == 1:
        return "mañana", "yellow"
    if dias <= 7:
        return f"en {dias}d", "yellow"
    return f"en {dias}d"[:ANCHO_VENCE], "dim"


def fecha_larga(vence: date | None, hoy: date) -> str:
    if vence is None:
        return "sin fecha de vencimiento"
    dias = (vence - hoy).days
    if dias < 0:
        cola = f"atrasada por {-dias} día{'s' if -dias != 1 else ''}"
    elif dias == 0:
        cola = "vence hoy"
    elif dias == 1:
        cola = "vence mañana"
    else:
        cola = f"faltan {dias} días"
    return f"{vence.strftime('%d-%m-%Y')} · {cola}"


def acortar(texto: str, ancho: int) -> str:
    """Trunca con puntos suspensivos: el ancho del pane manda, nada desborda."""
    if ancho <= 0:
        return ""
    return texto if len(texto) <= ancho else texto[: max(1, ancho - 1)] + "…"


def mas_un_mes(d: date) -> date:
    ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


# ------------------------------------------------------------------ backends
class Backend:
    """Lee y escribe el Project real con `gh`."""

    def __init__(self, config: Config) -> None:
        self.config = config

    async def listar(self) -> list[Tarea]:
        cfg = self.config
        crudo = await gh(
            "project", "item-list", cfg.project,
            "--owner", cfg.owner, "--format", "json", "--limit", "200",
        )
        tareas: list[Tarea] = []
        for item in json.loads(crudo or "{}").get("items", []):
            if str(item.get("status", "")).casefold() == cfg.estado_hecho.casefold():
                continue
            contenido = item.get("content") or {}
            if contenido.get("type") != "Issue":
                continue
            tareas.append(
                Tarea(
                    item_id=item.get("id", ""),
                    repo=contenido.get("repository", ""),
                    numero=int(contenido.get("number", 0)),
                    titulo=(contenido.get("title") or item.get("title") or "(sin título)").strip(),
                    url=contenido.get("url", ""),
                    cuerpo=(contenido.get("body") or "").strip(),
                    vence=parsear_fecha(valor_campo(item, cfg.campo_fecha)),
                )
            )
        return ordenar(tareas)

    async def repos(self) -> list[str]:
        crudo = await gh(
            "repo", "list", self.config.owner, "--limit", "200", "--json", "nameWithOwner"
        )
        return sorted(r["nameWithOwner"] for r in json.loads(crudo or "[]"))

    async def crear(self, repo: str, titulo: str, fecha: str | None) -> None:
        salida = await gh(
            "issue", "create", "--repo", repo, "--title", titulo,
            "--body", self.config.cuerpo_nuevo,
        )
        url = salida.strip().splitlines()[-1]
        item = (
            await gh(
                "project", "item-add", self.config.project,
                "--owner", self.config.owner, "--url", url,
                "--format", "json", "--jq", ".id",
            )
        ).strip()
        if fecha:
            await self.fechar(item, fecha)

    async def cerrar(self, tarea: Tarea) -> None:
        await gh("issue", "close", str(tarea.numero), "--repo", tarea.repo)

    async def fechar(self, item_id: str, fecha: str | None) -> None:
        """`fecha` vacía o None limpia el vencimiento."""
        base = [
            "project", "item-edit", "--id", item_id,
            "--project-id", self.config.project_id,
            "--field-id", self.config.campo_fecha_id,
        ]
        await gh(*base, "--date", fecha) if fecha else await gh(*base, "--clear")


def valor_campo(item: dict, nombre: str):
    """Busca el campo por nombre tolerando cómo lo normalice `gh`.

    `gh project item-list` aplana los campos personalizados con una clave derivada del
    nombre (para «Vencimiento» devuelve `vencimiento`), pero la forma exacta cambia si
    el nombre tiene espacios o mayúsculas. Comparamos sin espacios ni acentos de caja.
    """
    objetivo = nombre.replace(" ", "").casefold()
    for clave, valor in item.items():
        if clave.replace(" ", "").casefold() == objetivo:
            return valor
    return None


def ordenar(tareas: list[Tarea]) -> list[Tarea]:
    return sorted(tareas, key=lambda t: (t.vence or date(9999, 12, 31), t.titulo.casefold()))


_DEMO = (
    (31, "vela/landing", "Subir las fotos nuevas de la portada", -8),
    (12, "acme/web", "Renovar hosting y certificado SSL", -3),
    (48, "lumen/tienda", "Cambiar el flujo de pago del checkout", 0),
    (7, "nordic/erp", "Exportar las facturas del mes a XML", 2),
    (3, "vela/landing", "Ajustar los textos de la portada", 5),
    (21, "korta/api", "Migrar los webhooks a la versión 2", 19),
    (9, "mesa/intranet", "Revisar permisos por rol de usuario", None),
)


class BackendDemo(Backend):
    """Datos ficticios: alimenta las capturas del README y las pruebas."""

    def __init__(self) -> None:  # noqa: D107 - no necesita Config
        hoy = date.today()
        self._tareas = ordenar(
            [
                Tarea(
                    item_id=f"demo-{numero}",
                    repo=repo,
                    numero=numero,
                    titulo=titulo,
                    url=f"https://example.com/{repo}/issues/{numero}",
                    cuerpo=f"Pedido de ejemplo para la demo.\n\n- Detalle uno\n- Detalle dos",
                    vence=None if dias is None else hoy + timedelta(days=dias),
                )
                for numero, repo, titulo, dias in _DEMO
            ]
        )

    async def listar(self) -> list[Tarea]:
        return list(self._tareas)

    async def repos(self) -> list[str]:
        return ["acme/web", "korta/api", "lumen/tienda", "mesa/intranet", "vela/landing"]

    async def crear(self, repo: str, titulo: str, fecha: str | None) -> None:
        numero = max((t.numero for t in self._tareas), default=0) + 1
        self._tareas = ordenar(
            [
                *self._tareas,
                Tarea(
                    item_id=f"demo-{numero}",
                    repo=repo,
                    numero=numero,
                    titulo=titulo,
                    url=f"https://example.com/{repo}/issues/{numero}",
                    cuerpo="Creada en la demo.",
                    vence=parsear_fecha(fecha),
                ),
            ]
        )

    async def cerrar(self, tarea: Tarea) -> None:
        self._tareas = [t for t in self._tareas if t.item_id != tarea.item_id]

    async def fechar(self, item_id: str, fecha: str | None) -> None:
        self._tareas = ordenar(
            [
                replace(t, vence=parsear_fecha(fecha)) if t.item_id == item_id else t
                for t in self._tareas
            ]
        )


def vacio_demo() -> BackendDemo:
    """Demo sin tareas, para retratar el estado vacío."""
    backend = BackendDemo()
    backend._tareas = []
    return backend
