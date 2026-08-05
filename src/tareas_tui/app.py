"""Interfaz textual: lista densa arriba, todo lo demás en modales.

Tres reglas mandan sobre el diseño:

* **Cabe en un pane chico.** 80x15 es el caso de referencia; nada usa alto fijo y los
  anchos se recalculan en cada `Resize`, truncando con puntos suspensivos.
* **Respira cuando hay lugar.** Desde `UMBRAL_HOLGADO` filas de pantalla los modales
  separan sus grupos con una línea en blanco y ganan padding; desde `UMBRAL_PREVIEW`
  columnas la lista abre un panel lateral con el detalle de la fila seleccionada. Bajo
  esos umbrales todo vuelve al layout compacto, sin una fila ni una columna de más.
* **Se opera con mouse.** Cada acción tiene un blanco clickeable: filas, botones de la
  cabecera, teclas del footer y botones dentro de los modales. El teclado es atajo.
"""

from __future__ import annotations

import asyncio
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
    EJEMPLOS_FECHA,
    LIMITE_ITEMS,
    REPETICIONES,
    SECUNDARIO,
    Backend,
    Comentario,
    ErrorGh,
    ErrorParcial,
    Tarea,
    acortar,
    chip_pr,
    color_repo,
    componer_cuerpo,
    etiqueta_vencimiento,
    fecha_larga,
    hace_cuanto,
    interpretar_fecha,
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

# ------------------------------------------------------------------------------------
# Panel de preview: el reparto del ancho, medido y no estimado.
#
# La tabla gasta 28 columnas fijas antes de la primera letra de un título: 9 del
# vencimiento, 1 de la marca ↻, 8 de padding (2 por cada una de las 4 columnas) y 10
# del ancho mínimo de `repo#N`. Medido con la demo, el título recibe exactamente
# `ancho_del_pane - 28`:
#
#     ===== ======== =========
#     pane   título   panel
#     ===== ======== =========
#      80        52        —      (el pane de referencia)
#     120        92        —
#     120        46       46      (con el panel abierto)
#     160        86       46
#     ===== ======== =========
#
# ANCHO_PREVIEW es fijo y no elástico porque lo que se lee ahí es prosa: un párrafo
# markdown por debajo de ~40 columnas cae en dos o tres palabras por línea, y por
# encima de ~50 el ojo pierde el renglón. 46 deja 43 de texto (1 del borde que lo
# separa de la tabla y 2 de padding), que es justo esa franja.
#
# UMBRAL_PREVIEW es el ancho desde el que ese reparto todavía deja una tabla legible:
# a 120 el título conserva 46 columnas -contra las 52 del pane de referencia- y lo que
# se recorta en la fila el panel lo devuelve entero, envuelto y con el cuerpo debajo.
# Más abajo el panel costaría más de lo que muestra, así que directamente no aparece
# (bajo el umbral el layout es idéntico al de siempre, píxel por píxel).
UMBRAL_PREVIEW = 120
ANCHO_PREVIEW = 46
# Cursor quieto antes de pedirle los comentarios a `gh`. Bajar la lista con `j` puesto
# genera una selección por tecla, y sin esta espera cada una era una llamada de red;
# 0,4 s es más de lo que dura el autorrepeat entre teclas y menos de lo que se tarda en
# decidir que una tarea interesa. El worker es además `exclusive`: si igual se encolan
# dos, solo sobrevive la última.
ESPERA_COMENTARIOS = 0.4

ANCHO_ETIQUETA_REPEAT = max(len(r) for r in REPETICIONES)
# Fecha que no se entiende: el mismo aviso en los dos campos que la aceptan, y de paso
# vuelve a enseñar los formatos (el placeholder ya no está a la vista cuando hay texto).
ERROR_FECHA = f"invalid date, try {EJEMPLOS_FECHA}"

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
# Las tres funciones que siguen componen el TEXTO de una tarea y no su árbol de
# widgets: son lo que comparten el modal de detalle y el panel de preview, que muestran
# lo mismo en dos marcos distintos. La alternativa -copiar `_meta`/`_actividad` al
# panel- garantizaba que un arreglo se hiciera en un solo lado y el otro quedara viejo.
def meta_tarea(tarea: Tarea, hoy: date) -> Text:
    """Cliente · vencimiento · repetición, con el vencimiento en su color semántico.

    Misma jerarquía que la columna de la lista (vencida en rojo, hoy en acento, lejana
    en color 7): mirar el detalle no debería perder el dato que más se mira. Lo que no
    lleva estilo hereda el color que el CSS le ponga al widget que la muestre.
    """
    _, estilo = etiqueta_vencimiento(tarea.vence, hoy)
    partes = [Text(tarea.cliente), Text(fecha_larga(tarea.vence, hoy), estilo)]
    if tarea.repeat:
        partes.append(Text(f"↻ repeats {tarea.repeat}"))
    return Text(" · ").join(partes)


