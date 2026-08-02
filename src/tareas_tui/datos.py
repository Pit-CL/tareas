"""Capa de datos: el GitHub Project se lee y escribe con `gh`, siempre asíncrono.

Todas las llamadas usan `asyncio.create_subprocess_exec`, así que la UI nunca se
congela esperando a la red. `BackendDemo` sirve datos ficticios para las capturas
del README y para las pruebas, sin tocar GitHub.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from rich.color import Color as ColorRich
from rich.style import Style

from .config import Config

ANCHO_VENCE = 9  # "hace 120d" es el string más largo que generamos
ANCHO_REPEAT = 1  # la marca ↻ de las tareas repetitivas

# Estilo del texto secundario que igual se LEE: vencimientos lejanos o ausentes, repo de
# cada fila, hints accionables, timestamp del refresco. Reemplaza a `dim`, que se pinta
# mezclando texto y fondo y en esta paleta cae a 2,7:1 en claro y 4,0:1 en oscuro. El
# color 7 mide 7,38:1 en claro y 10,72:1 en oscuro, y sigue por debajo del texto normal
# (10,24:1 / 12,30:1), así que la jerarquía se mantiene. (`ansi_bright_black` no sirve:
# 1,4-1,7:1, ilegible.)
#
# Va como objeto Style y no como nombre porque los dos pipelines de render de Textual
# leen los nombres al revés: en una celda de DataTable (rich) "white" es el color 7 y
# "ansi_white" se ignora; en un Static (Content) "ansi_white" es el color 7 y "white"
# es un #ffffff fijo, que rompería la herencia de la paleta en un terminal claro.
SECUNDARIO = Style(color=ColorRich.from_ansi(7))

# Techo de espera de cualquier llamada a `gh`. Mismo valor que el `subprocess.run` de
# config.py: con la red muerta `gh` no vuelve nunca, y sin techo la app se quedaba en
# "loading tasks…" para siempre, sumando un proceso huérfano por ciclo de refresco.
TIMEOUT_GH = 30.0

# Items que pedimos del Project. GitHub aplica el límite ANTES de que descartemos las
# hechas, así que hay que dejar margen para las Done acumuladas; `Backend.truncado`
# avisa si aun así tocamos el techo.
LIMITE_ITEMS = 1000

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


class ErrorParcial(ErrorGh):
    """La operación dejó un efecto a medias en GitHub.

    Una alta son dos o tres llamadas a `gh` que no son atómicas: si falla la segunda,
    el issue ya existe. Decir "couldn't create the task" empuja al usuario a reintentar
    y a duplicar, así que estos errores llevan el mensaje de lo que SÍ quedó hecho.
    """


async def _matar(proc: asyncio.subprocess.Process) -> None:
    """Mata el `gh` en curso: cancelar el worker no mata su subproceso."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(asyncio.CancelledError):
        await proc.wait()


async def gh(*args: str) -> str:
    limite = TIMEOUT_GH  # se lee acá para que un test pueda bajarlo
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        salida, error = await asyncio.wait_for(proc.communicate(), limite)
    except TimeoutError as err:
        await _matar(proc)
        raise ErrorGh(
            f"`gh` didn't respond in {limite:.0f}s (check your connection)."
        ) from err
    except asyncio.CancelledError:
        await _matar(proc)
        raise
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
def etiqueta_vencimiento(vence: date | None, hoy: date) -> tuple[str, str | Style]:
    """Devuelve (texto, estilo rich). Máximo ANCHO_VENCE caracteres.

    La jerarquía se lee de un vistazo: vencida (rojo bold) > hoy (acento bold) >
    próxima (acento) > lejana o sin fecha (color 7). Ninguna va en `dim`: el
    vencimiento es el dato más importante de la fila, y `dim` lo dejaba en lo más
    lavado de la pantalla justo cuando hay que leerlo.
    """
    if vence is None:
        return "—", SECUNDARIO
    dias = (vence - hoy).days
    if dias < 0:
        return f"{-dias}d ago"[:ANCHO_VENCE], "bold red"
    if dias == 0:
        return "today", "bold yellow"
    if dias == 1:
        return "tomorrow", "yellow"
    if dias <= 7:
        return f"in {dias}d", "yellow"
    return f"in {dias}d"[:ANCHO_VENCE], SECUNDARIO


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

    #: La última lectura tocó `LIMITE_ITEMS`: puede haber pendientes fuera de la lista.
    truncado = False

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def titulo_project(self) -> str:
        return self.config.project_title

    async def listar(self) -> list[Tarea]:
        cfg = self.config
        crudo = await gh(
            "project", "item-list", cfg.project,
            "--owner", cfg.owner, "--format", "json", "--limit", str(LIMITE_ITEMS),
        )
        items = json.loads(crudo or "{}").get("items", [])
        # Se cuenta ANTES de filtrar: el límite lo aplica GitHub sobre el Project entero,
        # hechas incluidas, así que tocar el techo puede estar escondiendo pendientes.
        self.truncado = len(items) >= LIMITE_ITEMS
        tareas: list[Tarea] = []
        for item in items:
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
        """Alta en dos o tres pasos que NO son atómicos.

        No intentamos deshacer nada -borrar un issue recién creado es peor remedio-,
        pero a partir del primer paso cumplido los fallos salen como `ErrorParcial`
        para que la UI diga lo que de verdad quedó en GitHub.
        """
        salida = await gh(
            "issue", "create", "--repo", repo, "--title", titulo, "--body", cuerpo,
        )
        url = salida.strip().splitlines()[-1]
        try:
            item = (
                await gh(
                    "project", "item-add", self.config.project,
                    "--owner", self.config.owner, "--url", url,
                    "--format", "json", "--jq", ".id",
                )
            ).strip()
        except (ErrorGh, OSError) as err:
            raise ErrorParcial(
                f"the new issue exists on GitHub but wasn't added to the board "
                f"— check {url} ({err})"
            ) from err
        if not fecha:
            return
        try:
            await self.fechar(item, fecha)
        except (ErrorGh, OSError) as err:
            raise ErrorParcial(
                f"the new issue was created without a due date — set it by hand "
                f"at {url} ({err})"
            ) from err

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
