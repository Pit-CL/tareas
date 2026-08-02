"""Interfaz textual: lista densa arriba, todo lo demás en modales.

Dos reglas mandan sobre el diseño:

* **Cabe en un pane chico.** 80x15 es el caso de referencia; nada usa alto fijo y los
  anchos se recalculan en cada `Resize`, truncando con puntos suspensivos.
* **Se opera con mouse.** Cada acción tiene un blanco clickeable: filas, botones de la
  cabecera, teclas del footer y botones dentro de los modales. El teclado es atajo.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .datos import (
    ANCHO_VENCE,
    Backend,
    ErrorGh,
    Tarea,
    acortar,
    etiqueta_vencimiento,
    fecha_larga,
    mas_un_mes,
)

REFRESCO_SEGUNDOS = 300.0
ANCHO_CLIENTE_MAX = 22
ANCHO_CLIENTE_MIN = 10

# ------------------------------------------------------------------------------------
# Theme: hereda la paleta ANSI del terminal en vez de fijar colores propios.
# `ansi=True` deja pasar los colores 0-15 tal cual, y foreground/background en
# "ansi_default" usan los del terminal, así que si el terminal conmuta claro/oscuro
# la TUI conmuta con él sin enterarse. `surface`/`panel` van a "ansi_black" (color 0),
# que en cualquier esquema decente es un gris de panel y no el fondo: así los modales
# contrastan tanto en claro como en oscuro.
# ------------------------------------------------------------------------------------
THEME_TERMINAL = Theme(
    name="terminal",
    ansi=True,
    primary="ansi_yellow",
    secondary="ansi_cyan",
    accent="ansi_yellow",
    warning="ansi_yellow",
    error="ansi_red",
    success="ansi_green",
    foreground="ansi_default",
    background="ansi_default",
    surface="ansi_black",
    panel="ansi_black",
    boost="ansi_black",
    dark=True,
    variables={
        # Textual exige estas dos cuando ansi=True; en "default" quedan transparentes,
        # que es justo lo que queremos para no pelear con el fondo del terminal.
        "ansi-background": "ansi_default",
        "ansi-foreground": "ansi_default",
        "border-blurred": "ansi_bright_black",
        "block-cursor-foreground": "ansi_black",
        "block-cursor-background": "ansi_yellow",
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-foreground": "ansi_default",
        "block-cursor-blurred-background": "ansi_bright_black",
        "block-cursor-blurred-text-style": "none",
        "footer-background": "ansi_default",
        "footer-key-background": "ansi_default",
        "footer-description-background": "ansi_default",
        "footer-key-foreground": "ansi_yellow",
        "footer-description-foreground": "ansi_default",
        "input-cursor-background": "ansi_yellow",
        "input-cursor-foreground": "ansi_black",
        "input-selection-background": "ansi_bright_black",
        "input-selection-foreground": "ansi_default",
        "input-cursor-text-style": "none",
        "button-foreground": "ansi_default",
        "screen-selection-background": "ansi_bright_black",
        "screen-selection-foreground": "ansi_default",
        "scrollbar": "ansi_bright_black",
        "scrollbar-hover": "ansi_yellow",
        "scrollbar-active": "ansi_yellow",
        "scrollbar-background": "ansi_default",
        "scrollbar-background-hover": "ansi_default",
        "scrollbar-background-active": "ansi_default",
        "scrollbar-corner-color": "ansi_default",
    },
)


# ------------------------------------------------------------------------------------
# Piezas reutilizables
# ------------------------------------------------------------------------------------
class BotonCabecera(Static):
    """Acción clickeable de la cabecera; ocupa una fila y solo el ancho de su texto."""

    class Pulsado(Message):
        def __init__(self, accion: str) -> None:
            self.accion = accion
            super().__init__()

    def __init__(self, etiqueta: str, accion: str, **kwargs) -> None:
        super().__init__(etiqueta, **kwargs)
        self._accion = accion

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Pulsado(self._accion))


class AtajosFecha(Horizontal):
    """Quick-picks de vencimiento, todos clickeables.

    Preferimos esto a un calendario: el date picker mantenido para textual
    (textual-timepiece) despliega un overlay de 19 filas, que no entra en un pane de 15.
    Cada chip también responde a su número (1-5): lo resuelven los modales que la
    contienen, con prioridad sobre el campo de fecha (ver `InputFecha`).
    """

    OPCIONES: tuple[tuple[str, str], ...] = (
        ("today", "hoy"),
        ("tomorrow", "manana"),
        ("+3 days", "mas3"),
        ("next week", "semana"),
        ("+1 month", "mes"),
    )

    class Elegida(Message):
        def __init__(self, fecha: date) -> None:
            self.fecha = fecha
            super().__init__()

    def compose(self) -> ComposeResult:
        for indice, (etiqueta, clave) in enumerate(self.OPCIONES, start=1):
            yield Button(f"{indice}·{etiqueta}", id=f"qp-{clave}", classes="chip")

    @staticmethod
    def fecha(clave: str) -> date:
        hoy = date.today()
        return {
            "hoy": hoy,
            "manana": hoy + timedelta(days=1),
            "mas3": hoy + timedelta(days=3),
            "semana": hoy + timedelta(days=7),
            "mes": mas_un_mes(hoy),
        }[clave]

    @classmethod
    def fecha_por_indice(cls, indice: int) -> date:
        """`indice` 1-5, en el orden de `OPCIONES` (atajos numéricos de los modales)."""
        _, clave = cls.OPCIONES[indice - 1]
        return cls.fecha(clave)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("qp-"):
            return
        event.stop()
        self.post_message(self.Elegida(self.fecha(event.button.id[3:])))


class InputFecha(Input):
    """Input de fecha que libera las teclas 1-5 hacia los atajos numéricos del modal
    mientras está vacío; si ya tiene texto (fecha manual en curso o precargada), el
    dígito se escribe como cualquier otro carácter.

    Textual no deja que un binding de prioridad "gane" a un Input si este declara que
    consume esa tecla (`check_consume_key`): por eso el corte se resuelve acá, no con
    lógica en el modal.
    """

    def check_consume_key(self, key: str, character: str | None) -> bool:
        if key in {"1", "2", "3", "4", "5"} and not self.value:
            return False
        return super().check_consume_key(key, character)


class DialogoModal(ModalScreen):
    """Base de los modales: `esc` cierra y un clic fuera del diálogo también."""

    BINDINGS = [Binding("escape", "cancelar", "volver")]

    def action_cancelar(self) -> None:
        self.dismiss(None)

    def on_click(self, event: events.Click) -> None:
        golpeado, _ = self.get_widget_at(event.screen_x, event.screen_y)
        if golpeado is self:  # el clic cayó en el fondo, no dentro del diálogo
            event.stop()
            self.dismiss(None)


# ------------------------------------------------------------------------------------
# Modales
# ------------------------------------------------------------------------------------
class DetalleScreen(DialogoModal):
    """Detalle del issue. Devuelve 'cerrar', 'fecha' o None."""

    BINDINGS = [
        Binding("j", "abajo", "", show=False),
        Binding("k", "arriba", "", show=False),
    ]

    def __init__(self, tarea: Tarea) -> None:
        super().__init__()
        self.tarea = tarea

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg-detalle", classes="dlg"):
            yield Static(self.tarea.titulo, id="det-titulo")
            yield Static(
                f"{self.tarea.cliente} · {fecha_larga(self.tarea.vence, date.today())}",
                id="det-meta",
            )
            with VerticalScroll(id="det-cuerpo"):
                yield Markdown(self.tarea.cuerpo or "_(no description)_")
            with Horizontal(classes="fila-botones"):
                yield Button("close task", id="det-cerrar", classes="chip peligro")
                yield Button("change date", id="det-fecha", classes="chip")
                yield Button("back", id="det-volver", classes="chip")
            yield Static("j/k scroll · esc back", id="det-hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#det-cuerpo").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss({"det-cerrar": "cerrar", "det-fecha": "fecha"}.get(event.button.id or ""))

    def action_abajo(self) -> None:
        self.query_one("#det-cuerpo", VerticalScroll).scroll_down()

    def action_arriba(self) -> None:
        self.query_one("#det-cuerpo", VerticalScroll).scroll_up()


class FechaScreen(DialogoModal):
    """Elegir vencimiento. Devuelve 'AAAA-MM-DD', '' para quitarlo, o None."""

    BINDINGS = [
        Binding(str(i), f"quick_pick({i})", "", show=False, priority=True)
        for i in range(1, 6)
    ]

    def __init__(self, titulo: str, actual: date | None) -> None:
        super().__init__()
        self.titulo = titulo
        self.actual = actual

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg-fecha", classes="dlg"):
            yield Static(f"due date · {acortar(self.titulo, 120)}", classes="dlg-titulo")
            yield AtajosFecha(classes="fila-chips")
            with Horizontal(classes="fila-botones"):
                yield InputFecha(
                    value=self.actual.isoformat() if self.actual else "",
                    placeholder="YYYY-MM-DD",
                    id="fecha-input",
                )
                yield Button("save", id="fecha-guardar", classes="chip")
                yield Button("clear", id="fecha-quitar", classes="chip")
                yield Button("cancel", id="fecha-cancelar", classes="chip")
            yield Static("", id="fecha-error", classes="error-linea")
            yield Static("1-5 quick · enter save · esc cancel", id="fecha-hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#fecha-input", Input).focus()

    def action_quick_pick(self, indice: int) -> None:
        """Aplica y guarda de inmediato: `d`→número son dos teclas para fechar."""
        self.dismiss(AtajosFecha.fecha_por_indice(indice).isoformat())

    def on_atajos_fecha_elegida(self, event: AtajosFecha.Elegida) -> None:
        event.stop()
        self.dismiss(event.fecha.isoformat())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._guardar()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("fecha-"):
            return
        event.stop()
        if event.button.id == "fecha-guardar":
            self._guardar()
        elif event.button.id == "fecha-quitar":
            self.dismiss("")
        else:
            self.dismiss(None)

    def _guardar(self) -> None:
        texto = self.query_one("#fecha-input", Input).value.strip()
        if not texto:
            self.dismiss("")
            return
        try:
            date.fromisoformat(texto)
        except ValueError:
            self.query_one("#fecha-error", Static).update("invalid format, use YYYY-MM-DD")
            return
        self.dismiss(texto)


class NuevaScreen(DialogoModal):
    """Alta de tarea. Devuelve {'repo','titulo','fecha'} o None.

    Con `repo_prefijado` (modo repo) el picker arranca oculto y ese repo se muestra
    como una etiqueta clickeable; clickearla lo revela para elegir otro, igual que en
    modo todas.
    """

    BINDINGS = [
        Binding(str(i), f"quick_pick({i})", "", show=False, priority=True)
        for i in range(1, 6)
    ]

    def __init__(self, repos: list[str], repo_prefijado: str | None = None) -> None:
        super().__init__()
        self.repos = repos
        self.repo_prefijado = repo_prefijado
        self.repo_elegido: str | None = repo_prefijado

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg-nueva", classes="dlg"):
            yield Static("new task", classes="dlg-titulo")
            if self.repo_prefijado is not None:
                yield BotonCabecera(
                    f"repo: {self.repo_prefijado}", "cambiar-repo",
                    id="nueva-repo-fijo", classes="chip",
                )
            yield Input(placeholder="filter repo…", id="nueva-filtro")
            yield OptionList(id="nueva-repos")
            yield Input(placeholder="what did they ask for?", id="nueva-titulo")
            yield AtajosFecha(classes="fila-chips")
            with Horizontal(classes="fila-botones"):
                yield InputFecha(placeholder="YYYY-MM-DD (optional)", id="nueva-fecha")
                yield Button("create", id="nueva-crear", classes="chip")
                yield Button("cancel", id="nueva-cancelar", classes="chip")
            yield Static("", id="nueva-error", classes="error-linea")
            yield Static(
                "1-5 quick date · enter next/create · esc cancel",
                id="nueva-hint",
                classes="hint",
            )

    def on_mount(self) -> None:
        if self.repo_prefijado is not None:
            # El picker ni se pinta: `_pintar_repos` dispara un OptionHighlighted
            # (async) que pisaría este repo_elegido con el primero de la lista.
            self.repo_elegido = self.repo_prefijado
            self.query_one("#nueva-filtro", Input).display = False
            self.query_one("#nueva-repos", OptionList).display = False
            self.query_one("#nueva-titulo", Input).focus()
        else:
            self._pintar_repos(self.repos)
            self.query_one("#nueva-filtro", Input).focus()

    def on_boton_cabecera_pulsado(self, event: BotonCabecera.Pulsado) -> None:
        if event.accion != "cambiar-repo":
            return
        event.stop()
        self.query_one("#nueva-repo-fijo").remove()
        self._pintar_repos(self.repos)
        self.query_one("#nueva-filtro", Input).display = True
        self.query_one("#nueva-repos", OptionList).display = True
        self.query_one("#nueva-filtro", Input).focus()

    def _pintar_repos(self, repos: list[str]) -> None:
        lista = self.query_one("#nueva-repos", OptionList)
        lista.clear_options()
        self.repo_elegido = None
        if not repos:
            lista.add_option(Option("(no matching repos)", disabled=True))
            return
        lista.add_options([Option(r, id=r) for r in repos])
        lista.highlighted = 0
        self.repo_elegido = repos[0]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "nueva-filtro":
            return
        event.stop()
        aguja = event.value.strip().casefold()
        self._pintar_repos([r for r in self.repos if aguja in r.casefold()])

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option is not None and event.option.id:
            self.repo_elegido = event.option.id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id:
            self.repo_elegido = event.option.id
        self.query_one("#nueva-titulo", Input).focus()

    def action_quick_pick(self, indice: int) -> None:
        """Solo marca la fecha elegida: falta el título, así que no crea todavía."""
        self.query_one("#nueva-fecha", Input).value = AtajosFecha.fecha_por_indice(
            indice
        ).isoformat()

    def on_atajos_fecha_elegida(self, event: AtajosFecha.Elegida) -> None:
        event.stop()
        self.query_one("#nueva-fecha", Input).value = event.fecha.isoformat()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "nueva-filtro":
            self.query_one("#nueva-titulo", Input).focus()
        elif event.input.id == "nueva-titulo":
            self.query_one("#nueva-fecha", Input).focus()
        else:
            self._crear()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("nueva-"):
            return
        event.stop()
        if event.button.id == "nueva-crear":
            self._crear()
        else:
            self.dismiss(None)

    def _crear(self) -> None:
        error = self.query_one("#nueva-error", Static)
        titulo = self.query_one("#nueva-titulo", Input).value.strip()
        fecha = self.query_one("#nueva-fecha", Input).value.strip()
        if not self.repo_elegido:
            error.update("choose a repo from the list")
            filtro = self.query_one("#nueva-filtro", Input)
            if filtro.display:
                filtro.focus()
            return
        if not titulo:
            error.update("title is required")
            self.query_one("#nueva-titulo", Input).focus()
            return
        if fecha:
            try:
                date.fromisoformat(fecha)
            except ValueError:
                error.update("invalid date, use YYYY-MM-DD")
                self.query_one("#nueva-fecha", Input).focus()
                return
        self.dismiss({"repo": self.repo_elegido, "titulo": titulo, "fecha": fecha})


class ConfirmaScreen(DialogoModal):
    """Confirmación de una acción destructiva. Devuelve True/False."""

    BINDINGS = [
        Binding("y", "confirmar", "", show=False),
        Binding("n", "rechazar", "", show=False),
    ]

    def __init__(self, pregunta: str) -> None:
        super().__init__()
        self.pregunta = pregunta

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg-confirma", classes="dlg"):
            yield Static(self.pregunta, classes="dlg-titulo")
            with Horizontal(classes="fila-botones"):
                yield Button("yes, close", id="ok", classes="chip peligro")
                yield Button("cancel", id="no", classes="chip")
            yield Static("y close · n cancel", id="confirma-hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "ok")

    def action_confirmar(self) -> None:
        self.dismiss(True)

    def action_rechazar(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------------------------
# Pantalla principal
# ------------------------------------------------------------------------------------
class ListaScreen(Screen):
    BINDINGS = [
        # priority: DataTable también usa enter, y sin esto su binding (oculto) gana
        # el desempate y el footer se queda sin la pista más importante.
        Binding("enter", "ver", "view", priority=True),
        Binding("n", "nueva", "new"),
        Binding("d", "fecha", "date"),
        Binding("x", "cerrar", "close"),
        Binding("r", "refrescar", "refresh"),
        Binding("t", "toggle_repo", "repo"),
        Binding("q", "salir", "quit"),
        Binding("j", "abajo", "", show=False),
        Binding("k", "arriba", "", show=False),
        Binding("g", "inicio", "", show=False),
        Binding("G", "fin", "", show=False),
    ]

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend
        self.tareas: list[Tarea] = []
        self.repos: list[str] = []
        self.repo_actual: str | None = None
        self.modo_repo = False
        self.ultimo_ok: datetime | None = None
        self.ultimo_error: str | None = None
        self.cargando = True

    def compose(self) -> ComposeResult:
        with Horizontal(id="cabecera"):
            yield Static("", id="cab-titulo")
            yield BotonCabecera("", "toggle_repo", id="cab-toggle", classes="cab-btn")
            yield BotonCabecera("+ new", "nueva", id="cab-nueva", classes="cab-btn")
            yield BotonCabecera("⟳", "refrescar", id="cab-refrescar", classes="cab-btn")
        tabla: DataTable = DataTable(id="tabla")
        tabla.cursor_type = "row"
        tabla.show_header = False
        yield tabla
        yield Static("", id="vacio")
        yield Footer()

    def on_mount(self) -> None:
        tabla = self.query_one("#tabla", DataTable)
        tabla.add_column("vence", width=ANCHO_VENCE, key="vence")
        tabla.add_column("cliente", width=ANCHO_CLIENTE_MAX, key="cliente")
        tabla.add_column("título", width=40, key="titulo")
        self._pintar_vacio()
        self._pintar_cabecera()
        self.refrescar()
        self.cargar_repos()
        self.detectar_repo()
        self.set_interval(REFRESCO_SEGUNDOS, self.refrescar)
        self.set_interval(20.0, self._pintar_cabecera)

    # ------------------------------------------------------------------ datos
    @work(exclusive=True, group="listar")
    async def refrescar(self) -> None:
        try:
            self.tareas = await self.backend.listar()
            self.ultimo_ok = datetime.now()
            self.ultimo_error = None
        except Exception as err:  # noqa: BLE001 - cualquier fallo va a la UI, no al log
            self.ultimo_error = str(err)
            self.notify(f"couldn't read the Project: {err}", severity="error", timeout=6)
        finally:
            self.cargando = False
            self._pintar_tabla()

    @work(exclusive=True, group="repos")
    async def cargar_repos(self) -> None:
        try:
            self.repos = await self.backend.repos()
        except Exception:  # noqa: BLE001 - sin repos el modal lo dice y sigue
            self.repos = []

    @work(exclusive=True, group="repo-actual")
    async def detectar_repo(self) -> None:
        """Modo repo si el cwd cae dentro de uno con remote de GitHub; si no, modo
        todas en silencio (ver `Backend.repo_actual`)."""
        try:
            self.repo_actual = await self.backend.repo_actual()
        except Exception:  # noqa: BLE001 - cualquier fallo cae en modo todas
            self.repo_actual = None
        self.modo_repo = self.repo_actual is not None
        self.refresh_bindings()
        self._pintar_tabla()

    # ------------------------------------------------------------------ pintado
    @property
    def visibles(self) -> list[Tarea]:
        """`tareas`, filtradas al repo actual cuando el modo repo está activo.

        El filtro es client-side sobre la misma lista del Project; `tareas` sigue
        siendo el dato completo que trae `backend.listar()`.
        """
        if self.modo_repo and self.repo_actual:
            return [t for t in self.tareas if t.repo == self.repo_actual]
        return self.tareas

    def _pintar_tabla(self) -> None:
        tabla = self.query_one("#tabla", DataTable)
        recordado = tabla.cursor_row
        tabla.clear()
        visibles = self.visibles

        vacio = not visibles
        tabla.display = not vacio
        self.query_one("#vacio", Static).display = vacio
        self._pintar_vacio()
        self._pintar_cabecera()
        if vacio:
            return

        # Responsive: los anchos se derivan del pane, nunca son fijos.
        util = max(tabla.scrollable_content_region.width or self.size.width, 24)
        relleno = 2 * tabla.cell_padding
        disponible = util - ANCHO_VENCE - 3 * relleno
        # La columna de cliente se ajusta al contenido real: si sobra, el hueco se lo
        # queda el título, que es lo que se lee.
        largo_cliente = max((len(t.cliente) for t in visibles), default=ANCHO_CLIENTE_MIN)
        ancho_cliente = max(
            ANCHO_CLIENTE_MIN, min(ANCHO_CLIENTE_MAX, largo_cliente, disponible // 3)
        )
        ancho_titulo = max(6, disponible - ancho_cliente)

        claves = list(tabla.columns)
        tabla.columns[claves[1]].width = ancho_cliente
        tabla.columns[claves[2]].width = ancho_titulo

        hoy = date.today()
        for tarea in visibles:
            texto, estilo = etiqueta_vencimiento(tarea.vence, hoy)
            tabla.add_row(
                Text(texto, style=estilo),
                Text(acortar(tarea.cliente, ancho_cliente), style="dim"),
                Text(acortar(tarea.titulo, ancho_titulo)),
                key=tarea.item_id,
            )
        if recordado:
            tabla.cursor_coordinate = Coordinate(min(recordado, len(visibles) - 1), 0)

    def _pintar_vacio(self) -> None:
        widget = self.query_one("#vacio", Static)
        if self.cargando:
            widget.update(Text("loading tasks…", style="dim"))
        elif self.ultimo_error:
            widget.update(
                Text.assemble(
                    ("couldn't read the Project\n", "bold red"),
                    (f"{acortar(self.ultimo_error, 120)}\n\n", "dim"),
                    ("press r or click ⟳ to retry", "dim"),
                )
            )
        elif self.modo_repo and self.repo_actual:
            widget.update(
                Text.assemble(
                    (f"✓  nothing pending in {acortar(self.repo_actual, 40)}\n\n", "bold green"),
                    ('press t or click "all" to see the rest', "dim"),
                )
            )
        else:
            widget.update(
                Text.assemble(
                    ("✓  no pending tasks\n\n", "bold green"),
                    ('press n or click "+ new" to add one', "dim"),
                )
            )

    def _pintar_cabecera(self) -> None:
        visibles = self.visibles
        cuantas = len(visibles)
        hoy = date.today()
        vencidas = sum(1 for t in visibles if t.vence is not None and t.vence < hoy)

        # Segmentos (texto, estilo) del contador: la parte "overdue" usa el mismo rojo
        # semántico que las filas vencidas, para que el header se lea de un vistazo.
        if self.cargando:
            estado: list[tuple[str, str]] = [("loading…", "yellow")]
        elif self.ultimo_error:
            estado = [("no connection", "yellow")]
        elif cuantas == 0:
            estado = [("nothing pending", "yellow")]
        elif vencidas:
            estado = [
                (f"{cuantas} pending", "yellow"),
                (" · ", "dim"),
                (f"{vencidas} overdue", "bold red"),
            ]
        else:
            estado = [(f"{cuantas} pending", "yellow")]
        estado_texto = "".join(texto for texto, _ in estado)

        if self.modo_repo and self.repo_actual:
            etiqueta_titulo = self.repo_actual
        else:
            etiqueta_titulo = self.backend.titulo_project
        etiqueta_toggle = "all" if self.modo_repo else "this repo"
        toggle = self.query_one("#cab-toggle", BotonCabecera)
        toggle.display = self.repo_actual is not None
        toggle.update(Text(etiqueta_toggle))

        # Responsive: el título se trunca con puntos suspensivos antes de invadir a
        # los botones de la cabecera; el contador de pendientes siempre queda entero.
        separador = "  ·  "
        reservado = len("+ nueva") + 2 + len(f"⟳ {self._hace_cuanto()}") + 2
        if toggle.display:
            reservado += len(etiqueta_toggle) + 2
        disponible_titulo = max(6, self.size.width - reservado)
        disponible_repo = max(3, disponible_titulo - len(separador) - len(estado_texto))

        self.query_one("#cab-titulo", Static).update(
            Text.assemble(
                (acortar(etiqueta_titulo, disponible_repo), "bold"),
                (separador, "dim"),
                *estado,
            )
        )
        self.query_one("#cab-refrescar", BotonCabecera).update(
            Text(f"⟳ {self._hace_cuanto()}", style="dim")
        )

    def _hace_cuanto(self) -> str:
        if self.ultimo_ok is None:
            return "—"
        minutos = int((datetime.now() - self.ultimo_ok).total_seconds() // 60)
        return "just now" if minutos < 1 else f"{minutos}m ago"

    def on_resize(self, event: events.Resize) -> None:
        self._pintar_cabecera()
        if self.visibles:
            self.call_after_refresh(self._pintar_tabla)

    # ------------------------------------------------------------------ interacción
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_repo":
            return self.repo_actual is not None
        return True

    @property
    def seleccionada(self) -> Tarea | None:
        visibles = self.visibles
        if not visibles:
            return None
        fila = self.query_one("#tabla", DataTable).cursor_row
        return visibles[fila] if 0 <= fila < len(visibles) else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable ya distingue solo: un clic en otra fila mueve el cursor y un clic
        # en la fila ya seleccionada (o un doble clic, o enter) llega hasta acá.
        event.stop()
        self.action_ver()

    def on_boton_cabecera_pulsado(self, event: BotonCabecera.Pulsado) -> None:
        event.stop()
        if event.accion == "nueva":
            self.action_nueva()
        elif event.accion == "toggle_repo":
            self.action_toggle_repo()
        else:
            self.action_refrescar()

    def action_abajo(self) -> None:
        self.query_one("#tabla", DataTable).action_cursor_down()

    def action_arriba(self) -> None:
        self.query_one("#tabla", DataTable).action_cursor_up()

    def action_inicio(self) -> None:
        if self.visibles:
            self.query_one("#tabla", DataTable).cursor_coordinate = Coordinate(0, 0)

    def action_fin(self) -> None:
        if self.visibles:
            ultima = len(self.visibles) - 1
            self.query_one("#tabla", DataTable).cursor_coordinate = Coordinate(ultima, 0)

    def action_refrescar(self) -> None:
        self.refrescar()

    def action_toggle_repo(self) -> None:
        if self.repo_actual is None:
            return
        self.modo_repo = not self.modo_repo
        self._pintar_tabla()

    def action_salir(self) -> None:
        self.app.exit()

    @work
    async def action_ver(self) -> None:
        tarea = self.seleccionada
        if tarea is None:
            return
        siguiente = await self.app.push_screen_wait(DetalleScreen(tarea))
        if siguiente == "cerrar":
            await self._cerrar(tarea)
        elif siguiente == "fecha":
            await self._fechar(tarea)

    @work
    async def action_fecha(self) -> None:
        tarea = self.seleccionada
        if tarea is None:
            self.notify("no task selected", severity="warning", timeout=3)
            return
        await self._fechar(tarea)

    @work
    async def action_cerrar(self) -> None:
        tarea = self.seleccionada
        if tarea is None:
            self.notify("no task selected", severity="warning", timeout=3)
            return
        await self._cerrar(tarea)

    @work
    async def action_nueva(self) -> None:
        if not self.repos:
            self.cargar_repos()
        repo_prefijado = self.repo_actual if self.modo_repo else None
        datos = await self.app.push_screen_wait(NuevaScreen(self.repos, repo_prefijado))
        if not datos:
            return
        try:
            await self.backend.crear(datos["repo"], datos["titulo"], datos["fecha"] or None)
        except (ErrorGh, IndexError, OSError) as err:
            self.notify(f"couldn't create the task: {err}", severity="error", timeout=6)
            return
        self.notify("task created", timeout=3)
        self.refrescar()

    async def _fechar(self, tarea: Tarea) -> None:
        nueva = await self.app.push_screen_wait(FechaScreen(tarea.titulo, tarea.vence))
        if nueva is None:
            return
        try:
            await self.backend.fechar(tarea.item_id, nueva or None)
        except (ErrorGh, OSError) as err:
            self.notify(f"couldn't update the date: {err}", severity="error", timeout=6)
            return
        self.notify("due date updated" if nueva else "due date cleared", timeout=3)
        self.refrescar()

    async def _cerrar(self, tarea: Tarea) -> None:
        pregunta = f'close "{acortar(tarea.titulo, 60)}"?'
        if not await self.app.push_screen_wait(ConfirmaScreen(pregunta)):
            return
        try:
            await self.backend.cerrar(tarea)
        except (ErrorGh, OSError) as err:
            self.notify(f"couldn't close the task: {err}", severity="error", timeout=6)
            return
        self.notify(f"closed {tarea.cliente}", timeout=3)
        self.refrescar()


# ------------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------------
class TareasApp(App):
    TITLE = "tasks"
    # En un pane chico cada columna del footer cuenta, y la paleta no aporta acá.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { background: $background; }

    #cabecera { height: 1; width: 100%; }
    #cab-titulo { width: 1fr; height: 1; }
    .cab-btn { width: auto; height: 1; padding: 0 1; color: $accent; }
    .cab-btn:hover { background: $panel; text-style: bold; }

    #tabla { height: 1fr; width: 100%; scrollbar-size-vertical: 1; }
    #vacio {
        display: none; height: 1fr; width: 100%;
        content-align: center middle; text-align: center;
    }
    Footer { height: 1; }

    /* Modales: todo relativo al viewport, nada con medidas fijas. */
    DialogoModal { align: center middle; }
    .dlg {
        background: $surface; border: round $accent; padding: 0 1;
        width: 90%; max-width: 100; height: auto; max-height: 90%;
    }
    .dlg-titulo { height: 1; color: $accent; text-style: bold; }
    .fila-chips { height: 1; width: 100%; }
    .fila-botones { height: 1; width: 100%; }
    .error-linea { height: auto; color: $error; }
    .hint { height: 1; width: 100%; color: $foreground; text-style: dim; }

    /* Botones de una fila: clickeables sin engordar la UI. */
    /* El fondo va literal (color 8) y no por variable: las variables propias del theme
       todavía no existen cuando se parsea App.CSS. Sobre el panel del modal (color 0)
       queda un escalón visible, así que los botones se leen como botones. */
    .chip {
        height: 1; min-width: 0; width: auto; border: none;
        padding: 0 1; margin: 0 1 0 0; background: ansi_bright_black; color: $foreground;
        text-style: none;
    }
    .chip:hover { background: $accent; color: $background; text-style: bold; }
    .chip:focus { text-style: bold reverse; }
    .peligro { color: $error; }
    .peligro:hover { background: $error; color: $background; }

    #dlg-detalle { height: 90%; }
    #det-titulo { height: auto; max-height: 2; text-style: bold; }
    #det-meta { height: 1; color: $accent; }
    #det-cuerpo {
        height: 1fr; width: 100%; scrollbar-size-vertical: 1; border-top: solid $panel;
    }

    /* auto (no 1fr) para que la lista abrace a sus opciones y no deje hueco muerto */
    #nueva-repos {
        height: auto; max-height: 6; border: none; background: $background;
    }
    #nueva-filtro, #nueva-titulo, #nueva-fecha, #fecha-input {
        height: 1; border: none; padding: 0 1; background: $background; width: 1fr;
    }
    #nueva-fecha, #fecha-input { max-width: 24; }
    """

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend

    def get_default_screen(self) -> Screen:
        return ListaScreen(self.backend)

    def on_mount(self) -> None:
        self.register_theme(THEME_TERMINAL)
        self.theme = "terminal"