def actividad_tarea(tarea: Tarea) -> Text | None:
    """PR vinculado y comentarios; None si la tarea no tiene ni lo uno ni lo otro.

    Es la versión en palabras del chip de la lista: acá sí hay lugar para separar
    «merged» de «CI verde» y «closed» de «CI roja», que en un glifo se confundirían.
    El PR se lleva el color del chip -verde, rojo o color 7- para que una CI rota se
    vea al abrir el detalle sin tener que leer la frase.

    Devolver None y no un Text vacío es lo que deja que quien la muestra no gaste una
    fila: en un pane de 15, una línea en blanco es una línea menos de descripción.
    """
    partes: list[Text] = []
    if tarea.pr is not None:
        _, estilo = chip_pr(tarea.pr)
        partes.append(Text(resumen_pr(tarea.pr), estilo))
    if tarea.comentarios:
        plural = "" if tarea.comentarios == 1 else "s"
        partes.append(Text(f"{tarea.comentarios} comment{plural}", SECUNDARIO))
    return Text(" · ").join(partes) if partes else None


def cabecera_comentario(comentario: Comentario, ahora: datetime) -> Text:
    """«autor · hace cuánto», en secundario: quién habló y si es de recién."""
    partes = [comentario.autor]
    if comentario.creado is not None:
        partes.append(hace_cuanto(comentario.creado, ahora))
    return Text(" · ".join(partes), SECUNDARIO)


class BloqueComentario(Vertical):
    """Un comentario: su cabecera y su cuerpo renderizado como markdown.

    La regla de arriba (`border-top`) es la misma que separa la meta del cuerpo en el
    detalle, así que la conversación se lee como una continuación del issue y no como
    otra pantalla.
    """

    def __init__(self, comentario: Comentario, ahora: datetime) -> None:
        super().__init__(classes="comentario")
        self.comentario = comentario
        self.ahora = ahora

    def compose(self) -> ComposeResult:
        from textual.widgets import Markdown  # diferido, ver `DetalleScreen.compose`

        yield Static(cabecera_comentario(self.comentario, self.ahora), classes="com-cabecera")
        yield Markdown(self.comentario.cuerpo or "_(empty comment)_")


class Comentarios(Vertical):
    """Cola de comentarios al pie de un cuerpo scrolleable.

    La misma pieza en el modal de detalle y en el panel de preview: un comentario se
    lee igual en los dos lados y hay un solo lugar donde arreglarlo.

    Todo lo que remonta bloques pasa por `mostrar`, que borra los anteriores y monta
    los nuevos en un solo `await`. Con dos caminos -uno que borra y otro que monta- un
    cursor que se mueve rápido llegaba a intercalarlos y dejaba comentarios de la tarea
    anterior debajo de los de la nueva.
    """

    CARGANDO = "loading comments…"
    ESPERANDO = "…"  # el panel mientras corre el debounce: ni una palabra de ruido
    FALLO = "couldn't load comments"

    def compose(self) -> ComposeResult:
        yield Static("", classes="com-estado")

    def anunciar(self, texto: str) -> None:
        """Deja una sola línea en secundario (cargando, esperando o el fallo).

        No toca los bloques ya montados: quien cambia de tarea llama antes a
        `mostrar([])`.
        """
        for estado in self.query(".com-estado"):
            estado.update(Text(texto, SECUNDARIO))
            estado.display = bool(texto)

    async def mostrar(self, comentarios: list[Comentario]) -> None:
        """Reemplaza lo que haya por estos comentarios (la lista vacía deja el pie limpio)."""
        await self.query(BloqueComentario).remove()
        self.anunciar("")
        if comentarios:
            ahora = datetime.now()
            await self.mount_all([BloqueComentario(c, ahora) for c in comentarios])


