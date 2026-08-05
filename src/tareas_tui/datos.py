"""Capa de datos: el GitHub Project se lee y escribe con `gh`, siempre asíncrono.

Todas las llamadas usan `asyncio.create_subprocess_exec`, así que la UI nunca se
congela esperando a la red. `BackendDemo` sirve datos ficticios para las capturas
del README y para las pruebas, sin tocar GitHub.

Cada lectura buena se copia a disco (ver `cache.py`) para que el próximo arranque
pinte la lista sin esperar a la red. `BackendDemo` no cachea nada: la demo y la
suite tienen que ser siempre reproducibles.

Todo lo que toca el Project va por `gh api graphql` y no por los subcomandos
`gh project …`: estos resuelven el owner y el número del Project en CADA
invocación, y eso cuesta entre dos y cuatro veces más (medido contra el Project
real, mediana de 5 corridas):

===================== ======== =========================== ========
paso                  `gh …`   GraphQL directo             ahorro
===================== ======== =========================== ========
leer la lista          1,18 s   0,75 s                      0,43 s
agregar al board       1,69 s   0,38 s                      1,31 s
poner el vencimiento   0,71 s   0,46 s                      0,25 s
crear el issue         0,81 s   0,53 s                      0,28 s
===================== ======== =========================== ========

Los node IDs que las mutaciones necesitan ya estaban resueltos y cacheados (ver
`config.py`), así que el trabajo que `gh project` repetía era puro peaje.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import os
import re
import zlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from rich.color import Color as ColorRich
from rich.style import Style

from . import cache
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

# Tonos para el nombre de repo en la columna repo#issue de la lista. Ni rojo (reservado
# a vencido/peligro) ni amarillo (acento/selección): así el color de repo no compite con
# los dos que sí significan algo.
_COLORES_REPO: tuple[str, ...] = ("cyan", "magenta", "blue", "green")


def color_repo(repo: str) -> str:
    """Tono ANSI estable para `repo` ("owner/nombre"), siempre el mismo entre arranques.

    `hash()` builtin no sirve: Python lo aleatoriza por proceso (PYTHONHASHSEED), así
    que el mismo repo cambiaría de color en cada arranque de la app. `zlib.crc32` es
    determinista entre procesos y versiones de Python.
    """
    indice = zlib.crc32(repo.encode()) % len(_COLORES_REPO)
    return _COLORES_REPO[indice]


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

# Cuánto se recuerda -en disco- que una tarea se cerró desde acá. El workflow de
# Projects que pone Status=Done corre segundos después de `gh issue close`, así que
# un día es techo de sobra; sirve para que un item_id no quede escondido para
# siempre si el Project dejara de devolverlo por cualquier otra razón.
VIDA_CERRADAS = timedelta(days=1)

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


# ------------------------------------------------------------------ GraphQL
# Cada documento lleva nombre de operación (`ItemsDelProject`, `CrearIssue`, …) para
# que los dobles de prueba puedan responder por operación sin parsear GraphQL.

# El PR vinculado y los comentarios viajan DENTRO de esta consulta, en el mismo
# fragmento del Issue que ya se pedía: listar el Project sigue costando una sola ida y
# vuelta por página, que es lo que hace que el refresco no se note.
#
# El campo es `closedByPullRequestsReferences` y sus defaults importan (verificados
# contra la API real el 2026-08-04 con un PR abierto que declaraba `Closes #34`):
#
# * devuelve el PR ABIERTO que cierra el issue -es literalmente su descripción, «list
#   of open pull requests referenced from this issue»-, así que cubre el caso normal
#   de este flujo: la tarea sigue pendiente y el PR todavía no se mergeó;
# * `includeClosedPrs: true` suma los ya mergeados y los cerrados sin mergear. Va
#   puesto porque sin él un PR rechazado deja la fila SIN chip, indistinguible de una
#   tarea que nadie empezó — que es justo lo contrario de lo que pasó;
# * `userLinkedOnly` se queda en false a propósito: con true la misma consulta devolvió
#   `totalCount: 0` para ese PR, porque el vínculo lo hizo la palabra clave `Closes` y
#   no una acción manual en la UI de GitHub.
#
# Se piden 3 PRs y no 1 porque el orden no está prometido: con los cerrados incluidos,
# un intento muerto puede venir primero y el chip hablaría del PR equivocado (elige
# `_pr_principal`). El costo en nodos es despreciable: 100 items × (3 PRs + 1 commit
# cada uno) queda tres órdenes de magnitud bajo el techo de GitHub.
#
# Lo que sí cuesta es el tiempo de GitHub. Medido contra el Project real, A/B con las
# dos consultas alternadas en orden sorteado (25 vueltas, mediana):
#
#     ===================================== ======== =========
#     consulta                               mediana   delta
#     ===================================== ======== =========
#     sin PR ni comentarios (la de antes)     0,77 s        —
#     + comments                              0,84 s   +0,06 s
#     + el PR (número, estado, draft)         0,82 s   +0,04 s
#     + statusCheckRollup                     0,88 s   +0,10 s
#     + mergeable                             0,89 s   +0,11 s
#     todo junto (esta)                       0,98 s   +0,21 s
#     ===================================== ======== =========
#
# Sigue siendo UNA ida y vuelta por página -que es lo que importa: nunca una consulta
# por tarea-, y las dos mitades caras son justo las que dan el chip (`statusCheckRollup`)
# y el «ready to merge» honesto (`mergeable`, que es lo único que sabe de conflictos).
# Además pasa entera dentro del worker de fondo: la lista se pinta de memoria antes y
# durante el refresco, así que estos 0,2 s no bloquean la pantalla en ningún momento.
_Q_ITEMS = """
query ItemsDelProject($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldDateValue {
                date
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue {
              id number title body url
              repository { nameWithOwner }
              comments { totalCount }
              closedByPullRequestsReferences(first: 3, includeClosedPrs: true) {
                nodes {
                  number
                  state
                  isDraft
                  mergeable
                  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_M_CREAR_ISSUE = """
mutation CrearIssue($repo: ID!, $titulo: String!, $cuerpo: String!) {
  createIssue(input: {repositoryId: $repo, title: $titulo, body: $cuerpo}) {
    issue { id number url }
  }
}
"""

_M_AGREGAR_ITEM = """
mutation AgregarItem($proyecto: ID!, $contenido: ID!) {
  addProjectV2ItemById(input: {projectId: $proyecto, contentId: $contenido}) {
    item { id }
  }
}
"""

_M_FECHAR = """
mutation FecharItem($proyecto: ID!, $item: ID!, $campo: ID!, $valor: Date!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $proyecto, itemId: $item, fieldId: $campo, value: {date: $valor}}
  ) { projectV2Item { id } }
}
"""

_M_LIMPIAR_FECHA = """
mutation LimpiarFecha($proyecto: ID!, $item: ID!, $campo: ID!) {
  clearProjectV2ItemFieldValue(
    input: {projectId: $proyecto, itemId: $item, fieldId: $campo}
  ) { projectV2Item { id } }
}
"""

_M_CERRAR_ISSUE = """
mutation CerrarIssue($issue: ID!) {
  closeIssue(input: {issueId: $issue}) { issue { id } }
}
"""

_Q_ID_REPO = """
query IdDeRepo($owner: String!, $nombre: String!) {
  repository(owner: $owner, name: $nombre) { id }
}
"""


async def gh_graphql(consulta: str, **variables: str) -> dict:
    """Una llamada a la API GraphQL; devuelve el `data` de la respuesta.

    Las variables van con `-f` (raw-field) y NO con `-F` (field): `-F` interpreta un
    valor que empiece con «@» como la ruta de un archivo a leer, y por acá pasan
    títulos y notas que escribe el usuario.

    No hace falta mirar el `errors` de la respuesta: ante un error de GraphQL `gh`
    sale con código 1 y deja el mensaje en stderr, así que `gh()` ya levanta `ErrorGh`
    con él (verificado contra la API real).
    """
    args = ["api", "graphql", "-f", f"query={consulta}"]
    for nombre, valor in variables.items():
        args += ["-f", f"{nombre}={valor}"]
    crudo = await gh(*args)
    return json.loads(crudo or "{}").get("data") or {}


#: Orden en que se elige QUÉ PR representa a la tarea cuando el issue tiene varios.
#: También es el conjunto de estados válidos: cualquier otro se descarta.
PRIORIDAD_PR: tuple[str, ...] = ("open", "draft", "merged", "closed")

#: `statusCheckRollup.state` (enum StatusState de GitHub) mapeado a lo que la UI
#: distingue. Lo que no esté acá -EXPECTED, PENDING- cae en "pending"; el rollup
#: AUSENTE es otra cosa y se marca "none": un PR sin un solo check no está corriendo
#: nada, así que no tiene sentido hacerle esperar el «ready to merge».
_CI_POR_ROLLUP = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure"}


@dataclass(frozen=True)
class PrVinculado:
    """El PR que cerraría el issue (`Closes #N`), resumido para la fila y el detalle."""

    numero: int
    estado: str  # uno de PRIORIDAD_PR
    ci: str  # "success" | "failure" | "pending" | "none"
    #: GitHub confirma que se puede mergear. Falso también mientras lo calcula
    #: (`UNKNOWN`), así que solo sirve para AFIRMAR que está listo, nunca para negarlo.
    mergeable: bool = False

    @property
    def listo(self) -> bool:
        """Mergeable ya: abierto, fuera de draft, sin CI roja ni conflictos."""
        return self.estado == "open" and self.ci in {"success", "none"} and self.mergeable


def _pr_de_nodo(nodo: dict) -> PrVinculado | None:
    """Un nodo de `closedByPullRequestsReferences`; None si no se entiende."""
    numero = int(nodo.get("number") or 0)
    estado = str(nodo.get("state") or "").casefold()
    if estado == "open" and nodo.get("isDraft"):
        estado = "draft"
    if not numero or estado not in PRIORIDAD_PR:
        return None
    commits = (nodo.get("commits") or {}).get("nodes") or []
    rollup = ((commits[0] if commits else {}).get("commit") or {}).get("statusCheckRollup")
    return PrVinculado(
        numero=numero,
        estado=estado,
        ci=_CI_POR_ROLLUP.get(str((rollup or {}).get("state") or ""), "pending")
        if rollup
        else "none",
        mergeable=str(nodo.get("mergeable") or "") == "MERGEABLE",
    )


def _pr_principal(nodos: list[dict]) -> PrVinculado | None:
    """El PR que representa a la tarea: primero el trabajo vivo, después la historia.

    Un issue acumula varios PRs con facilidad (un intento cerrado y el bueno), y la API
    no promete orden. Elegir por estado -abierto antes que draft, y cualquiera de los
    dos antes que uno ya cerrado- es lo que hace que el chip hable del PR que el usuario
    está esperando. A igual estado gana el más nuevo.
    """
    prs = [pr for pr in map(_pr_de_nodo, nodos) if pr is not None]
    if not prs:
        return None
    return min(prs, key=lambda pr: (PRIORIDAD_PR.index(pr.estado), -pr.numero))


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
    #: Node ID del issue, para cerrarlo con una mutación en vez de `gh issue close`
    #: (1,06 s contra 0,45 s). Vacío en una tarea que venga de un caché viejo: ahí
    #: `Backend.cerrar` cae al camino de siempre.
    issue_id: str = ""
    #: PR que cerraría el issue, o None si nadie lo empezó. Llega en la MISMA consulta
    #: que lista el Project, así que el chip no cuesta ni un viaje extra.
    pr: PrVinculado | None = None
    #: Comentarios del issue: la conversación que la TUI no muestra pero conviene saber
    #: que existe (el usuario escribe la spec y después la discute en GitHub).
    comentarios: int = 0

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


# ------------------------------------------------------------------ entrada de fechas (en)
# Lo que se puede escribir en un campo de vencimiento además del formato canónico.
# Todo en inglés, porque la UI lo está.
_DIAS_SEMANA: tuple[str, ...] = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_MESES: tuple[str, ...] = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
# `+10d`, `+2w`, `+3m`. Los espacios se sacan antes de mirar, así que "+ 10 d" también.
_RE_RELATIVA = re.compile(r"^\+(\d{1,4})([dwm])$")

#: Los tres formatos que los placeholders de la app enseñan, en una línea. Van juntos y
#: en este orden a propósito: el canónico primero (es el que se guarda en GitHub) y
#: después uno de cada familia, que es lo que hace adivinar el resto.
EJEMPLOS_FECHA = "YYYY-MM-DD · fri · +10d"


def _por_prefijo(token: str, nombres: tuple[str, ...]) -> int | None:
    """Índice del ÚNICO nombre de `nombres` que empieza con `token`; None si hay duda.

    Con tres letras alcanza para que ni los días (`mon`…`sun`) ni los meses
    (`jan`…`dec`) se pisen entre sí, y de yapa entran las formas largas y las
    intermedias que la gente escribe igual (`tues`, `thurs`, `sept`, `august`).
    """
    if len(token) < 3:
        return None
    coinciden = [i for i, nombre in enumerate(nombres) if nombre.startswith(token)]
    return coinciden[0] if len(coinciden) == 1 else None


def _dia_y_mes(texto: str, hoy: date) -> date | None:
    """`aug 20` o `20 aug`; si ese día ya pasó este año, se entiende el del que viene.

    El único año que se prueba después del actual es el siguiente: escribir un mes y
    un día es hablar de los próximos doce meses, no de una fecha lejana (para eso está
    el formato canónico, que lleva el año escrito).
    """
    partes = texto.replace(",", " ").split()
    if len(partes) != 2:
        return None
    primera, segunda = partes
    mes, dia = _por_prefijo(primera, _MESES), segunda
    if mes is None:
        mes, dia = _por_prefijo(segunda, _MESES), primera
    if mes is None or not dia.isdigit():
        return None
    for ano in (hoy.year, hoy.year + 1):
        try:
            candidata = date(ano, mes + 1, int(dia))
        except ValueError:
            # Un día que no existe en ese mes (`feb 30`), o el 29 de febrero cuando el
            # año que toca no es bisiesto: no hay nada que adivinar, se pide el formato
            # canónico como con cualquier otra entrada que no se entiende.
            return None
        if candidata >= hoy:
            return candidata
    return None


def interpretar_fecha(texto: str | None, hoy: date) -> date | None:
    """Lo que el usuario escribió en un campo de vencimiento, resuelto a una fecha.

    `hoy` va como parámetro y no se lee de `date.today()` acá adentro para que todo
    esto sea probable sin depender del día en que corra la suite.

    Acepta, sin importar la caja ni los espacios de más:

    ===================== ==========================================================
    `2026-08-20`           el formato canónico, el que viaja a GitHub
    `today`                hoy
    `tomorrow` · `tom`     mañana
    `+10d` `+2w` `+3m`     días, semanas y meses calendario contados desde `hoy`
    `fri` · `friday`       la PRÓXIMA vez que caiga ese día: parado en un viernes,
                           `fri` es el viernes que viene (hoy + 7), nunca hoy — un
                           vencimiento que se escribe es siempre uno que todavía no
                           llegó
    `aug 20` · `20 aug`    ese día; si ya pasó este año, el del año que viene
    ===================== ==========================================================

    Devuelve None ante cualquier cosa que no se entienda de UNA sola forma. Quien
    llama decide qué hacer con eso (la TUI avisa en la línea del hint, el CLI sale con
    código 2): fechar con una interpretación a medias sería peor que no fechar.

    No reusa `parsear_fecha` para el formato canónico a propósito: esa es tolerante
    porque lee el caché -un valor corrupto en disco no puede tumbar la app- y acá hace
    falta lo contrario, que lo que está mal escrito falle.
    """
    limpio = " ".join((texto or "").split()).casefold()
    if not limpio:
        return None

    try:
        return date.fromisoformat(limpio)
    except ValueError:
        pass

    if limpio == "today":
        return hoy
    if limpio in {"tomorrow", "tom"}:
        return hoy + timedelta(days=1)

    relativa = _RE_RELATIVA.match(limpio.replace(" ", ""))
    if relativa:
        cantidad, unidad = int(relativa.group(1)), relativa.group(2)
        if unidad == "d":
            return hoy + timedelta(days=cantidad)
        if unidad == "w":
            return hoy + timedelta(weeks=cantidad)
        return mas_meses(hoy, cantidad)  # el recorte de fin de mes ya está resuelto ahí

    dia_semana = _por_prefijo(limpio, _DIAS_SEMANA)
    if dia_semana is not None:
        salto = (dia_semana - hoy.weekday()) % 7
        return hoy + timedelta(days=salto or 7)

    return _dia_y_mes(limpio, hoy)


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


# Chip del PR vinculado: un número y UN glifo. Contesta de un vistazo las dos únicas
# preguntas que se hacen sobre una fila con PR -¿cuál es? y ¿está sano?- y no gasta ni
# un carácter en el matiz, que se lee con palabras en el detalle (`resumen_pr`).
#
# El estado del PR manda sobre el de la CI porque en uno ya mergeado o cerrado la CI no
# dice nada accionable. Colores: los mismos que el resto de la app -verde éxito, rojo
# error, color 7 para lo secundario (ver SECUNDARIO)-, nunca `dim`.
_CHIP_POR_ESTADO: dict[str, tuple[str, str | Style]] = {
    "merged": ("✓", "green"),
    "closed": ("✗", "bold red"),
    "draft": ("·", SECUNDARIO),
}
_CHIP_POR_CI: dict[str, tuple[str, str | Style]] = {
    "success": ("✓", "green"),
    "failure": ("✗", "bold red"),
}

_CI_EN_PALABRAS = {
    "success": "CI passing",
    "failure": "CI failing",
    "pending": "CI running",
    "none": "no checks",
}
_ESTADO_EN_PALABRAS = {
    "open": "open",
    "draft": "draft",
    "merged": "merged",
    "closed": "closed unmerged",
}


def chip_pr(pr: PrVinculado | None) -> tuple[str, str | Style]:
    """(texto, estilo) del indicador de PR de la lista; `("", "")` si no hay PR.

    Sin PR no devuelve nada a propósito: la mayoría de las tareas no tiene uno y la
    lista no debe pagar ruido -ni columnas- por una minoría de filas.
    """
    if pr is None:
        return "", ""
    glifo, estilo = (
        _CHIP_POR_ESTADO.get(pr.estado) or _CHIP_POR_CI.get(pr.ci) or ("·", SECUNDARIO)
    )
    return f"#{pr.numero}{glifo}", estilo


def resumen_pr(pr: PrVinculado) -> str:
    """Línea del detalle: dice con palabras lo que el chip resume en un glifo."""
    partes = [f"PR #{pr.numero}", _ESTADO_EN_PALABRAS.get(pr.estado, pr.estado)]
    if pr.estado in {"open", "draft"}:
        partes.append(_CI_EN_PALABRAS.get(pr.ci, pr.ci))
    if pr.listo:
        partes.append("ready to merge")
    return " · ".join(partes)


def acortar(texto: str, ancho: int) -> str:
    """Trunca con puntos suspensivos: el ancho del pane manda, nada desborda."""
    if ancho <= 0:
        return ""
    return texto if len(texto) <= ancho else texto[: max(1, ancho - 1)] + "…"


# ------------------------------------------------------------------ caché en disco
@dataclass(frozen=True)
class Instantanea:
    """Lo último que llegó de GitHub, leído del disco al arrancar.

    `tareas` en None significa «nunca hubo una lectura buena»: la pantalla arranca
    entonces en «loading tasks…» como siempre. Una lista vacía es un dato legítimo
    (el Project no tenía pendientes) y se pinta como tal.
    """

    tareas: list[Tarea] | None = None
    momento: datetime | None = None
    repos: list[str] = field(default_factory=list)
    repo_actual: str | None = None
    #: item_ids cerrados desde la app que el Project todavía puede devolver como
    #: pendientes. Sin esto el caché los resucitaba en el próximo arranque.
    cerradas: set[str] = field(default_factory=set)


def _pr_a_dict(pr: PrVinculado | None) -> dict | None:
    if pr is None:
        return None
    return {"numero": pr.numero, "estado": pr.estado, "ci": pr.ci, "mergeable": pr.mergeable}


def _pr_de_dict(crudo: object) -> PrVinculado | None:
    """None ante cualquier entrada rara, igual que `_tarea_de_dict`.

    Una tarea guardada por una versión anterior no trae la clave: el chip aparece recién
    con el primer refresco, que es exactamente lo que pasa con cualquier dato nuevo.
    """
    if not isinstance(crudo, dict):
        return None
    try:
        return PrVinculado(
            numero=int(crudo["numero"]),
            estado=str(crudo["estado"]),
            ci=str(crudo["ci"]),
            mergeable=bool(crudo.get("mergeable")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _tarea_a_dict(tarea: Tarea) -> dict:
    return {
        "item_id": tarea.item_id,
        "repo": tarea.repo,
        "numero": tarea.numero,
        "titulo": tarea.titulo,
        "url": tarea.url,
        "cuerpo": tarea.cuerpo,
        "vence": tarea.vence.isoformat() if tarea.vence else None,
        "repeat": tarea.repeat,
        "issue_id": tarea.issue_id,
        "pr": _pr_a_dict(tarea.pr),
        "comentarios": tarea.comentarios,
    }


def _tarea_de_dict(crudo: object) -> Tarea | None:
    """Devuelve None ante cualquier entrada rara: el caché no puede tumbar la app."""
    if not isinstance(crudo, dict):
        return None
    try:
        return Tarea(
            item_id=str(crudo["item_id"]),
            repo=str(crudo["repo"]),
            numero=int(crudo["numero"]),
            titulo=str(crudo["titulo"]),
            url=str(crudo.get("url", "")),
            cuerpo=str(crudo.get("cuerpo", "")),
            vence=parsear_fecha(crudo.get("vence")),
            repeat=str(crudo["repeat"]) if crudo.get("repeat") else None,
            issue_id=str(crudo.get("issue_id") or ""),
            pr=_pr_de_dict(crudo.get("pr")),
            comentarios=int(crudo.get("comentarios") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _momento_de(crudo: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(crudo))
    except (TypeError, ValueError):
        return None


def _cwd() -> str:
    """Directorio actual, o "" si ya no existe.

    La app está pensada para vivir en un pane de larga vida (ver README): renombrar
    o borrar el directorio desde el que se lanzó deja a `os.getcwd()` levantando
    OSError, y esto corre en el arranque. Sin repo detectado se cae en modo todas,
    que es exactamente lo que ya pasaba cuando `gh repo view` fallaba.
    """
    try:
        return os.getcwd()
    except OSError:
        return ""


def numero_de_url(url: str) -> int:
    """Número del issue a partir de su URL; 0 si la URL no termina en un número."""
    cola = url.rstrip("/").rsplit("/", 1)[-1]
    return int(cola) if cola.isdigit() else 0


# ------------------------------------------------------------------ backends
class Backend:
    """Lee y escribe el Project real con `gh`."""

    #: La última lectura tocó `LIMITE_ITEMS`: puede haber pendientes fuera de la lista.
    truncado = False

    #: Mensaje si el campo de fecha configurado ya no aparece con ese nombre en el
    #: Project. La lectura "funcionaba" igual, con TODOS los vencimientos vacíos: esa
    #: es justo la falla silenciosa que esto viene a romper. Lo consume la UI.
    aviso_campo: str | None = None

    #: El sondeo del campo se hace una vez por proceso: solo se dispara cuando ningún
    #: item trae la columna, y repetirlo en cada refresco sería una llamada de red
    #: cada 5 minutos para volver a saber lo mismo.
    _campo_probado = False
    _campo_actual: str | None = None

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def titulo_project(self) -> str:
        return self.config.project_title

    # -------------------------------------------------------------- caché
    @property
    def _clave_cache(self) -> str:
        """Una entrada por Project: cambiar de board no debe mostrar el anterior."""
        return f"{self.config.owner}/{self.config.project}"

    def _cachear(self, **campos) -> None:
        cache.escribir(self._clave_cache, campos)

    def instantanea(self) -> Instantanea:
        """Última lectura buena guardada en disco, para pintar antes de la red."""
        guardado = cache.leer(self._clave_cache)
        crudas = guardado.get("tareas")
        tareas: list[Tarea] | None = None
        if isinstance(crudas, list):
            tareas = [t for t in map(_tarea_de_dict, crudas) if t is not None]
        repos = guardado.get("repos")
        # El repo del cwd se cachea por directorio: la app se lanza desde muchos.
        repos_cwd = guardado.get("repo_actual")
        repo_actual = None
        if isinstance(repos_cwd, dict) and _cwd():
            valor = repos_cwd.get(_cwd())
            repo_actual = str(valor) if valor else None
        return Instantanea(
            tareas=tareas,
            momento=_momento_de(guardado.get("momento")),
            repos=[str(r) for r in repos] if isinstance(repos, list) else [],
            repo_actual=repo_actual,
            cerradas=self._cerradas_vigentes(guardado.get("cerradas")),
        )

    @staticmethod
    def _cerradas_vigentes(crudo: object) -> set[str]:
        """Cerradas guardadas que todavía valen; las de más de `VIDA_CERRADAS` caducan.

        Una marca sin fecha legible se descarta: esconder una tarea para siempre por
        un timestamp corrupto sería peor que mostrarla de más.
        """
        if not isinstance(crudo, dict):
            return set()
        limite = datetime.now() - VIDA_CERRADAS
        vigentes: set[str] = set()
        for item_id, cuando in crudo.items():
            momento = _momento_de(cuando)
            if momento is not None and momento > limite:
                vigentes.add(str(item_id))
        return vigentes

    def recordar_cerradas(self, ids: set[str]) -> None:
        """Persiste qué tareas se cerraron desde acá, conservando su fecha original.

        `ids` es el set completo: lo que no venga se olvida. Así el mismo llamado
        sirve para anotar un cierre nuevo y para podar las que el Project ya dejó de
        devolver, sin dos caminos que puedan desincronizarse.
        """
        ahora = datetime.now().isoformat(timespec="seconds")
        guardado = cache.leer(self._clave_cache).get("cerradas")
        previas = guardado if isinstance(guardado, dict) else {}
        self._cachear(cerradas={str(i): previas.get(i, ahora) for i in ids})

    async def _nombre_del_campo(self, items: list[dict]) -> str:
        """Nombre bajo el que `gh` aplana el campo de fecha en estos items.

        Casi siempre es el de la config y no cuesta nada. Si no aparece en NINGÚN
        item hay dos explicaciones posibles: que nadie tenga vencimiento puesto, o
        que el campo se haya renombrado en GitHub -y entonces la app pintaba todo
        «no due date» sin decir una palabra-. Solo en ese caso se preguntan los
        campos del Project y se desempata por **id**, que es lo único que un rename
        no cambia: si el campo sigue ahí con otro nombre se usa el nuevo y se avisa.
        """
        nombre = self.config.campo_fecha
        if self._campo_probado:
            return self._campo_actual or nombre
        if not items or any(valor_campo(item, nombre) is not None for item in items):
            return nombre

        self._campo_probado = True
        try:
            crudo = await gh(
                "project", "field-list", self.config.project, "--owner", self.config.owner,
                "--format", "json", "--limit", "50",
            )
            campos = json.loads(crudo or "{}").get("fields", [])
        except (ErrorGh, OSError, json.JSONDecodeError):
            return nombre  # sin poder comprobarlo no se inventa un aviso

        por_id = next((c for c in campos if c.get("id") == self.config.campo_fecha_id), None)
        if por_id is None:
            nombres = ", ".join(str(c.get("name", "?")) for c in campos) or "(none)"
            self.aviso_campo = (
                f'the date field "{nombre}" is gone from the Project, so every task '
                f"shows up with no due date. Fields now: {nombres}"
            )
            return nombre

        actual = str(por_id.get("name") or nombre)
        if actual.replace(" ", "").casefold() != nombre.replace(" ", "").casefold():
            self.aviso_campo = (
                f'the date field was renamed on GitHub: "{nombre}" is now "{actual}". '
                f"Reading it by id for now — update campo_fecha in your config file."
            )
            self._campo_actual = actual
            return actual
        return nombre  # el campo está y se llama igual: nadie tiene vencimiento puesto

    async def _traer_items(self) -> list[dict]:
        """Los items del Project, aplanados igual que `gh project item-list --format json`.

        Se replica esa forma a propósito: el parseo de `listar`, `valor_campo` y la
        detección de campo renombrado siguen valiendo tal cual, así que el cambio de
        transporte no toca ni una regla de negocio.

        La paginación es la misma que hacía `gh` por dentro (100 por página), menos el
        peaje de resolver el owner en cada invocación.
        """
        items: list[dict] = []
        cursor = ""
        while len(items) < LIMITE_ITEMS:
            variables = {"id": self.config.project_id}
            if cursor:  # la primera página va sin `after`: "" no es un cursor válido
                variables["cursor"] = cursor
            datos = await gh_graphql(_Q_ITEMS, **variables)
            pagina = (datos.get("node") or {}).get("items") or {}
            items.extend(_aplanar_item(nodo) for nodo in pagina.get("nodes") or [])
            info = pagina.get("pageInfo") or {}
            cursor = str(info.get("endCursor") or "")
            if not info.get("hasNextPage") or not cursor:
                break
        return items[:LIMITE_ITEMS]

    async def listar(self) -> list[Tarea]:
        cfg = self.config
        items = await self._traer_items()
        # Se cuenta ANTES de filtrar: el límite lo aplica GitHub sobre el Project entero,
        # hechas incluidas, así que tocar el techo puede estar escondiendo pendientes.
        self.truncado = len(items) >= LIMITE_ITEMS
        campo = await self._nombre_del_campo(items)
        tareas: list[Tarea] = []
        for item in items:
            # `valor_campo` y no `item["status"]`: la clave llega con la caja del campo
            # tal como lo llame el Project ("Status"), no siempre en minúsculas.
            if str(valor_campo(item, "status") or "").casefold() == cfg.estado_hecho.casefold():
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
                    titulo=(
                        contenido.get("title") or valor_campo(item, "title") or "(untitled)"
                    ).strip(),
                    url=contenido.get("url", ""),
                    cuerpo=cuerpo,
                    vence=parsear_fecha(valor_campo(item, campo)),
                    repeat=repeat,
                    issue_id=contenido.get("id", ""),
                    pr=_pr_principal(contenido.get("prs") or []),
                    comentarios=int(contenido.get("comments") or 0),
                )
            )
        ordenadas = ordenar(tareas)
        self._cachear(
            tareas=[_tarea_a_dict(t) for t in ordenadas],
            momento=datetime.now().isoformat(timespec="seconds"),
        )
        return ordenadas

    async def repos(self) -> list[str]:
        """Repos del owner para el picker; de paso guarda sus node IDs.

        Pedir `id` en el mismo `--json` no cuesta nada y es justo lo que necesita
        `createIssue`, así que el alta no gasta una llamada extra en resolverlo.
        """
        crudo = await gh(
            "repo", "list", self.config.owner, "--limit", "200", "--json", "nameWithOwner,id"
        )
        crudos = json.loads(crudo or "[]")
        listado = sorted(r["nameWithOwner"] for r in crudos)
        ids = {str(r["nameWithOwner"]): str(r["id"]) for r in crudos if r.get("id")}
        self._cachear(repos=listado, repo_ids=ids)
        return listado

    async def _id_repo(self, repo: str) -> str:
        """Node ID de «owner/nombre», el que pide `createIssue`.

        Sale gratis del `gh repo list` que alimenta el picker (cacheado en disco, así
        que también sobrevive al reinicio). Solo se pregunta cuando no está: pasa con
        un repo de otro owner, que el listado del picker no incluye.
        """
        guardados = cache.leer(self._clave_cache).get("repo_ids")
        guardados = guardados if isinstance(guardados, dict) else {}
        if guardados.get(repo):
            return str(guardados[repo])
        owner, _, nombre = repo.partition("/")
        datos = await gh_graphql(_Q_ID_REPO, owner=owner, nombre=nombre)
        identificador = str((datos.get("repository") or {}).get("id") or "")
        if not identificador:
            raise ErrorGh(f"GitHub didn't return an id for the repository {repo}")
        self._cachear(repo_ids={**guardados, repo: identificador})
        return identificador

    async def repo_actual(self) -> str | None:
        """Repo GitHub del directorio donde se lanzó la app, o None si no aplica.

        Cubre estar fuera de un repo git, un repo sin remote de GitHub o sin `gh`
        autenticado ahí: cualquier falla de `gh` cae en modo todas, en silencio.
        """
        try:
            crudo = await gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        except ErrorGh:
            return None
        repo = crudo.strip() or None
        if repo and _cwd():
            # Solo se cachea el caso positivo: estando fuera de un repo no hay filtro
            # que aplicar, así que el arranque no gana nada recordándolo.
            guardado = cache.leer(self._clave_cache).get("repo_actual")
            por_cwd = dict(guardado) if isinstance(guardado, dict) else {}
            por_cwd[_cwd()] = repo
            self._cachear(repo_actual=por_cwd)
        return repo

    async def crear(self, repo: str, titulo: str, fecha: str | None, cuerpo: str = "") -> Tarea:
        """Alta en dos o tres pasos que NO son atómicos.

        No intentamos deshacer nada -borrar un issue recién creado es peor remedio-,
        pero a partir del primer paso cumplido los fallos salen como `ErrorParcial`
        para que la UI diga lo que de verdad quedó en GitHub.

        Devuelve la tarea creada para que la lista la muestre en el acto: releerla del
        Project costaba otra lectura entera sobre un alta que ya gastó tres llamadas.
        """
        repo_id = await self._id_repo(repo)
        datos = await gh_graphql(_M_CREAR_ISSUE, repo=repo_id, titulo=titulo, cuerpo=cuerpo)
        issue = (datos.get("createIssue") or {}).get("issue") or {}
        url = str(issue.get("url") or "")
        contenido = str(issue.get("id") or "")
        if not contenido or not url:
            # Todavía NO es un efecto parcial: sin id no hay issue que reportar.
            raise ErrorGh("GitHub didn't return the new issue")
        try:
            datos = await gh_graphql(
                _M_AGREGAR_ITEM, proyecto=self.config.project_id, contenido=contenido
            )
            item = str(((datos.get("addProjectV2ItemById") or {}).get("item") or {}).get("id") or "")
            if not item:
                raise ErrorGh("GitHub didn't return the board item")
        except (ErrorGh, OSError) as err:
            raise ErrorParcial(
                f"the new issue exists on GitHub but wasn't added to the board "
                f"— check {url} ({err})"
            ) from err
        notas, repeat = separar_repeticion(cuerpo)
        creada = Tarea(
            item_id=item,
            repo=repo,
            numero=int(issue.get("number") or 0) or numero_de_url(url),
            titulo=titulo,
            url=url,
            cuerpo=notas,
            vence=parsear_fecha(fecha),
            repeat=repeat,
            issue_id=contenido,
        )
        if not fecha:
            return creada
        try:
            await self.fechar(item, fecha)
        except (ErrorGh, OSError) as err:
            raise ErrorParcial(
                f"the new issue was created without a due date — set it by hand "
                f"at {url} ({err})"
            ) from err
        return creada

    async def cerrar(self, tarea: Tarea) -> None:
        """Cierra el issue. Idempotente en los dos caminos: cerrar uno ya cerrado no
        es un error ni para `closeIssue` ni para `gh issue close`."""
        if tarea.issue_id:
            await gh_graphql(_M_CERRAR_ISSUE, issue=tarea.issue_id)
            return
        # Tarea venida de un caché anterior a que se guardara el node ID.
        await gh("issue", "close", str(tarea.numero), "--repo", tarea.repo)

    async def repetir(self, tarea: Tarea, hoy: date) -> Tarea:
        """Crea la siguiente ocurrencia de una repetitiva; devuelve la tarea nueva."""
        if not tarea.repeat or tarea.vence is None:
            raise ValueError("the task doesn't repeat or has no due date")
        proxima = proxima_fecha(tarea.vence, tarea.repeat, hoy)
        return await self.crear(
            tarea.repo, tarea.titulo, proxima.isoformat(),
            componer_cuerpo(tarea.cuerpo, tarea.repeat),
        )

    async def fechar(self, item_id: str, fecha: str | None) -> None:
        """`fecha` vacía o None limpia el vencimiento."""
        comun = {
            "proyecto": self.config.project_id,
            "item": item_id,
            "campo": self.config.campo_fecha_id,
        }
        if fecha:
            await gh_graphql(_M_FECHAR, **comun, valor=fecha)
        else:
            await gh_graphql(_M_LIMPIAR_FECHA, **comun)


def _aplanar_item(nodo: dict) -> dict:
    """Un item de GraphQL con la MISMA forma que devolvía `gh project item-list`.

    Los campos personalizados se aplanan por nombre en minúsculas (`Vencimiento` →
    `vencimiento`, `Status` → `status`), que es exactamente lo que hacía `gh`; para
    los nombres con espacios `valor_campo` normaliza los dos lados, así que da igual
    la forma exacta. Reproducir la forma vieja es a propósito: así el parseo de
    `listar` y la detección de campo renombrado no se enteran del cambio.
    """
    contenido = nodo.get("content") or {}
    plano: dict = {
        "id": nodo.get("id", ""),
        "content": {
            "type": contenido.get("__typename", ""),
            "number": contenido.get("number", 0),
            "title": contenido.get("title", ""),
            "body": contenido.get("body", ""),
            "url": contenido.get("url", ""),
            "repository": (contenido.get("repository") or {}).get("nameWithOwner", ""),
            "id": contenido.get("id", ""),
            # Dos claves que `gh project item-list` no daba (por eso no hay forma vieja
            # que respetar): los PRs que cerrarían el issue y sus comentarios.
            "comments": (contenido.get("comments") or {}).get("totalCount", 0),
            "prs": (contenido.get("closedByPullRequestsReferences") or {}).get("nodes") or [],
        },
    }
    for valor in (nodo.get("fieldValues") or {}).get("nodes") or []:
        nombre = str((valor.get("field") or {}).get("name") or "").strip()
        if not nombre:
            continue  # un fragmento que no pedimos (iteración, número, …) llega vacío
        plano[nombre.casefold()] = valor.get("date") or valor.get("name") or valor.get("text")
    return plano


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

# PR vinculado y comentarios de algunas tareas de la demo, por número de issue: las
# capturas del README enseñan así el chip en sus tres formas -verde, roja y en curso- y
# las otras cuatro filas siguen mostrando que sin PR no hay ruido.
_DEMO_PR: dict[int, PrVinculado] = {
    48: PrVinculado(numero=112, estado="open", ci="failure", mergeable=True),
    3: PrVinculado(numero=87, estado="open", ci="success", mergeable=True),
    21: PrVinculado(numero=9, estado="draft", ci="pending", mergeable=False),
}
_DEMO_COMENTARIOS: dict[int, int] = {48: 3, 12: 1}


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
                    pr=_DEMO_PR.get(numero),
                    comentarios=_DEMO_COMENTARIOS.get(numero, 0),
                )
                for numero, repo, titulo, dias, repeat in _DEMO
            ]
        )

    @property
    def titulo_project(self) -> str:
        return self._project_title

    def instantanea(self) -> Instantanea:
        """La demo nunca lee ni escribe el caché del usuario: siempre arranca limpia."""
        return Instantanea()

    def _cachear(self, **campos) -> None:
        return None

    def recordar_cerradas(self, ids: set[str]) -> None:
        # No solo por no cachear: `Backend.recordar_cerradas` necesita `self.config`,
        # que la demo no tiene (nunca llama a `super().__init__`).
        return None

    async def listar(self) -> list[Tarea]:
        return list(self._tareas)

    async def repos(self) -> list[str]:
        return ["acme/web", "korta/api", "lumen/shop", "mesa/intranet", "vela/landing"]

    async def repo_actual(self) -> str | None:
        return self._repo_actual

    async def crear(self, repo: str, titulo: str, fecha: str | None, cuerpo: str = "") -> Tarea:
        # Igual que el backend real: el cuerpo entra crudo y la repetición se vuelve a
        # leer de ahí, así la demo ejercita el mismo ida y vuelta del metadato.
        notas, repeat = separar_repeticion(cuerpo)
        numero = max((t.numero for t in self._tareas), default=0) + 1
        creada = Tarea(
            item_id=f"demo-{numero}",
            repo=repo,
            numero=numero,
            titulo=titulo,
            url=f"https://example.com/{repo}/issues/{numero}",
            cuerpo=notas,
            vence=parsear_fecha(fecha),
            repeat=repeat,
        )
        self._tareas = ordenar([*self._tareas, creada])
        return creada

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
