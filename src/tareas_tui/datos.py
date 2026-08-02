"""Capa de datos: el GitHub Project se lee y escribe con `gh`, siempre asíncrono.

Todas las llamadas usan `asyncio.create_subprocess_exec`, así que la UI nunca se
congela esperando a la red. `BackendDemo` sirve datos ficticios para las capturas
del README y para las pruebas, sin tocar GitHub.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .config import Config

ANCHO_VENCE = 9  # "hace 120d" es el string más largo que generamos
ANCHO_REPEAT = 1  # la marca ↻ de las tareas repetitivas

# Intervalos de repetición, en el orden en que los cicla el chip del modal.
REPETICIONES: tuple[str, ...] = ("none", "daily", "weekly", "biweekly", "monthly")

# La repetición viaja en el propio cuerpo del issue como comentario HTML: GitHub no
# lo muestra al renderizar el markdown, así que el usuario ve solo sus notas y no
# hace falta ningún campo extra en el Project.
MARCA_REPEAT = "<!-- tareas:repeat={} -->"
_RE_MARCA = re.compile(r"[ \t]*<!--\s*tareas:repeat=([a-zA-Z]+)\s*-->[ \t]*\n?")

_PASOS_DIAS = {"daily": 1, "weekly": 7, "biweekly": 14}


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
    cuerpo: str  # notas visibles, ya sin el metadato de repetición
    vence: date | None
    repeat: str | None = None  # None, o uno de REPETICIONES distinto de "none"

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


# ------------------------------------------------------------------ repetición
def separar_repeticion(cuerpo: str | None) -> tuple[str, str | None]:
    """Parte el cuerpo del issue en (notas visibles, intervalo de repetición)."""
    texto = cuerpo or ""
    encontrado = _RE_MARCA.search(texto)
    repeat: str | None = None
    if encontrado:
        valor = encontrado.group(1).casefold()
        if valor in REPETICIONES and valor != "none":
            repeat = valor
    return _RE_MARCA.sub("", texto).strip(), repeat


def componer_cuerpo(notas: str | None, repeat: str | None) -> str:
    """Cuerpo del issue: las notas del usuario más el metadato invisible si repite."""
    limpio = (notas or "").strip()
    if not repeat or repeat == "none":
        return limpio
    marca = MARCA_REPEAT.format(repeat)
    return f"{limpio}\n\n{marca}" if limpio else marca


def mas_meses(d: date, meses: int) -> date:
    """`meses` meses calendario después de `d`, recortando al último día del mes."""
    total = d.month - 1 + meses
    ano, mes = d.year + total // 12, total % 12 + 1
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


def mas_un_mes(d: date) -> date:
    return mas_meses(d, 1)


def avanzar(base: date, repeat: str, veces: int) -> date:
    """`veces` intervalos después de `base`.

    Monthly cuenta siempre desde el día original, nunca desde el ya recortado: así
    31-ene + 2 meses da 31-mar y no 28-mar.
    """
    if repeat == "monthly":
        return mas_meses(base, veces)
    paso = _PASOS_DIAS.get(repeat)
    if paso is None:
        raise ValueError(f"unknown repeat interval: {repeat!r}")
    return base + timedelta(days=paso * veces)


def proxima_fecha(base: date, repeat: str, hoy: date) -> date:
    """Siguiente vencimiento después de `base`, siempre posterior a `hoy`.

    El catch-up importa cuando la tarea se cierra tarde: una semanal vencida hace un
    mes no debe reaparecer ya vencida, sino en su próxima ocurrencia futura. El salto
    se estima de una vez (no día a día) y el bucle solo ajusta el borde.
    """
    if repeat == "monthly":
        veces = max(1, (hoy.year - base.year) * 12 + hoy.month - base.month)
    elif repeat in _PASOS_DIAS:
        veces = max(1, (hoy - base).days // _PASOS_DIAS[repeat])
    else:
        raise ValueError(f"unknown repeat interval: {repeat!r}")
    while avanzar(base, repeat, veces) <= hoy:
        veces += 1
    return avanzar(base, repeat, veces)


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
            cuerpo, repeat = separar_repeticion(contenido.get("body"))
            tareas.append(
                Tarea(
                    item_id=item.get("id", ""),
                    repo=contenido.get("repository", ""),
                    numero=int(contenido.get("number", 0)),
                    titulo=(contenido.get("title") or item.get("title") or "(untitled)").strip(),
                    url=contenido.get("url", ""),
                    cuerpo=cuerpo,
                    vence=parsear_fecha(valor_campo(item, cfg.campo_fecha)),
                    repeat=repeat,
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

    async def crear(self, repo: str, titulo: str, fecha: str | None, cuerpo: str = "") -> None:
        salida = await gh(
            "issue", "create", "--repo", repo, "--title", titulo, "--body", cuerpo,
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

    async def repetir(self, tarea: Tarea, hoy: date) -> date:
        """Crea la siguiente ocurrencia de una tarea repetitiva; devuelve su fecha."""
        if not tarea.repeat or tarea.vence is None:
            raise ValueError("the task doesn't repeat or has no due date")
        proxima = proxima_fecha(tarea.vence, tarea.repeat, hoy)
        await self.crear(
            tarea.repo, tarea.titulo, proxima.isoformat(),
            componer_cuerpo(tarea.cuerpo, tarea.repeat),
        )
        return proxima

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
    (31, "vela/landing", "Upload the new homepage photos", -8, None),
    (12, "acme/web", "Renew hosting and SSL certificate", -3, "monthly"),
    (48, "lumen/shop", "Change the checkout payment flow", 0, None),
    (7, "nordic/erp", "Export this month's invoices to XML", 2, "monthly"),
    (3, "vela/landing", "Tweak the homepage copy", 5, None),
    (21, "korta/api", "Migrate webhooks to version 2", 19, None),
    (9, "mesa/intranet", "Review permissions by user role", None, None),
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
                    repeat=repeat,
                )
                for numero, repo, titulo, dias, repeat in _DEMO
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

    async def crear(self, repo: str, titulo: str, fecha: str | None, cuerpo: str = "") -> None:
        # Igual que el backend real: el cuerpo entra crudo y la repetición se vuelve a
        # leer de ahí, así la demo ejercita el mismo ida y vuelta del metadato.
        notas, repeat = separar_repeticion(cuerpo)
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
                    cuerpo=notas,
                    vence=parsear_fecha(fecha),
                    repeat=repeat,
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
