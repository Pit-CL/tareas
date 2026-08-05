"""Interfaz textual: lista densa arriba, todo lo demás en modales.

Tres reglas mandan sobre el diseño:

* **Cabe en un pane chico.** 80x15 es el caso de referencia; nada usa alto fijo y los
  anchos se recalculan en cada `Resize`, truncando con puntos suspensivos.
* **Respira cuando hay lugar.** Desde `UMBRAL_HOLGADO` filas de pantalla los modales
  separan sus grupos con una línea en blanco y ganan padding; bajo eso vuelven al
  layout compacto. El alto de los diálogos siempre lo pone su contenido.
* **Se opera con mouse.** Cada acción tiene un blanco clickeable: filas, botones de la
  cabecera, teclas del footer y botones dentro de los modales. El teclado es atajo.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from time import monotonic

from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static
from textual.widgets.data_table import CellDoesNotExist
from textual.widgets.option_list import Option

from .datos import (
    ANCHO_REPEAT,
    ANCHO_VENCE,
    LIMITE_ITEMS,
    REPETICIONES,
    SECUNDARIO,
    Backend,
    ErrorGh,
    ErrorParcial,
    Tarea,
    acortar,
    chip_pr,
    componer_cuerpo,
    etiqueta_vencimiento,
    fecha_larga,
    mas_un_mes,
    ordenar,
    parsear_fecha,
    proxima_fecha,
    resumen_pr,
)

REFRESCO_SEGUNDOS = 300.0
ANCHO_CLIENTE_MAX = 22
ANCHO_CLIENTE_MIN = 10
# Cuánto se sostiene en la lista una tarea recién creada que el Project todavía no
# devuelve. Medido contra el Project real: un item recién agregado tarda entre 4 y 6
# segundos en aparecer en la lectura (da igual el camino: `gh project item-list` tiene
# el mismo retardo). El refresco que dispara el alta llega antes que eso, así que sin
# esta ventana la fila aparecía y se borraba sola al segundo. 120 s es techo de sobra
# y a la vez garantiza que una tarea que GitHub nunca devuelva no se quede pegada.
VIDA_NACIENTES = 120.0
# Filas de pantalla desde las que los modales respiran. El corte no es estético: el
# modal más alto (nueva tarea, con el picker desplegado) mide 21 filas holgado, así que
# 22 es el primer alto donde entra entero con margen. Debajo, layout compacto.
UMBRAL_HOLGADO = 22
ANCHO_ETIQUETA_REPEAT = max(len(r) for r in REPETICIONES)

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
class TablaTareas(DataTable):
    """DataTable cuya fila bajo el cursor se lee entera, sin `dim`.

    `cursor_foreground_priority="css"` (el default) solo pisa el **color** del
    renderable: Textual lo aplica como `Style.from_color(color=...)` en el `post_style`
    de la celda, y ahí no hay forma de tocar los atributos. Una celda marcada `dim`
    -la columna de repo, o un vencimiento lejano- se seguía difuminando contra el fondo
    del cursor (el filtro ANSI resuelve `dim` mezclando texto y fondo), y sobre el
    ámbar quedaba ilegible. Sumamos `dim=False` a ese mismo `post_style`, que es el
    único punto del pipeline donde se puede cancelar un atributo del renderable.

    El `lru_cache` no es optimización: DataTable llama a `cache_clear()` sobre este
    método al invalidar sus cachés, así que sin el decorador la llamada revienta.
    """

    @functools.lru_cache(maxsize=32)  # noqa: B019 - misma estrategia que DataTable
    def _get_styles_to_render_cell(
        self,
        is_header_cell: bool,
        is_row_label_cell: bool,
        is_fixed_style_cell: bool,
        hover: bool,
        cursor: bool,
        show_cursor: bool,
        show_hover_cursor: bool,
        has_css_foreground_priority: bool,
        has_css_background_priority: bool,
    ) -> tuple[Style, Style]:
        component_style, post_style = super()._get_styles_to_render_cell(
            is_header_cell,
            is_row_label_cell,
            is_fixed_style_cell,
            hover,
            cursor,
            show_cursor,
            show_hover_cursor,
            has_css_foreground_priority,
            has_css_background_priority,
        )
        if cursor and show_cursor and not is_header_cell:
            post_style += Style(dim=False)
        return component_style, post_style


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
    """Base de los modales: `esc` cierra, un clic fuera del diálogo también, y el
    diálogo respira si la pantalla da para ello."""

    BINDINGS = [Binding("escape", "cancelar", "volver")]

    def on_mount(self) -> None:
        self._respirar()
        self.al_montar()

    def al_montar(self) -> None:
        """Gancho de montaje de los modales: `on_mount` ya lo usa esta base."""

    def on_resize(self, event: events.Resize) -> None:
        self._respirar()

    def _respirar(self) -> None:
        """Marca el diálogo como holgado o compacto según el alto de la pantalla."""
        holgado = self.app.size.height >= UMBRAL_HOLGADO
        for dialogo in self.query(".dlg"):
            dialogo.set_class(holgado, "holgado")

    def _avisar(self, mensaje: str) -> None:
        """Muestra un error en la línea del hint: en un pane chico no sobran filas."""
        for error in self.query(".error-linea"):
            error.update(mensaje)
            error.display = True
        for hint in self.query(".hint"):
            hint.display = False

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
    """Detalle del issue. Devuelve 'cerrar', 'fecha' o None.

    Los atajos son los MISMOS que en la lista (`x` cierra, `d` fecha): acá no hay
    ningún Input que se coma las letras, así que el gesto no cambia según dónde
    estés parado.
    """

    BINDINGS = [
        Binding("j", "abajo", "", show=False),
        Binding("k", "arriba", "", show=False),
        Binding("x", "cerrar_tarea", "", show=False),
        Binding("d", "cambiar_fecha", "", show=False),
    ]

    def __init__(self, tarea: Tarea) -> None:
        super().__init__()
        self.tarea = tarea

    def compose(self) -> ComposeResult:
        # `Markdown` se importa acá y no arriba porque arrastra markdown-it y pygments:
        # 39 ms de los 245 que tarda en importarse la app, pagados en CADA arranque
        # para un widget que solo aparece si el usuario abre un detalle.
        from textual.widgets import Markdown

        with Vertical(id="dlg-detalle", classes="dlg"):
            yield Static(self.tarea.titulo, id="det-titulo")
            yield Static(self._meta(), id="det-meta")
            # La fila solo existe si hay algo que decir: en un pane de 15 filas, una
            # línea en blanco es una línea menos de descripción.
            actividad = self._actividad()
            if actividad is not None:
                yield Static(actividad, id="det-actividad")
            with VerticalScroll(id="det-cuerpo"):
                yield Markdown(self.tarea.cuerpo or "_(no description)_")
            yield Static("", classes="respiro")
            with Horizontal(classes="fila-botones"):
                yield Button("\\[x·close task]", id="det-cerrar", classes="chip peligro")
                yield Button("\\[d·change date]", id="det-fecha", classes="chip")
                yield Button("\\[back]", id="det-volver", classes="chip secundario")
            yield Static(
                # x y d ya van en el label de su botón (`[x·close task]`,
                # `[d·change date]`): repetirlos acá era la misma info dos veces.
                "j/k scroll · esc back", id="det-hint", classes="hint"
            )

    def _meta(self) -> Text:
        """Cliente · vencimiento · repetición, con el vencimiento en su color semántico.

        Misma jerarquía que la columna de la lista (vencida en rojo, hoy en acento,
        lejana en color 7): abrir el detalle no debería perder el dato que más se mira.
        Lo que no lleva estilo hereda el `$accent` que el CSS le pone a `#det-meta`.
        """
        hoy = date.today()
        _, estilo = etiqueta_vencimiento(self.tarea.vence, hoy)
        partes = [Text(self.tarea.cliente), Text(fecha_larga(self.tarea.vence, hoy), estilo)]
        if self.tarea.repeat:
            partes.append(Text(f"↻ repeats {self.tarea.repeat}"))
        return Text(" · ").join(partes)

    def _actividad(self) -> Text | None:
        """PR vinculado y comentarios; None si la tarea no tiene ni lo uno ni lo otro.

        Es la versión en palabras del chip de la lista: acá sí hay lugar para separar
        «merged» de «CI verde» y «closed» de «CI roja», que en un glifo se confundirían.
        El PR se lleva el color del chip -verde, rojo o color 7- para que una CI rota se
        vea al abrir el detalle sin tener que leer la frase.
        """
        partes: list[Text] = []
        if self.tarea.pr is not None:
            _, estilo = chip_pr(self.tarea.pr)
            partes.append(Text(resumen_pr(self.tarea.pr), estilo))
        if self.tarea.comentarios:
            plural = "" if self.tarea.comentarios == 1 else "s"
            partes.append(Text(f"{self.tarea.comentarios} comment{plural}", SECUNDARIO))
        return Text(" · ").join(partes) if partes else None

    def al_montar(self) -> None:
        self.query_one("#det-cuerpo").focus()

    def _respirar(self) -> None:
        super()._respirar()
        # El cuerpo se ajusta a lo que hay: una nota de una línea no debe abrir un
        # modal gigante. El techo lo pone la pantalla menos el resto del diálogo
        # (bordes, título, meta, separador, botones, hint y, si respira, su padding).
        alto = self.app.size.height
        # La línea de actividad, cuando está, es una fila más de diálogo que el cuerpo
        # tiene que devolver: sin descontarla el modal crecía hasta pasarse del borde.
        extra = 1 if self.query("#det-actividad") else 0
        for cuerpo in self.query("#det-cuerpo"):
            cuerpo.styles.max_height = max(
                3, alto - extra - (11 if alto >= UMBRAL_HOLGADO else 8)
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss({"det-cerrar": "cerrar", "det-fecha": "fecha"}.get(event.button.id or ""))

    def action_abajo(self) -> None:
        self.query_one("#det-cuerpo", VerticalScroll).scroll_down()

    def action_arriba(self) -> None:
        self.query_one("#det-cuerpo", VerticalScroll).scroll_up()

    def action_cerrar_tarea(self) -> None:
        self.dismiss("cerrar")

    def action_cambiar_fecha(self) -> None:
        self.dismiss("fecha")


class FechaScreen(DialogoModal):
    """Elegir vencimiento. Devuelve 'AAAA-MM-DD', '' para quitarlo, o None.

    Todo lo que no sea un quick-pick va con `ctrl+letra`: hay un Input con el foco y
    Textual deja que se quede con cualquier tecla imprimible antes que un binding,
    por prioritario que sea. `ctrl+enter` queda de bonus para quien tenga el protocolo
    de teclado de Kitty de punta a punta, pero NUNCA se anuncia (ver el hint).
    """

    BINDINGS = [
        Binding(str(i), f"quick_pick({i})", "", show=False, priority=True)
        for i in range(1, 6)
    ] + [
        Binding("ctrl+enter,ctrl+s", "guardar", "", show=False, priority=True),
        Binding("ctrl+x", "quitar", "", show=False, priority=True),
    ]

    def __init__(self, titulo: str, actual: date | None) -> None:
        super().__init__()
        self.titulo = titulo
        self.actual = actual

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg-fecha", classes="dlg"):
            yield Static(f"due date · {acortar(self.titulo, 120)}", classes="dlg-titulo")
            yield Static("", classes="respiro")
            yield AtajosFecha(classes="fila-chips")
            yield Static("", classes="respiro")
            with Horizontal(classes="fila-botones"):
                yield InputFecha(
                    value=self.actual.isoformat() if self.actual else "",
                    placeholder="YYYY-MM-DD",
                    id="fecha-input",
                )
                yield Button("\\[^s·save]", id="fecha-guardar", classes="chip primario")
                yield Button("\\[^x·clear]", id="fecha-quitar", classes="chip peligro")
                yield Button("\\[cancel]", id="fecha-cancelar", classes="chip secundario")
            yield Static("", id="fecha-error", classes="error-linea")
            yield Static(
                # 1-5 (quick-picks), ^s y ^x ya van en el label de su propio widget:
                # lo único sin representación visible en la pantalla es esc.
                "esc cancel",
                id="fecha-hint",
                classes="hint",
            )

    def al_montar(self) -> None:
        self.query_one("#fecha-input", Input).focus()

    def action_quick_pick(self, indice: int) -> None:
        """Aplica y guarda de inmediato: `d`→número son dos teclas para fechar."""
        self.dismiss(AtajosFecha.fecha_por_indice(indice).isoformat())

    def action_guardar(self) -> None:
        self._guardar()

    def action_quitar(self) -> None:
        self.dismiss("")

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
            self._avisar("invalid format, use YYYY-MM-DD")
            return
        self.dismiss(texto)


class NuevaScreen(DialogoModal):
    """Alta de tarea. Devuelve {'repo','titulo','notas','fecha','repeat'} o None.

    Con `repo_prefijado` (modo repo) el picker arranca oculto y ese repo se muestra
    como una etiqueta clickeable; clickearla lo revela para elegir otro, igual que en
    modo todas.

    Los repos NO llegan por valor sino por `traer_repos`, una corrutina que espera a la
    carga en curso: recibir la lista de la lista principal la congelaba en el momento de
    abrir el modal, y como esa carga REASIGNA el atributo, abrir «nueva» antes de que
    llegaran dejaba el picker vacío para siempre.
    """

    BINDINGS = [
        Binding(str(i), f"quick_pick({i})", "", show=False, priority=True)
        for i in range(1, 6)
    ] + [
        Binding("ctrl+enter,ctrl+s", "crear", "", show=False, priority=True),
        Binding("ctrl+r", "ciclar_repeat", "", show=False, priority=True),
        Binding("ctrl+p", "cambiar_repo", "", show=False, priority=True),
    ]

    def __init__(
        self,
        traer_repos: Callable[[], Awaitable[list[str]]],
        repo_prefijado: str | None = None,
    ) -> None:
        super().__init__()
        self._traer_repos = traer_repos
        self.repos: list[str] = []
        self.cargando_repos = True
        self.repo_prefijado = repo_prefijado
        self.repo_elegido: str | None = repo_prefijado
        self.repeticion: str = "none"

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg-nueva", classes="dlg"):
            yield Static("new task", classes="dlg-titulo")
            if self.repo_prefijado is not None:
                yield BotonCabecera(
                    f"\\[^p·repo: {self.repo_prefijado}]", "cambiar-repo",
                    id="nueva-repo-fijo", classes="chip",
                )
            yield Input(placeholder="filter repo…", id="nueva-filtro")
            yield OptionList(id="nueva-repos")
            yield Static("", classes="respiro")
            # Los placeholders nombran su campo ("title ·", "description ·") en vez de
            # solo insinuarlo: dos Input de una fila, pegados y sin etiqueta, se leen
            # como un párrafo y el segundo pasaba desapercibido. Y dice "description"
            # -la palabra de GitHub- porque es la que la gente va a buscar.
            yield Input(placeholder="title · what did they ask for?", id="nueva-titulo")
            yield Input(
                placeholder="description · notes, links, context (optional)",
                id="nueva-notas",
            )
            yield Static("", classes="respiro")
            yield AtajosFecha(classes="fila-chips")
            with Horizontal(classes="fila-fecha"):
                yield InputFecha(placeholder="YYYY-MM-DD (optional)", id="nueva-fecha")
                yield Button(self._etiqueta_repeat(), id="nueva-repeat", classes="chip")
            yield Static("", classes="respiro")
            with Horizontal(classes="fila-botones"):
                yield Button("\\[^s·create]", id="nueva-crear", classes="chip primario")
                yield Button("\\[cancel]", id="nueva-cancelar", classes="chip secundario")
            yield Static("", id="nueva-error", classes="error-linea")
            yield Static(
                # 1-5 (quick-picks), ^r y ^s ya van en el label de su propio widget:
                # lo único sin representación visible en la pantalla es esc.
                #
                # `^enter` (Ctrl+Enter) no se anuncia en ningún lado, ni acá ni en el
                # botón de crear, porque no es un atajo confiable: la mayoría de las
                # terminales (sin el protocolo de teclado de Kitty de punta a punta,
                # p. ej. cualquier sesión con tmux/SSH de por medio) mandan el MISMO
                # byte que Enter (\r) para ambos, así que Textual nunca ve "ctrl+enter"
                # y el binding no dispara — verificado con XTermParser().feed("\r").
                # `ctrl+s` sí llega distinguible (Textual desactiva IXON/IXOFF, así
                # que ni el flow control de la tty se lo come) y es el que el botón
                # `[^s·create]` anuncia de verdad.
                "esc cancel",
                id="nueva-hint",
                classes="hint",
            )

    def al_montar(self) -> None:
        if self.repo_prefijado is not None:
            # El picker ni se pinta: `_pintar_repos` dispara un OptionHighlighted
            # (async) que pisaría este repo_elegido con el primero de la lista.
            self.repo_elegido = self.repo_prefijado
            self.query_one("#nueva-filtro", Input).display = False
            self.query_one("#nueva-repos", OptionList).display = False
            self.query_one("#nueva-titulo", Input).focus()
        else:
            self._pintar_repos([])  # placeholder "loading repos…" hasta que lleguen
            self.query_one("#nueva-filtro", Input).focus()
        self._cargar_repos()

    @work
    async def _cargar_repos(self) -> None:
        try:
            repos = await self._traer_repos()
        except Exception:  # noqa: BLE001 - sin repos el picker lo dice y sigue
            repos = []
        self.cargando_repos = False
        self.repos = repos
        # Solo se repinta si el picker está a la vista: con un repo prefijado, pintarlo
        # borraría el `repo_elegido` que el usuario ya tiene puesto.
        if self.query_one("#nueva-repos", OptionList).display:
            self._filtrar()

    def on_boton_cabecera_pulsado(self, event: BotonCabecera.Pulsado) -> None:
        if event.accion != "cambiar-repo":
            return
        event.stop()
        self.action_cambiar_repo()

    def action_cambiar_repo(self) -> None:
        """Revela el picker para elegir otro repo (solo aplica en modo repo)."""
        fijo = self.query("#nueva-repo-fijo")
        if not fijo:  # el picker ya está a la vista: no hay nada que revelar
            return
        fijo.remove()
        self.query_one("#nueva-filtro", Input).display = True
        self.query_one("#nueva-repos", OptionList).display = True
        self._filtrar()
        self.query_one("#nueva-filtro", Input).focus()

    def _filtrar(self) -> None:
        aguja = self.query_one("#nueva-filtro", Input).value.strip().casefold()
        self._pintar_repos([r for r in self.repos if aguja in r.casefold()])

    def _pintar_repos(self, repos: list[str]) -> None:
        lista = self.query_one("#nueva-repos", OptionList)
        lista.clear_options()
        self.repo_elegido = None
        if not repos:
            lista.add_option(
                Option(
                    "loading repos…" if self.cargando_repos else "(no matching repos)",
                    disabled=True,
                )
            )
            return
        lista.add_options([Option(r, id=r) for r in repos])
        lista.highlighted = 0
        self.repo_elegido = repos[0]

    # ------------------------------------------------------------------ repetición
    def _etiqueta_repeat(self) -> str:
        """Etiqueta del chip, siempre del mismo largo.

        Textual no vuelve a medir un Button con `width: auto` cuando le cambia el
        label: el texto nuevo se cortaría a mitad. Rellenando todos los valores al
        largo del más largo, el chip nunca cambia de ancho -así que ni se corta ni
        hace bailar la fila al ciclar-.
        """
        return f"↻ ^r·repeat: {self.repeticion:<{ANCHO_ETIQUETA_REPEAT}}"

    def action_ciclar_repeat(self) -> None:
        siguiente = (REPETICIONES.index(self.repeticion) + 1) % len(REPETICIONES)
        self.repeticion = REPETICIONES[siguiente]
        chip = self.query_one("#nueva-repeat", Button)
        chip.label = self._etiqueta_repeat()
        chip.set_class(self.repeticion != "none", "primario")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "nueva-filtro":
            return
        event.stop()
        self._filtrar()

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

    def action_crear(self) -> None:
        self._crear()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        encadenado = {
            "nueva-filtro": "nueva-titulo",
            "nueva-titulo": "nueva-notas",
            "nueva-notas": "nueva-fecha",
        }.get(event.input.id or "")
        if encadenado:
            self.query_one(f"#{encadenado}", Input).focus()
        else:
            self._crear()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("nueva-"):
            return
        event.stop()
        if event.button.id == "nueva-crear":
            self._crear()
        elif event.button.id == "nueva-repeat":
            self.action_ciclar_repeat()
        else:
            self.dismiss(None)

    def _crear(self) -> None:
        titulo = self.query_one("#nueva-titulo", Input).value.strip()
        notas = self.query_one("#nueva-notas", Input).value.strip()
        fecha = self.query_one("#nueva-fecha", Input).value.strip()
        if not self.repo_elegido:
            self._avisar(
                "still loading repos…" if self.cargando_repos else "choose a repo from the list"
            )
            filtro = self.query_one("#nueva-filtro", Input)
            if filtro.display:
                filtro.focus()
            return
        if not titulo:
            self._avisar("title is required")
            self.query_one("#nueva-titulo", Input).focus()
            return
        if fecha:
            try:
                date.fromisoformat(fecha)
            except ValueError:
                self._avisar("invalid date, use YYYY-MM-DD")
                self.query_one("#nueva-fecha", Input).focus()
                return
        repeticion = self.repeticion
        if repeticion != "none" and not fecha:
            # Sin vencimiento no hay desde dónde contar el intervalo: se crea igual,
            # pero el usuario tiene que enterarse de que la repetición no quedó.
            self.app.notify(
                "↻ repeat needs a due date: task created without it",
                severity="warning",
                timeout=6,
            )
            repeticion = "none"
        self.dismiss(
            {
                "repo": self.repo_elegido,
                "titulo": titulo,
                "notas": notas,
                "fecha": fecha,
                "repeat": repeticion,
            }
        )


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
            yield Static("", classes="respiro")
            with Horizontal(classes="fila-botones"):
                yield Button("\\[y·yes, close]", id="ok", classes="chip peligro")
                yield Button("\\[n·cancel]", id="no", classes="chip secundario")
            # y y n ya van en el label de su botón (`[y·yes, close]`, `[n·cancel]`):
            # lo único sin representación visible en la pantalla es esc.
            yield Static("esc cancel", id="confirma-hint", classes="hint")

    def al_montar(self) -> None:
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
        # item_id -> etiqueta de la escritura en vuelo ("closing…", "saving…"). La fila
        # sigue en pantalla mientras corren las llamadas a `gh`, así que sin esto un
        # segundo `x` abría otra confirmación sobre la MISMA tarea y la cerraba dos
        # veces (`gh issue close` sobre un issue ya cerrado sale 0), duplicando de paso
        # la siguiente ocurrencia de las repetitivas.
        self.ocupadas: dict[str, str] = {}
        # True mientras hay un modal de `_mostrar_modal` en pantalla. Un doble clic o
        # dos teclas casi simultáneas agendan dos workers ANTES de que el primero
        # llegue a montar su modal (`action_ver`, `action_fecha`, `action_cerrar` y
        # `action_nueva` no son exclusivos: ver el comentario de `_mostrar_modal`), y
        # sin este guard el segundo disparo apilaba una SEGUNDA pantalla idéntica.
        self._modal_abierto = False
        self.limite_avisado = False
        # item_ids cerrados desde acá que el Project todavía puede devolver como
        # pendientes: el Status "Done" lo pone un workflow de Projects DESPUÉS de que
        # `gh issue close` vuelve, así que el refresco inmediato llega a destiempo y
        # resucitaría la fila que el usuario acaba de ver desaparecer.
        #
        # Se persiste con el caché (no solo en memoria): al vivir únicamente en el
        # proceso, reiniciar la app resucitaba la tarea desde el caché de disco y
        # cerrarla de nuevo creaba una SEGUNDA ocurrencia de la repetitiva en GitHub.
        self.cerradas: set[str] = set()
        # El espejo de `cerradas`: item_id -> reloj monotónico del alta. Son tareas que
        # se crearon desde acá y que el Project TODAVÍA no devuelve (ver
        # VIDA_NACIENTES). Sin esto el refresco que dispara el alta pisaba la lista con
        # una lectura que aún no las trae, y la fila recién creada desaparecía sola.
        # No se persiste: para cuando la app reinicia, GitHub ya la devuelve.
        self.nacientes: dict[str, float] = {}
        self.aviso_campo_dado = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="cabecera"):
            yield Static("", id="cab-titulo")
            yield BotonCabecera("", "toggle_repo", id="cab-toggle", classes="cab-btn")
            yield BotonCabecera("+ new", "nueva", id="cab-nueva", classes="cab-btn")
            yield BotonCabecera("⟳", "refrescar", id="cab-refrescar", classes="cab-btn")
        tabla = TablaTareas(id="tabla")
        tabla.cursor_type = "row"
        tabla.show_header = False
        yield tabla
        yield Static("", id="vacio")
        yield Footer()

    def on_mount(self) -> None:
        tabla = self.query_one("#tabla", TablaTareas)
        tabla.add_column("vence", width=ANCHO_VENCE, key="vence")
        tabla.add_column("↻", width=ANCHO_REPEAT, key="repeat")
        tabla.add_column("cliente", width=ANCHO_CLIENTE_MAX, key="cliente")
        tabla.add_column("título", width=40, key="titulo")
        self._precargar()
        self._pintar_vacio()
        self._pintar_cabecera()
        self.refrescar()
        self.cargar_repos()
        self.detectar_repo()
        self.set_interval(REFRESCO_SEGUNDOS, self.refrescar)
        self.set_interval(20.0, self._pintar_cabecera)

    def _precargar(self) -> None:
        """Pinta la última lectura guardada en disco antes de tocar la red.

        Las tres llamadas a `gh` del arranque no vuelven antes de ~0,3 s y la lista
        tardaba ~1,3 s en aparecer. Con el caché la pantalla nace poblada y el
        refresco real la corrige por detrás; la antigüedad del dato no se disimula,
        va en el `⟳ Xm ago` de la cabecera.
        """
        guardado = self.backend.instantanea()
        if guardado.tareas is None:
            return
        # El filtro de cerradas se aplica ANTES de pintar: el caché se escribió con la
        # lista cruda del Project, que sigue trayendo lo que cerramos hasta que su
        # workflow voltea el Status.
        self.cerradas = set(guardado.cerradas)
        self.tareas = [t for t in guardado.tareas if t.item_id not in self.cerradas]
        self.ultimo_ok = guardado.momento
        self.repos = guardado.repos
        if guardado.repo_actual:
            # Se aplica el filtro desde el primer frame: sin esto la lista se pintaba
            # entera y saltaba al repo actual recién cuando volvía `gh repo view`.
            self.repo_actual = guardado.repo_actual
            self.modo_repo = True
        self.cargando = False
        self._pintar_tabla()

    # ------------------------------------------------------------------ datos
    @work(exclusive=True, group="listar")
    async def refrescar(self) -> None:
        try:
            llegadas = await self.backend.listar()
            # Se recuerdan solo las que el Project sigue devolviendo: en cuanto deja de
            # mandarlas el filtro sobra y el set se vacía solo, sin caducidad inventada.
            previas = set(self.cerradas)
            self.cerradas &= {t.item_id for t in llegadas}
            if self.cerradas != previas:  # el refresco corre cada 5 min: no reescribir por nada
                self._recordar_cerradas()
            self.tareas = self._conservar_nacientes(
                [t for t in llegadas if t.item_id not in self.cerradas]
            )
            self.ultimo_ok = datetime.now()
            self.ultimo_error = None
            self._avisar_limite()
            self._avisar_campo()
        except Exception as err:  # noqa: BLE001 - cualquier fallo va a la UI, no al log
            self.ultimo_error = str(err)
            self.notify(f"couldn't read the Project: {err}", severity="error", timeout=6)
        finally:
            self.cargando = False
            self._pintar_tabla()

    def _recordar_cerradas(self) -> None:
        """Baja el set de cerradas al disco, junto al caché de la lista."""
        self.backend.recordar_cerradas(self.cerradas)

    def _nacer(self, tarea: Tarea) -> None:
        """Marca una tarea recién creada para que el próximo refresco no se la lleve."""
        self.nacientes[tarea.item_id] = monotonic()

    def _conservar_nacientes(self, llegadas: list[Tarea]) -> list[Tarea]:
        """Vuelve a meter en la lista las recién creadas que el Project aún no manda.

        Se deja de sostener una tarea en cuanto GitHub empieza a devolverla (que es lo
        normal, a los pocos segundos) o cuando se pasó de `VIDA_NACIENTES`: así una
        tarea que por lo que sea no vuelva nunca no se queda pegada para siempre.

        La fila que se conserva es la de `self.tareas` y no la que se guardó al crearla,
        para no perder lo que el usuario le haya hecho mientras tanto (fecharla, p.ej.).
        """
        traidas = {t.item_id for t in llegadas}
        limite = monotonic() - VIDA_NACIENTES
        self.nacientes = {
            item_id: nacimiento
            for item_id, nacimiento in self.nacientes.items()
            if item_id not in traidas and nacimiento > limite
        }
        if not self.nacientes:
            return llegadas
        conocidas = {t.item_id: t for t in self.tareas}
        return ordenar(
            [*llegadas, *(conocidas[i] for i in self.nacientes if i in conocidas)]
        )

    def _avisar_campo(self) -> None:
        """Rompe el silencio cuando el campo de fecha ya no se llama como dice la config.

        Sin esto la app pintaba TODAS las tareas como «no due date» y no había forma
        de distinguirlo de un Project sin vencimientos puestos. Va como error y una
        sola vez por sesión: en cada refresco sería ruido.
        """
        aviso = self.backend.aviso_campo
        if not aviso or self.aviso_campo_dado:
            return
        self.aviso_campo_dado = True
        self.notify(aviso, severity="error", timeout=15)

    def _avisar_limite(self) -> None:
        """Una sola vez por sesión: repetirlo cada 5 minutos sería ruido, no información."""
        if not self.backend.truncado or self.limite_avisado:
            return
        self.limite_avisado = True
        self.notify(
            f"the Project returned the full {LIMITE_ITEMS}-item limit: some pending "
            "tasks may be missing — archive the done ones",
            severity="warning",
            timeout=10,
        )

    @work(exclusive=True, group="repos")
    async def cargar_repos(self) -> None:
        try:
            self.repos = await self.backend.repos()
        except Exception:  # noqa: BLE001 - sin repos el modal lo dice y sigue
            self.repos = []

    async def obtener_repos(self) -> list[str]:
        """Repos del owner, esperando a la carga en curso si todavía no llegaron.

        Es lo que consume `NuevaScreen`: pasarle `self.repos` por valor congelaba una
        lista vacía si el modal abría antes de que terminara la carga.
        """
        if not self.repos:
            await self.cargar_repos().wait()
        return self.repos

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

    @property
    def _pintable(self) -> bool:
        """False cuando la pantalla ya se está yendo y no queda nada que pintar.

        Salir con una llamada a `gh` en vuelo cancela su worker, pero el `finally` de
        `refrescar` (y el cierre de `detectar_repo`) igual intentan repintar. Para
        entonces Textual ya sacó a los hijos aunque la Screen siga marcada como
        montada, así que hay que preguntar por el widget y no por `is_mounted`: sin
        esto reventaba con NoMatches, y `bin/tareas` lee esa caída como crash y
        relanza la app en bucle.
        """
        return bool(self.query("#tabla"))

    def _pintar_tabla(self) -> None:
        if not self._pintable:
            return
        tabla = self.query_one("#tabla", TablaTareas)
        recordado = self._item_bajo_cursor(tabla)
        fila_recordada = tabla.cursor_row
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
        disponible = util - ANCHO_VENCE - ANCHO_REPEAT - 4 * relleno
        # La columna de cliente se ajusta al contenido real: si sobra, el hueco se lo
        # queda el título, que es lo que se lee.
        largo_cliente = max((len(t.cliente) for t in visibles), default=ANCHO_CLIENTE_MIN)
        ancho_cliente = max(
            ANCHO_CLIENTE_MIN, min(ANCHO_CLIENTE_MAX, largo_cliente, disponible // 3)
        )
        ancho_titulo = max(6, disponible - ancho_cliente)

        claves = list(tabla.columns)
        tabla.columns[claves[2]].width = ancho_cliente
        tabla.columns[claves[3]].width = ancho_titulo

        hoy = date.today()
        for tarea in visibles:
            ocupada = self.ocupadas.get(tarea.item_id)
            if ocupada:
                vence = Text(ocupada, style="bold yellow")
            else:
                texto, estilo = etiqueta_vencimiento(tarea.vence, hoy)
                vence = Text(texto, style=estilo)
            tabla.add_row(
                vence,
                # En acento y no en dim: una columna de un carácter ya es discreta, y
                # el dim de esta paleta se queda en 3,98:1 sobre el fondo claro.
                Text("↻" if tarea.repeat else "", style="yellow"),
                # Color 7 y no dim: el repo se LEE (ver SECUNDARIO).
                Text(acortar(tarea.cliente, ancho_cliente), style=SECUNDARIO),
                self._celda_titulo(tarea, ancho_titulo),
                key=tarea.item_id,
            )
        self._reubicar_cursor(tabla, recordado, fila_recordada, visibles)

    @staticmethod
    def _celda_titulo(tarea: Tarea, ancho: int) -> Text:
        """Título, con el chip del PR pegado al borde derecho de la columna.

        El chip NO tiene columna propia a propósito. Una columna de DataTable ocupa su
        ancho siempre, y la mayoría de las tareas no tiene PR: a 80 columnas serían 7 de
        las ~62 útiles regaladas para dejar casi todas las filas en blanco. Yendo dentro
        de la columna elástica, la tarea sin PR no paga nada, y la que sí lo tiene igual
        queda alineada con las demás, porque la columna mide lo mismo en todas las filas.
        """
        texto, estilo = chip_pr(tarea.pr)
        hueco = ancho - len(texto) - 1
        # Con el pane tan angosto que no quedan ni tres letras de título, el chip sobra:
        # una fila que solo dice "#45✓" no comunica de qué tarea habla.
        if not texto or hueco < 3:
            return Text(acortar(tarea.titulo, ancho))
        titulo = acortar(tarea.titulo, hueco)
        return Text.assemble(titulo, " " * (ancho - len(titulo) - len(texto)), (texto, estilo))

    @staticmethod
    def _item_bajo_cursor(tabla: TablaTareas) -> str | None:
        """item_id de la fila bajo el cursor, leído de la propia tabla.

        No sirve mirar `self.tareas`: para cuando repintamos ya trae el orden nuevo, y
        la clave de cada fila es justamente el item_id, así que la tabla es la única
        fuente que todavía describe lo que el usuario tenía seleccionado.
        """
        try:
            return tabla.coordinate_to_cell_key(tabla.cursor_coordinate).row_key.value
        except CellDoesNotExist:
            return None

    @staticmethod
    def _reubicar_cursor(
        tabla: TablaTareas, item_id: str | None, fila: int, visibles: list[Tarea]
    ) -> None:
        """Deja el cursor sobre la MISMA tarea tras repintar.

        `ordenar()` resortea por (vence, título), así que recordar el número de fila
        dejaba el cursor sobre otra tarea tras fechar una -o tras un refresco de fondo-
        y un `x` inmediato cerraba la equivocada. Si la tarea ya no está (recién
        cerrada), el cursor cae en la fila más cercana que siga siendo válida.
        """
        destino = fila
        for indice, tarea in enumerate(visibles):
            if tarea.item_id == item_id:
                destino = indice
                break
        tabla.cursor_coordinate = Coordinate(max(0, min(destino, len(visibles) - 1)), 0)

    def _pintar_vacio(self) -> None:
        # Todo lo de acá se LEE (es lo único en pantalla), así que va en color 7 y no
        # en dim (ver SECUNDARIO).
        widget = self.query_one("#vacio", Static)
        if self.cargando:
            widget.update(Text("loading tasks…", style=SECUNDARIO))
        elif self.ultimo_error:
            widget.update(
                Text.assemble(
                    ("couldn't read the Project\n", "bold red"),
                    (f"{acortar(self.ultimo_error, 120)}\n\n", SECUNDARIO),
                    ("press r or click ⟳ to retry", SECUNDARIO),
                )
            )
        elif self.modo_repo and self.repo_actual:
            widget.update(
                Text.assemble(
                    (f"✓  nothing pending in {acortar(self.repo_actual, 40)}\n\n", "bold green"),
                    ('press t or click "all" to see the rest', SECUNDARIO),
                )
            )
        else:
            widget.update(
                Text.assemble(
                    ("✓  no pending tasks\n\n", "bold green"),
                    ('press n or click "+ new" to add one', SECUNDARIO),
                )
            )

    def _pintar_cabecera(self) -> None:
        if not self._pintable:  # misma carrera que en `_pintar_tabla`
            return
        visibles = self.visibles
        cuantas = len(visibles)
        hoy = date.today()
        vencidas = sum(1 for t in visibles if t.vence is not None and t.vence < hoy)

        # Segmentos (texto, estilo) del contador: la parte "overdue" usa el mismo rojo
        # semántico que las filas vencidas, para que el header se lea de un vistazo.
        # "loading…" solo mientras no haya NADA que mostrar: si la lista viene del
        # caché se muestra su conteo real, y el `⟳ Xm ago` de al lado dice de cuándo es.
        if self.cargando and not self.tareas:
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
        # El timestamp se consulta de reojo pero se lee: color 7, no dim.
        self.query_one("#cab-refrescar", BotonCabecera).update(
            Text(f"⟳ {self._hace_cuanto()}", style=SECUNDARIO)
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
        fila = self.query_one("#tabla", TablaTareas).cursor_row
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
        self.query_one("#tabla", TablaTareas).action_cursor_down()

    def action_arriba(self) -> None:
        self.query_one("#tabla", TablaTareas).action_cursor_up()

    def action_inicio(self) -> None:
        if self.visibles:
            self.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(0, 0)

    def action_fin(self) -> None:
        if self.visibles:
            ultima = len(self.visibles) - 1
            self.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(ultima, 0)

    def action_refrescar(self) -> None:
        # El refresco a mano es la vía de escape si una tarea se reabrió en GitHub
        # después de cerrarla acá: deja de ocultarse y vuelve a la lista.
        self.cerradas.clear()
        self._recordar_cerradas()
        self.refrescar()

    def action_toggle_repo(self) -> None:
        if self.repo_actual is None:
            return
        self.modo_repo = not self.modo_repo
        self._pintar_tabla()

    def action_salir(self) -> None:
        self.app.exit()

    async def _mostrar_modal(self, pantalla: ModalScreen) -> object:
        """Abre `pantalla`; un segundo disparo mientras ya hay un modal en pantalla no
        hace nada (mismo resultado que si el usuario lo hubiera cancelado).

        No sirve poner `exclusive=True` en el `@work` de estas acciones: cancelaría el
        worker que sigue esperando el `push_screen_wait` del PRIMER modal y lo dejaría
        huérfano en el `screen_stack` en vez de evitar el segundo. El guard va acá,
        ANTES de empujar la pantalla, así que la segunda `pantalla` construida por el
        llamador ni llega a montarse.
        """
        if self._modal_abierto:
            return None
        self._modal_abierto = True
        try:
            return await self.app.push_screen_wait(pantalla)
        finally:
            self._modal_abierto = False

    @work
    async def action_ver(self) -> None:
        tarea = self.seleccionada
        if tarea is None:
            return
        siguiente = await self._mostrar_modal(DetalleScreen(tarea))
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
        repo_prefijado = self.repo_actual if self.modo_repo else None
        datos = await self._mostrar_modal(NuevaScreen(self.obtener_repos, repo_prefijado))
        if not datos:
            return
        try:
            creada = await self.backend.crear(
                datos["repo"],
                datos["titulo"],
                datos["fecha"] or None,
                componer_cuerpo(datos["notas"], datos["repeat"]),
            )
        except ErrorParcial as err:
            # El issue ya existe: decir "couldn't create the task" invita a reintentar y
            # a terminar con dos. Se refresca para que se vea lo que sí quedó.
            self.notify(str(err), severity="warning", timeout=10)
            self.refrescar()
            return
        except (ErrorGh, IndexError, OSError) as err:
            self.notify(f"couldn't create the task: {err}", severity="error", timeout=6)
            return
        if creada is not None:
            self._nacer(creada)
            self._aplicar([*self.tareas, creada])
        self.notify("task created", timeout=3)
        self.refrescar()

    # ------------------------------------------------------------------ escrituras
    def _ocupada(self, tarea: Tarea) -> bool:
        """True si ya hay una escritura en vuelo sobre esa tarea (y se lo dice al usuario)."""
        etiqueta = self.ocupadas.get(tarea.item_id)
        if etiqueta is None:
            return False
        self.notify(f"already {etiqueta.rstrip('…')} this task", severity="warning", timeout=3)
        return True

    def _ocupar(self, tarea: Tarea, etiqueta: str) -> None:
        self.ocupadas[tarea.item_id] = etiqueta
        self._pintar_tabla()  # la fila muestra la operación en curso donde iba la fecha

    def _liberar(self, tarea: Tarea) -> None:
        self.ocupadas.pop(tarea.item_id, None)
        self._pintar_tabla()

    def _aplicar(self, tareas: list[Tarea]) -> None:
        """Deja la lista como quedó tras una escritura que `gh` YA confirmó.

        Antes cada acción terminaba llamando a `refrescar()` y nada más: la fila
        seguía mostrando el dato viejo (o la tarea cerrada seguía ahí) durante el
        `gh project item-list` completo, ~1 s. El refresco se sigue disparando
        igual -GitHub manda-, pero ya no se espera para mostrar lo obvio.
        """
        self.tareas = ordenar(tareas)
        self._pintar_tabla()

    async def _fechar(self, tarea: Tarea) -> None:
        if self._ocupada(tarea):
            return
        nueva = await self._mostrar_modal(FechaScreen(tarea.titulo, tarea.vence))
        if nueva is None:
            return
        self._ocupar(tarea, "saving…")
        try:
            await self.backend.fechar(tarea.item_id, nueva or None)
        except (ErrorGh, OSError) as err:
            self.notify(f"couldn't update the date: {err}", severity="error", timeout=6)
            return
        finally:
            self._liberar(tarea)
        self._aplicar(
            [
                replace(t, vence=parsear_fecha(nueva)) if t.item_id == tarea.item_id else t
                for t in self.tareas
            ]
        )
        self.notify("due date updated" if nueva else "due date cleared", timeout=3)
        self.refrescar()

    async def _cerrar(self, tarea: Tarea) -> None:
        if self._ocupada(tarea):
            return
        pregunta = f'close "{acortar(tarea.titulo, 60)}"?'
        if not await self._mostrar_modal(ConfirmaScreen(pregunta)):
            return
        self._ocupar(tarea, "closing…")
        try:
            await self._cerrar_ahora(tarea)
        finally:
            self._liberar(tarea)
        self.refrescar()

    async def _cerrar_ahora(self, tarea: Tarea) -> None:
        try:
            await self.backend.cerrar(tarea)
        except (ErrorGh, OSError) as err:
            self.notify(f"couldn't close the task: {err}", severity="error", timeout=6)
            return
        self.cerradas.add(tarea.item_id)
        self.nacientes.pop(tarea.item_id, None)  # cerrar gana sobre sostenerla
        self._recordar_cerradas()
        self._aplicar([t for t in self.tareas if t.item_id != tarea.item_id])
        self.notify(f"closed {tarea.cliente}", timeout=3)
        if tarea.repeat:
            await self._repetir(tarea)

    async def _repetir(self, tarea: Tarea) -> None:
        """Siguiente ocurrencia de una repetitiva recién cerrada.

        El cierre ya ocurrió: si esto falla hay que decirlo fuerte, o la serie se corta
        en silencio y el usuario recién se entera cuando la tarea no vuelve.
        """
        if tarea.repeat and tarea.vence is not None:
            # Segunda red de seguridad contra la ocurrencia duplicada: si la que
            # tocaba crear ya está en la lista, no se crea otra. Cubre cerrar dos
            # veces la misma tarea repetitiva -por un fantasma del caché, por haberla
            # reabierto en GitHub o por dos instancias de la app abiertas-, que dejaba
            # dos issues con la MISMA fecha y sin forma de saber cuál sobra.
            proxima = proxima_fecha(tarea.vence, tarea.repeat, date.today())
            if any(
                t.item_id != tarea.item_id
                and t.repo == tarea.repo
                and t.titulo == tarea.titulo
                and t.vence == proxima
                for t in self.tareas
            ):
                self.notify(
                    f"↻ next occurrence already exists ({proxima.isoformat()})", timeout=4
                )
                return
        if tarea.vence is None:
            self.notify(
                "↻ no due date on this task: next occurrence not created",
                severity="warning",
                timeout=6,
            )
            return
        try:
            siguiente = await self.backend.repetir(tarea, date.today())
        except ErrorParcial as err:
            # La ocurrencia ya nació, solo que incompleta: decir que no se creó llevaría
            # al usuario a crearla de nuevo y a duplicar la serie.
            self.notify(f"closed · ↻ {err}", severity="warning", timeout=10)
            return
        except (ErrorGh, IndexError, OSError, ValueError) as err:
            self.notify(
                f"closed, but couldn't create the next occurrence: {err}",
                severity="error",
                timeout=8,
            )
            return
        self._nacer(siguiente)
        self._aplicar([*self.tareas, siguiente])
        self.notify(f"↻ next: {siguiente.vence.isoformat()}", timeout=4)


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

    /* Modales: todo relativo al viewport, nada con medidas fijas. El alto lo pone el
       contenido (height: auto), así un detalle de una línea no abre un modal enorme. */
    DialogoModal { align: center middle; }
    .dlg {
        background: $surface; border: round $accent; padding: 0 1;
        width: 90%; max-width: 100; height: auto; max-height: 100%;
    }
    /* Respiración adaptativa: la clase "holgado" la pone el modal cuando la pantalla
       tiene UMBRAL_HOLGADO filas o más. Los separadores existen siempre en el árbol y
       solo aparecen ahí, así el layout compacto queda idéntico al de antes. */
    .dlg.holgado { padding: 1 2; }
    .respiro { display: none; height: 1; width: 100%; }
    .holgado .respiro { display: block; }

    .dlg-titulo { height: 1; color: $accent; text-style: bold; }
    .fila-chips { height: 1; width: 100%; }
    .fila-fecha { height: 1; width: 100%; }
    .fila-botones { height: 1; width: 100%; }
    /* El error ocupa la fila del hint en vez de sumar una: en 80x15 no sobra ninguna. */
    .error-linea { height: 1; width: 100%; color: $error; display: none; }
    /* Los hints son accionables: se leen. Van en ansi_white (color 7), que mide 7,38:1
       en claro y 10,72:1 en oscuro, en vez de "dim", que en esta paleta no pasa de
       4,0:1. El texto normal (10,24:1 / 12,30:1) sigue por encima, así que la
       jerarquía se conserva. */
    .hint { height: 1; width: 100%; color: ansi_white; }

    /* Botones de una fila: clickeables sin engordar la UI.
       El color va literal (ansi_yellow/ansi_red/ansi_default) y no por variable: las
       variables propias del theme todavía no existen cuando se parsea App.CSS. Sin
       relleno gris: texto en acento sobre el fondo default, delimitado por los
       corchetes del propio label (el borde de Button no cabe en height:1). En foco/
       hover, "reverse" invierte fg/bg del par ya elegido -mismo truco que fzf/
       lazygit-, así el contraste queda garantizado por construcción en cualquier
       paleta ANSI decente. */
    .chip {
        height: 1; min-width: 0; width: auto; border: none;
        padding: 0 1; margin: 0 1 0 0; background: ansi_default; color: ansi_yellow;
        text-style: none;
    }
    .chip:hover, .chip:focus { text-style: bold reverse; }
    /* Los 5 quick-picks miden 67 columnas justas y en 80x15 el diálogo da 68: con el
       margen derecho de .chip (5 columnas más) el último quedaba cortado por el borde.
       El padding propio de cada chip ya los separa. */
    .fila-chips .chip { margin: 0; }
    .primario { text-style: bold; }
    .peligro { color: ansi_red; }
    /* Misma razón que .hint: [cancel] y [back] son botones, no decoración. */
    .secundario { color: ansi_white; }

    #det-titulo { height: auto; max-height: 2; text-style: bold; }
    #det-meta { height: 1; color: $accent; }
    /* El color lo pone cada segmento del Text (verde/rojo/color 7 según el PR), así
       que acá no se fija ninguno: heredar el del terminal sería pisarlos. */
    #det-actividad { height: 1; }
    /* auto + max-height (que fija el modal según la pantalla) para que el cuerpo
       abrace su contenido en vez de estirar el diálogo hasta el borde.
       La regla que separa la meta del cuerpo va en ansi_white y no en $panel: $panel
       es ansi_black, EL MISMO color que el fondo del diálogo ($surface), así que la
       línea existía en el árbol pero no se veía en ninguna de las dos paletas. */
    #det-cuerpo {
        height: auto; min-height: 1; width: 100%; scrollbar-size-vertical: 1;
        border-top: solid ansi_white;
    }

    /* auto (no 1fr) para que la lista abrace a sus opciones y no deje hueco muerto;
       el techo sube cuando el modal respira. overflow-x oculto para que un repo largo
       no gaste una fila en una barra horizontal. */
    #nueva-repos {
        height: auto; max-height: 4; border: none; background: $background;
        overflow-x: hidden;
    }
    .holgado #nueva-repos { max-height: 6; }
    /* border-left literal (marcador de "esto es un input"): a diferencia de
       border-bottom, no consume una fila extra con height:1 (verificado).
       En reposo va en ansi_white y no en ansi_default: con el color del fondo el
       marcador era invisible, así que un Input sin foco no se distinguía de una
       línea de texto -de ahí que el campo de notas pasara desapercibido-. Con foco
       salta a ansi_yellow, así que se sigue viendo cuál está activo. */
    #nueva-filtro, #nueva-titulo, #nueva-notas, #nueva-fecha, #fecha-input {
        height: 1; border: none; border-left: solid ansi_white; padding: 0 1;
        background: $background; width: 1fr;
    }
    #nueva-filtro:focus, #nueva-titulo:focus, #nueva-notas:focus,
    #nueva-fecha:focus, #fecha-input:focus {
        border-left: solid ansi_yellow;
    }
    #nueva-fecha, #fecha-input { max-width: 24; }

    /* Los placeholders dicen QUÉ va en cada campo: se leen. El theme ansi de Textual
       los pinta con `text-style: dim` sobre ansi_default (ver Input.DEFAULT_CSS, rama
       `&:ansi`), que es el mismo problema de contraste del resto: en claro quedaban en
       2,7:1. Color 7 y sin dim, igual que los hints. */
    Input > .input--placeholder, Input > .input--suggestion {
        color: ansi_white;
        text-style: none;
    }
    """

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend

    def get_default_screen(self) -> Screen:
        return ListaScreen(self.backend)

    def on_mount(self) -> None:
        self.register_theme(THEME_TERMINAL)
        self.theme = "terminal"
