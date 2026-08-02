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
        raise ErrorGh(detalle[-1][:200] if detalle else f"gh exited with {proc.returncode}")
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


# ------------------------------------------------------------------ format (en)
def etiqueta_vencimiento(vence: date | None, hoy: date) -> tuple[str, str]:
    """Devuelve (texto, estilo rich). Máximo ANCHO_VENCE caracteres."""
    if vence is None:
        return "—", "dim"
    dias = (vence - hoy).days
    if dias < 0:
        return f"{-dias}d ago"[:ANCHO_VENCE], "bold red"
    if dias == 0:
        return "today", "bold yellow"
    if dias == 1:
        return "tomorrow", "yellow"
    if dias <= 7:
        return f"in {dias}d", "yellow"
    return f"in {dias}d"[:ANCHO_VENCE], "dim"


def fecha_larga(vence: date | None, hoy: date) -> str:
    if vence is None:
        return "no due date"
    dias = (vence - hoy).days
    if dias < 0:
        cola = f"{-dias} day{'s' if -dias != 1 else ''} overdue"
    elif dias == 0:
        cola = "due today"
    elif dias == 1:
        cola = "due tomorrow"
    else:
        cola = f"{dias} days left"
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

    @property
    def titulo_project(self) -> str:
        return self.config.project_title

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
                    titulo=(contenido.get("title") or item.get("title") or "(untitled)").strip(),
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

    async def repo_actual(self) -> str | None:
        """Repo GitHub del directorio donde se lanzó la app, o None si no aplica.

        Cubre estar fuera de un repo git, un repo sin remote de GitHub o sin `gh`
        autenticado ahí: cualquier falla de `gh` cae en modo todas, en silencio.
        """
        try:
            crudo = await gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        except ErrorGh:
            return None
        return crudo.strip() or None

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
    (31, "vela/landing", "Upload the new homepage photos", -8),
    (12, "acme/web", "Renew hosting and SSL certificate", -3),
    (48, "lumen/shop", "Change the checkout payment flow", 0),
    (7, "nordic/erp", "Export this month's invoices to XML", 2),
    (3, "vela/landing", "Tweak the homepage copy", 5),
    (21, "korta/api", "Migrate webhooks to version 2", 19),
    (9, "mesa/intranet", "Review permissions by user role", None),
)


class BackendDemo(Backend):
    """Datos ficticios: alimenta las capturas del README y las pruebas."""

    def __init__(self, repo_actual: str | None = None, project_title: str = "Client Tasks") -> None:
        """`repo_actual` simula estar parado en ese repo (modo repo en la demo)."""
        hoy = date.today()
        self._repo_actual = repo_actual
        self._project_title = project_title
        self._tareas = ordenar(
            [
                Tarea(
                    item_id=f"demo-{numero}",
                    repo=repo,
                    numero=numero,
                    titulo=titulo,
                    url=f"https://example.com/{repo}/issues/{numero}",
                    cuerpo="Sample request for the demo.\n\n- Detail one\n- Detail two",
                    vence=None if dias is None else hoy + timedelta(days=dias),
                )
                for numero, repo, titulo, dias in _DEMO
            ]
        )

    @property
    def titulo_project(self) -> str:
        return self._project_title

    async def listar(self) -> list[Tarea]:
        return list(self._tareas)

    async def repos(self) -> list[str]:
        return ["acme/web", "korta/api", "lumen/shop", "mesa/intranet", "vela/landing"]

    async def repo_actual(self) -> str | None:
        return self._repo_actual

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
                    cuerpo="Created in the demo.",
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


def vacio_demo(repo_actual: str | None = None) -> BackendDemo:
    """Demo sin tareas, para retratar el estado vacío (también en modo repo)."""
    backend = BackendDemo(repo_actual=repo_actual)
    backend._tareas = []
    return backend