class TablaTareas(DataTable):
    """DataTable cuya fila bajo el cursor se lee entera, sin `dim`.

    `cursor_foreground_priority="css"` (el default) solo pisa el **color** del
    renderable: Textual lo aplica como `Style.from_color(color=...)` en el `post_style`
    de la celda, y ahí no hay forma de tocar los atributos. Una celda marcada `dim`
    -la columna de repo, o un vencimiento lejano- se seguía difuminando contra el fondo
    del cursor (el filtro ANSI resuelve `dim` mezclando texto y fondo), y sobre el
    ámbar quedaba ilegible. Sumamos `dim=False` a ese mismo `post_style`, que es el
    único punto del pipeline donde se puede cancelar un atributo del renderable.

    El #39 probó `cursor_foreground_priority="renderable"` para que la fila bajo el
    cursor conservara el color semántico de cada celda (vencido en rojo, repo en su
    tono) en vez de aplanarse al color del cursor. Se revirtió (2026-08-05, con
    captura de la terminal real del usuario): en su paleta clara, "iktus-erp" en
    ansi_blue y "#422" en color 7 quedaban casi ilegibles sobre la barra de selección
    oliva -el color por repo depende de cómo cada terminal resuelve los 16 ANSI, y no
    hay forma de garantizar contraste terminal a terminal-. Con "css" el fg de toda la
    fila pasa a ser el del cursor, que sí tiene contraste garantizado por construcción.
    No reintroducir "renderable" sin resolver antes ese problema de contraste.

    El `lru_cache` no es optimización: DataTable llama a `cache_clear()` sobre este
    método al invalidar sus cachés, así que sin el decorador la llamada revienta.
    """

    class Redimensionada(Message):
        """La tabla cambió de tamaño: sus columnas se derivan del ancho que le tocó."""

    def on_resize(self, event: events.Resize) -> None:
        # El `Resize` de la PANTALLA no alcanza: cuando el panel de preview aparece o
        # se va, el pane mide lo mismo y es la tabla la que cambia de ancho. Escuchar
        # el suyo es lo único que garantiza que las columnas se recalculen con el
        # reparto ya hecho y no con el de un frame antes (`DataTable` atiende el suyo
        # en `_on_resize`, así que este handler público no le pisa nada).
        self.post_message(self.Redimensionada())

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

    Los comentarios NO se esperan para abrir: el modal aparece al instante con lo que
    ya está en memoria y un worker los agrega al pie del cuerpo cuando llegan. Abrir
    una tarea no puede costar una ida y vuelta a GitHub.
    """

    BINDINGS = [
        Binding("j", "abajo", "", show=False),
        Binding("k", "arriba", "", show=False),
        Binding("x", "cerrar_tarea", "", show=False),
        Binding("d", "cambiar_fecha", "", show=False),
    ]

    def __init__(
        self,
        tarea: Tarea,
        traer_comentarios: Callable[[Tarea], Awaitable[list[Comentario]]] | None = None,
    ) -> None:
        """`traer_comentarios` llega por corrutina y no por valor, igual que los repos de
        `NuevaScreen`: la trae quien tiene el backend y el modal no se entera de nada.
        Sin ella (tests que solo miran el texto) el detalle es exactamente el de antes.
        """
        super().__init__()
        self.tarea = tarea
        self._traer_comentarios = traer_comentarios

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
                # Sin conversación no se monta nada: una tarea sin comentarios no paga
                # ni la línea de "loading" ni la regla que la separaría del cuerpo.
                if self._va_a_traer_comentarios:
                    yield Comentarios(id="det-comentarios")
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

    @property
    def _va_a_traer_comentarios(self) -> bool:
        """Hay conversación que leer Y alguien a quien pedírsela."""
        return bool(self.tarea.comentarios) and self._traer_comentarios is not None

    def _meta(self) -> Text:
        """La meta de esta tarea (ver `meta_tarea`, compartida con el panel de preview)."""
        return meta_tarea(self.tarea, date.today())

    def _actividad(self) -> Text | None:
        """La actividad de esta tarea (ver `actividad_tarea`, compartida con el panel)."""
        return actividad_tarea(self.tarea)

    def al_montar(self) -> None:
        self.query_one("#det-cuerpo").focus()
        if self._va_a_traer_comentarios:
            self.query_one("#det-comentarios", Comentarios).anunciar(Comentarios.CARGANDO)
            self._cargar_comentarios()

    @work
    async def _cargar_comentarios(self) -> None:
        """Trae los comentarios y los deja al pie del cuerpo, ya con el modal abierto.

        El guard de la vuelta no es paranoia: cerrar el modal cancela el worker, pero
        la cancelación llega en el siguiente punto de espera, así que la línea que
        sigue a este `await` puede correr con el diálogo ya desmontado. Sin preguntar
        por el widget -y no por `is_mounted`, igual que `ListaScreen._pintable`-,
        pintar ahí revienta con NoMatches.
        """
        traer = self._traer_comentarios
        if traer is None:  # `al_montar` ya lo comprueba; acá por si alguien más llama
            return
        try:
            comentarios = await traer(self.tarea)
        except Exception:  # noqa: BLE001 - un fallo de `gh` no puede tumbar el detalle
            if self.query("#det-comentarios"):
                self.query_one("#det-comentarios", Comentarios).anunciar(Comentarios.FALLO)
            return
        if not self.query("#det-comentarios"):
            return
        await self.query_one("#det-comentarios", Comentarios).mostrar(comentarios)

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

    Lo que se escribe pasa por `interpretar_fecha`, así que además del formato canónico
    entran `fri`, `+10d` o `aug 20`; lo que sale del modal es SIEMPRE ISO, que es lo
    único que la API entiende.

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
        with Vertical(id="dlg-fecha", classes="dlg") as dlg:
            dlg.border_title = f"due date · {acortar(self.titulo, 120)}"
            yield Static("", classes="respiro")
            yield AtajosFecha(classes="fila-chips")
            yield Static("", classes="respiro")
            with Horizontal(classes="fila-botones"):
                yield InputFecha(
                    value=self.actual.isoformat() if self.actual else "",
                    placeholder=EJEMPLOS_FECHA,
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
        fecha = interpretar_fecha(texto, date.today())
        if fecha is None:
            self._avisar(ERROR_FECHA)
            return
        self.dismiss(fecha.isoformat())


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
        with Vertical(id="dlg-nueva", classes="dlg") as dlg:
            dlg.border_title = "new task"
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
                yield InputFecha(
                    placeholder=f"{EJEMPLOS_FECHA} (optional)", id="nueva-fecha"
                )
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
            # El alta viaja siempre en ISO: `fri` o `+10d` se resuelven acá, con el
            # modal todavía abierto, para que un error se corrija en el acto.
            vence = interpretar_fecha(fecha, date.today())
            if vence is None:
                self._avisar(ERROR_FECHA)
                self.query_one("#nueva-fecha", Input).focus()
                return
            fecha = vence.isoformat()
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
# Panel de preview
# ------------------------------------------------------------------------------------
class PanelPreview(Vertical):
    """Detalle de la tarea bajo el cursor, a la derecha de la lista en un pane ancho.

    Es SOLO LECTURA y no toma el foco nunca -ni por tab, de ahí el `can_focus=False`
    del scroll-: la tabla manda, y `enter` sigue abriendo el modal, que es donde viven
    los botones de acción. El panel solo evita tener que abrirlo para mirar.

    Muestra lo mismo que el modal y con las mismas funciones (`meta_tarea`,
    `actividad_tarea`, `Comentarios`); lo único propio es el reparto del ancho.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # El ancho se fija ANTES de montar, no en `on_mount`: `Vertical` viene con
        # `width: 1fr` de fábrica y con eso el primer layout repartía el pane mitad y
        # mitad. La tabla se recalculaba con ese ancho de mentira y sus columnas
        # quedaban a la mitad de lo que les tocaba. Vive en Python y no en el CSS
        # porque el CSS de la App es un string de clase: así la medida y su porqué
        # quedan juntos en ANCHO_PREVIEW.
        self.styles.width = ANCHO_PREVIEW
        #: (item_id, cuerpo) ya pintados. El markdown solo se rehace cuando cambian:
        #: bajar la lista con `j` puesto reparsearía un documento por tecla.
        self._pintado: tuple[str, str] = ("", "")

    def compose(self) -> ComposeResult:
        from textual.widgets import Markdown  # diferido, ver `DetalleScreen.compose`

        yield Static("", id="prev-titulo")
        yield Static("", id="prev-meta")
        yield Static("", id="prev-actividad")
        with VerticalScroll(id="prev-cuerpo", can_focus=False):
            yield Markdown(id="prev-md")
            yield Comentarios(id="prev-comentarios")

    def mostrar(self, tarea: Tarea) -> None:
        """Repinta el panel con `tarea`.

        La meta y la actividad se rehacen siempre -son dos Static y cambian al fechar
        o al llegar un refresco-; el cuerpo, solo cuando hay otro que renderizar.
        """
        self.query_one("#prev-titulo", Static).update(Text(tarea.titulo, style="bold"))
        self.query_one("#prev-meta", Static).update(meta_tarea(tarea, date.today()))
        actividad = actividad_tarea(tarea)
        linea = self.query_one("#prev-actividad", Static)
        linea.display = actividad is not None
        linea.update(actividad if actividad is not None else Text(""))

        pintado = (tarea.item_id, tarea.cuerpo)
        if pintado == self._pintado:
            return
        self._pintado = pintado
        from textual.widgets import Markdown

        self.query_one("#prev-md", Markdown).update(tarea.cuerpo or "_(no description)_")
        # Se vuelve arriba: quedarse al pie del cuerpo anterior mostraría la nueva
        # tarea empezada por la mitad.
        self.query_one("#prev-cuerpo", VerticalScroll).scroll_home(animate=False)


# ------------------------------------------------------------------------------------
# Pantalla principal
# ------------------------------------------------------------------------------------
class ListaScreen(Screen):
    #: El foco arranca SIEMPRE en la tabla. Hace falta decirlo desde que existe la fila
    #: del filtro: el auto-foco de Textual se queda con el primer widget «focusable» del
    #: DOM y esa prueba mira `visibility`, no `display`, así que la fila escondida se lo
    #: llevaba y ninguna tecla de la lista disparaba (todas se iban al Input).
    AUTO_FOCUS = "#tabla"

    BINDINGS = [
        # priority: DataTable también usa enter, y sin esto su binding (oculto) gana
        # el desempate y el footer se queda sin la pista más importante.
        Binding("enter", "ver", "view", priority=True),
        # El mismo enter mientras se escribe el filtro: los dos son prioritarios y
        # `check_action` decide cuál vale, así que nunca compiten (ver `check_action`).
        Binding("enter", "aplicar_filtro", "apply", priority=True),
        Binding("n", "nueva", "new"),
        Binding("d", "fecha", "date"),
        Binding("x", "cerrar", "close"),
        Binding("r", "refrescar", "refresh"),
        Binding("t", "toggle_repo", "repo"),
        Binding("slash", "filtrar", "filter"),
        Binding("escape", "limpiar_filtro", "clear"),
        Binding("q", "salir", "quit"),
        Binding("j", "abajo", "", show=False),
        Binding("k", "arriba", "", show=False),
        Binding("g", "inicio", "", show=False),
        Binding("G", "fin", "", show=False),
    ]

    #: Lo único que sigue disparando mientras el foco está en el filtro: ahí las letras
    #: son texto, no atajos, y el footer no puede prometer los que no funcionan (#37).
    ACCIONES_FILTRO = frozenset({"aplicar_filtro", "limpiar_filtro"})

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend
        self.tareas: list[Tarea] = []
        self.repos: list[str] = []
        self.repo_actual: str | None = None
        self.modo_repo = False
        # Texto del filtro incremental ("" es sin filtro). Vive acá y no en el Input
        # porque la fila del filtro va y viene, y el filtro tiene que sobrevivir a los
        # repintados -el refresco de fondo entre ellos-, no al revés.
        self.filtro = ""
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
        # Comentarios ya traídos, mientras dure la sesión. La clave lleva el CONTEO de
        # comentarios además del item_id: cuando el refresco periódico trae una
        # conversación más larga, la clave cambia sola y el próximo vistazo vuelve a
        # preguntar, sin caducidad inventada ni invalidación a mano. No baja a disco a
        # propósito -son cuerpos de texto que envejecen rápido y el caché de la lista ya
        # cubre lo que hace falta para pintar al arrancar-.
        self._comentarios: dict[tuple[str, int], list[Comentario]] = {}
        #: Clave de los comentarios que el panel de preview tiene pedidos, para no
        #: volver a pedirlos en cada repintado de la tabla. None con el panel escondido.
        self._preview_comentado: tuple[str, int] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="cabecera"):
            yield Static("", id="cab-titulo")
            yield BotonCabecera("", "toggle_repo", id="cab-toggle", classes="cab-btn")
            yield BotonCabecera("+ new", "nueva", id="cab-nueva", classes="cab-btn")
            yield BotonCabecera("⟳", "refrescar", id="cab-refrescar", classes="cab-btn")
        # Nace escondido (`display: none` en el CSS) y solo aparece mientras el filtro
        # está activo: en 80x15 la cabecera y el footer ya se llevan dos filas de
        # quince, y una tercera permanente sería una tarea menos a la vista.
        # `select_on_focus=False` porque `/` con un filtro puesto es para EDITARLO: con
        # el default de Textual (seleccionar todo al enfocar) la primera tecla borraba
        # lo que ya estaba escrito y había que volver a tipear la búsqueda entera.
        yield Input(
            placeholder="filter · title or repo", id="filtro", select_on_focus=False
        )
        tabla = TablaTareas(id="tabla")
        tabla.cursor_type = "row"
        tabla.show_header = False
        # La fila se reparte entre la tabla y el panel de preview. El panel NO se monta
        # acá: lo hace `_panel_preview` la primera vez que el pane da el ancho (ver
        # UMBRAL_PREVIEW), así un pane angosto no paga ni el widget ni los 39 ms de
        # markdown-it que arrastra su Markdown. Con el panel ausente la fila queda
        # exactamente como antes: un solo hijo al 100%.
        with Horizontal(id="cuerpo"):
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

    async def obtener_comentarios(self, tarea: Tarea) -> list[Comentario]:
        """Los últimos comentarios del issue, recordados por lo que dure la sesión.

        La misma corrutina alimenta al panel de preview y al modal de detalle, así que
        abrir el detalle de la tarea que el panel ya mostró no vuelve a llamar a `gh`
        (ver `self._comentarios` para la clave y por qué no se persiste).
        """
        clave = (tarea.item_id, tarea.comentarios)
        if clave not in self._comentarios:
            self._comentarios[clave] = await self.backend.comentarios(tarea)
        return self._comentarios[clave]

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
    def del_repo(self) -> list[Tarea]:
        """`tareas`, filtradas al repo actual cuando el modo repo está activo.

        El filtro es client-side sobre la misma lista del Project; `tareas` sigue
        siendo el dato completo que trae `backend.listar()`.
        """
        if self.modo_repo and self.repo_actual:
            return [t for t in self.tareas if t.repo == self.repo_actual]
        return self.tareas

    @property
    def visibles(self) -> list[Tarea]:
        """Lo que la tabla pinta: el modo repo Y el filtro incremental, combinados.

        Los dos se suman (AND): filtrar estando en «this repo» busca solo ahí. La
        búsqueda es substring simple sobre el título y el nombre del repo, sin importar
        la caja -es lo que la fila muestra, así que es lo que se busca- y sin nada de
        fuzzy: escribir «check» y que aparezca una tarea sin esa palabra sería peor que
        no encontrarla.
        """
        if not self.filtro:
            return self.del_repo
        aguja = self.filtro.casefold()
        return [
            t
            for t in self.del_repo
            if aguja in t.titulo.casefold() or aguja in t.repo.casefold()
        ]

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
            self._pintar_preview()  # sin fila seleccionada no hay nada que previsualizar
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
                self._celda_cliente(tarea, ancho_cliente),
                self._celda_titulo(tarea, ancho_titulo),
                key=tarea.item_id,
            )
        self._reubicar_cursor(tabla, recordado, fila_recordada, visibles)
        self._pintar_preview()

    @staticmethod
    def _celda_cliente(tarea: Tarea, ancho: int) -> Text:
        """repo#N con el nombre del repo en su tono estable y el número en color 7.

        Solo el nombre lleva color -distingue de un vistazo a qué repo pertenece la
        fila-; el `#N` sigue en SECUNDARIO, igual que antes (ver `color_repo`). Sin
        bold: ya hay tres colores con significado en la fila (vencimiento, ↻, PR) y
        uno más en negrita competiría con ellos.
        """
        corto = tarea.repo.split("/", 1)[-1]
        sufijo = f"#{tarea.numero}"
        if len(corto) + len(sufijo) > ancho:
            # No cabe entero: se trunca como un bloque (mismo comportamiento que
            # antes de colorear por repo) en vez de partir un color a la fuerza.
            return Text(acortar(tarea.cliente, ancho), style=SECUNDARIO)
        return Text.assemble((corto, color_repo(tarea.repo)), (sufijo, SECUNDARIO))

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

    # ------------------------------------------------------------------ preview
    def _panel_preview(self) -> PanelPreview | None:
        """El panel, montándolo la primera vez que hace falta; None si recién nació.

        No se monta en `compose` porque su `Markdown` arrastra markdown-it y pygments
        (39 ms del arranque, ver `DetalleScreen.compose`): en un pane angosto -el caso
        de referencia- el panel no existe y no se paga nada. Recién montado todavía no
        compuso sus hijos, así que devuelve None y quien llama vuelve tras el refresco.
        """
        paneles = self.query(PanelPreview)
        if not paneles:
            self.query_one("#cuerpo", Horizontal).mount(PanelPreview(id="preview"))
            self.call_after_refresh(self._pintar_preview)
            return None
        panel = paneles.first(PanelPreview)
        if not panel.query("#prev-titulo"):
            # Montado pero todavía sin componer (Textual monta a los hijos en su propio
            # ciclo): pintarlo ahora reventaría con NoMatches.
            self.call_after_refresh(self._pintar_preview)
            return None
        return panel

    def _pintar_preview(self) -> None:
        """Deja el panel mostrando la tarea bajo el cursor, o lo esconde.

        Se llama tras cada repintado de la tabla, en cada movimiento del cursor y en
        cada resize: el panel es un espejo de la selección, no un estado propio. Con el
        filtro puesto la selección es la del subconjunto filtrado, así que el panel
        habla de lo que se está viendo y no de la lista entera.
        """
        if not self._pintable:
            return
        tarea = self.seleccionada
        if tarea is None or self.size.width < UMBRAL_PREVIEW:
            self._esconder_preview()
            return
        panel = self._panel_preview()
        if panel is None:
            return  # se acaba de montar: `call_after_refresh` vuelve a pasar por acá
        aparecio = not panel.display
        panel.display = True
        panel.mostrar(tarea)
        # Los comentarios solo se vuelven a pedir cuando cambia la tarea o le crecen
        # (la clave lleva el conteo, ver `self._comentarios`). Sin este corte, cada
        # repintado de la tabla -uno por tecla mientras se escribe el filtro- volvía a
        # desmontar y montar los bloques que ya estaban a la vista.
        clave = (tarea.item_id, tarea.comentarios)
        if aparecio or clave != self._preview_comentado:
            self._preview_comentado = clave
            self._comentarios_al_preview(tarea)
        if aparecio:
            self._repartir_de_nuevo()

    def _esconder_preview(self) -> None:
        self._preview_comentado = None
        for panel in self.query(PanelPreview):
            if panel.display:
                panel.display = False
                self._repartir_de_nuevo()

    def _repartir_de_nuevo(self) -> None:
        """Recalcula las columnas de la tabla cuando el panel aparece o desaparece.

        Tiene que ser DESPUÉS del refresco: los anchos salen de
        `tabla.scrollable_content_region`, que hasta que Textual no rehace el layout
        sigue midiendo el pane sin repartir. No hay bucle: la segunda vuelta encuentra
        al panel ya en su sitio y no vuelve a pedir reparto.
        """
        self.call_after_refresh(self._pintar_tabla)

    @work(exclusive=True, group="comentarios")
    async def _comentarios_al_preview(self, tarea: Tarea) -> None:
        """Los comentarios de la tarea seleccionada, al pie del cuerpo del panel.

        `exclusive` más la espera de `ESPERA_COMENTARIOS` son el debounce: cada tecla
        cancela el worker anterior -y con él su espera-, así que recorrer la lista de
        punta a punta no dispara una llamada a `gh` por fila, solo la de donde el
        cursor se queda. Lo ya traído se pinta sin esperar nada.
        """
        pie = self.query("#prev-comentarios")
        if not pie:
            return
        comentarios = pie.first(Comentarios)
        guardados = self._comentarios.get((tarea.item_id, tarea.comentarios))
        if guardados is None:
            # Se limpia ANTES de esperar: los comentarios de la tarea anterior bajo el
            # cuerpo de la nueva se leerían como si fueran de esta.
            await comentarios.mostrar([])
            if not tarea.comentarios:
                return
            comentarios.anunciar(Comentarios.ESPERANDO)
            await asyncio.sleep(ESPERA_COMENTARIOS)
            try:
                guardados = await self.obtener_comentarios(tarea)
            except Exception:  # noqa: BLE001 - el panel lo dice en una línea y sigue
                comentarios.anunciar(Comentarios.FALLO)
                return
        # El cursor pudo moverse (o la pantalla irse) mientras `gh` contestaba: sin
        # este guard el panel mostraría la conversación de otra tarea.
        actual = self.seleccionada if self._pintable else None
        if actual is None or actual.item_id != tarea.item_id or not self.query("#prev-comentarios"):
            return
        await comentarios.mostrar(guardados)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # El panel sigue al cursor, se mueva con teclado o con un clic.
        self._pintar_preview()

    def on_tabla_tareas_redimensionada(self, event: TablaTareas.Redimensionada) -> None:
        event.stop()
        if self.visibles:
            self._pintar_tabla()

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
        elif self.filtro:
            # No es un estado feliz -hay tareas, solo que ninguna coincide-, así que no
            # lleva el ✓ verde de los otros dos: es el filtro el que hay que cambiar.
            widget.update(
                Text.assemble(
                    (f'no tasks match "{acortar(self.filtro, 40)}"\n\n', "bold yellow"),
                    ("press esc to clear the filter", SECUNDARIO),
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
            # Con el filtro puesto NO es que no haya pendientes: es que ninguna coincide,
            # y decir "nothing pending" mandaría a cerrar la app tranquilo.
            estado = [("no matches" if self.filtro else "nothing pending", "yellow")]
        elif vencidas:
            estado = [
                (f"{self._conteo(cuantas)} pending", "yellow"),
                (" · ", "dim"),
                # Token invertido: "reverse" pone el rojo de fondo y el texto en el
                # color por defecto de la terminal, así el contraste queda garantizado
                # por construcción en cualquier paleta (mismo truco que `.chip:hover`).
                # Los espacios son parte del token, no relleno: sin ellos el fondo
                # rojo pegaría el número y la palabra al resto de la cabecera.
                (f" {vencidas} overdue ", "bold red reverse"),
            ]
        else:
            estado = [(f"{self._conteo(cuantas)} pending", "yellow")]
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

    def _conteo(self, cuantas: int) -> str:
        """«3» normalmente; «3/12» con el filtro puesto.

        El total es el del modo en curso (todas o el repo actual), así que el segundo
        número dice exactamente cuánto está escondiendo el filtro y no otra cosa.
        """
        return f"{cuantas}/{len(self.del_repo)}" if self.filtro else str(cuantas)

    def _hace_cuanto(self) -> str:
        """Antigüedad del dato de la lista, con la misma escala que los comentarios.

        Antes contaba solo minutos y una sesión larga sin red terminaba anunciando
        «180m ago»; `hace_cuanto` sube a horas y días donde corresponde.
        """
        if self.ultimo_ok is None:
            return "—"
        return hace_cuanto(self.ultimo_ok, datetime.now())

    def on_resize(self, event: events.Resize) -> None:
        self._pintar_cabecera()
        # El panel aparece y desaparece en vivo al cambiar el ancho del pane; va antes
        # que la tabla porque es lo que decide cuánto ancho le queda (`_repartir_de_nuevo`
        # se encarga de que la tabla se recalcule con el reparto ya hecho).
        self.call_after_refresh(self._pintar_preview)
        if self.visibles:
            self.call_after_refresh(self._pintar_tabla)

    # ------------------------------------------------------------------ interacción
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Qué atajos valen AHORA. False esconde el binding del footer y le impide
        disparar (Textual reserva None para «visible pero apagado»).

        Es lo que sostiene las dos caras del filtro: mientras el Input tiene el foco las
        letras son texto y no atajos, así que el footer no anuncia ni uno solo de los
        que no funcionan; y el mismo `enter` sirve para abrir el detalle o para dejar el
        filtro puesto según dónde esté el foco, sin que los dos bindings compitan.
        """
        if self._filtro_enfocado:
            return action in self.ACCIONES_FILTRO
        if action == "aplicar_filtro":
            return False  # sin el foco en el filtro, enter es «ver el detalle»
        if action == "limpiar_filtro":
            return self._filtrando
        if action == "toggle_repo":
            return self.repo_actual is not None
        return True

    # ------------------------------------------------------------------ filtro
    @property
    def _filtrando(self) -> bool:
        """True mientras la fila del filtro está en pantalla (con o sin foco)."""
        entradas = self.query("#filtro")
        return bool(entradas) and entradas.first().display

    @property
    def _filtro_enfocado(self) -> bool:
        entradas = self.query("#filtro")
        return bool(entradas) and entradas.first().has_focus

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # El footer cambia con el foco (ver `check_action`), y el foco también se mueve
        # con el mouse -un clic en la tabla saca del filtro-, así que se recalcula acá
        # y no en cada acción.
        self.refresh_bindings()

    def action_filtrar(self) -> None:
        """`/`: abre la fila del filtro, o la vuelve a enfocar si ya estaba puesta."""
        entrada = self.query_one("#filtro", Input)
        entrada.display = True
        entrada.focus()
        # Reabrir con un filtro puesto es para editarlo, no para empezar de cero: el
        # cursor va al final del texto que ya está.
        entrada.cursor_position = len(entrada.value)
        self.refresh_bindings()

    def action_aplicar_filtro(self) -> None:
        """`enter`: el filtro queda puesto y el foco vuelve a la tabla, donde x/d/n
        son atajos otra vez. La fila sigue a la vista: es lo que dice por qué la lista
        está corta."""
        if not self.query_one("#filtro", Input).value.strip():
            self.action_limpiar_filtro()
            return
        self.query_one("#tabla", TablaTareas).focus()
        self.refresh_bindings()

    def action_limpiar_filtro(self) -> None:
        """`esc`: saca el filtro y devuelve su fila a la lista."""
        entrada = self.query_one("#filtro", Input)
        entrada.value = ""
        entrada.display = False
        self.filtro = ""
        self.query_one("#tabla", TablaTareas).focus()
        self._pintar_tabla()
        self.refresh_bindings()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filtro":
            return
        event.stop()
        # Filtra mientras se escribe: son ~7 tareas en memoria, no hay nada que debouncear.
        self.filtro = event.value.strip()
        self._pintar_tabla()

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
        siguiente = await self._mostrar_modal(
            DetalleScreen(tarea, self.obtener_comentarios)
        )
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

    /* La fila del medio: la tabla (o el mensaje de lista vacía) y, en un pane ancho,
       el panel de preview a su derecha. Con el panel ausente -que es el caso bajo
       UMBRAL_PREVIEW- el único hijo visible ocupa el 100% y el layout queda idéntico
       al de antes de que existiera este contenedor. */
    #cuerpo { height: 1fr; width: 100%; }
    #tabla { height: 1fr; width: 1fr; scrollbar-size-vertical: 1; }
    #vacio {
        display: none; height: 1fr; width: 1fr;
        content-align: center middle; text-align: center;
    }
    Footer { height: 1; }

    /* El ancho lo pone `PanelPreview.on_mount` (ver ANCHO_PREVIEW). El borde izquierdo
       es la única separación con la tabla: en ansi_white, el mismo tono neutro que la
       regla del detalle y el marco de los modales, nunca el acento -que está reservado
       a lo accionable, y el panel no lo es-. */
    #preview { height: 1fr; border-left: solid ansi_white; padding: 0 1; }
    #prev-titulo { height: auto; max-height: 3; text-style: bold; }
    #prev-meta { height: auto; color: $accent; }
    /* Igual que #det-actividad: el color lo trae cada segmento del Text. */
    #prev-actividad { height: auto; }
    #prev-cuerpo {
        height: 1fr; width: 100%; scrollbar-size-vertical: 1;
        border-top: solid ansi_white;
    }
    /* Markdown se pone `padding: 0 2` por defecto: en el modal eso indenta el cuerpo
       bajo la meta y se lee como jerarquía, pero acá son 4 de las 43 columnas del
       panel gastadas en sangría. A este ancho el texto vale más que el margen. */
    #preview Markdown { padding: 0; }
    /* Sin esto el contenedor de comentarios se lleva el 1fr que Vertical trae de
       fábrica y empuja el cuerpo fuera de la vista aun estando vacío. */
    Comentarios { height: auto; width: 100%; }
    /* Cada comentario se separa del anterior (y del cuerpo del issue) con la MISMA
       regla que parte la meta del cuerpo en el detalle: la conversación se lee como
       continuación del issue, no como otra pantalla. */
    .comentario { height: auto; width: 100%; border-top: solid ansi_white; }
    .com-cabecera { height: auto; width: 100%; }
    /* Nace escondido: solo aparece mientras hay algo que decir ("loading comments…",
       "…" o el fallo), así una tarea sin conversación no gasta ni una fila. */
    .com-estado { display: none; height: auto; width: 100%; }

    /* Modales: todo relativo al viewport, nada con medidas fijas. El alto lo pone el
       contenido (height: auto), así un detalle de una línea no abre un modal enorme. */
    DialogoModal { align: center middle; }
    /* El borde va en ansi_white (color 7, el mismo tono que .hint y .secundario) y no
       en $accent: con el acento reservado a [create] y a los quick-picks, el ojo va
       directo a lo clickeable en vez de perderse en el marco. El título del borde SÍ
       lleva acento -es la única etiqueta de "qué modal es este"-, así el amarillo no
       desaparece del todo, solo se concentra donde importa. */
    .dlg {
        background: $surface; border: round ansi_white; padding: 0 1;
        border-title-color: $accent; border-title-style: bold;
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
    #nueva-filtro, #nueva-titulo, #nueva-notas, #nueva-fecha, #fecha-input, #filtro {
        height: 1; border: none; border-left: solid ansi_white; padding: 0 1;
        background: $background; width: 1fr;
    }
    #nueva-filtro:focus, #nueva-titulo:focus, #nueva-notas:focus,
    #nueva-fecha:focus, #fecha-input:focus, #filtro:focus {
        border-left: solid ansi_yellow;
    }
    /* La fila del filtro de la lista nace escondida y solo existe mientras el filtro
       está activo (la muestra `action_filtrar`, la esconde `action_limpiar_filtro`):
       en 80x15 la cabecera y el footer ya se llevan dos filas de quince, así que la
       tercera se paga solo cuando se está usando. */
    #filtro { display: none; }
    /* Cada campo de fecha mide lo que su placeholder necesita (el Input se come 3
       columnas entre el borde izquierdo y el padding): "YYYY-MM-DD · fri · +10d" son
       23 caracteres y pide 26, y la variante con " (optional)" son 34 y pide 37. A 80
       columnas las dos filas siguen entrando en los 68 útiles del diálogo: los tres
       botones de la fecha se llevan 36 (30+36=66) y el chip de repetición 24
       (37+24=61). */
    #fecha-input { max-width: 30; }
    #nueva-fecha { max-width: 37; }

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
