"""Smoke test con el Pilot de textual, sobre `BackendDemo` (sin tocar GitHub).

Cubre el modo contextual por repo, los atajos de teclado, el contraste de la fila
bajo el cursor, la respiración adaptativa de los modales, el campo de notas y el
ciclo de vida de las tareas repetitivas (crear con ↻, cerrar y que nazca la
siguiente ocurrencia).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta

import gh_falso
import pytest
from gh_falso import GhFalso
from rich.style import Style
from textual.coordinate import Coordinate
from textual.widgets import Button, Input, OptionList

from tareas_tui import datos
from tareas_tui.app import (
    ANCHO_PREVIEW,
    ESPERA_COMENTARIOS,
    UMBRAL_PREVIEW,
    BloqueComentario,
    ConfirmaScreen,
    DetalleScreen,
    FechaScreen,
    ListaScreen,
    NuevaScreen,
    PanelPreview,
    TablaTareas,
    TareasApp,
)
from tareas_tui.datos import BackendDemo, ErrorGh, color_repo, proxima_fecha, vacio_demo

pytestmark = pytest.mark.asyncio

TAMANOS = [(80, 15), (110, 24), (160, 45)]


async def _esperar(pilot, condicion, intentos: int = 40) -> None:
    for _ in range(intentos):
        if condicion():
            return
        await pilot.pause(0.02)


async def _listo(pilot) -> ListaScreen:
    """Espera a que carguen tareas, repos y la detección de repo del cwd."""
    screen = pilot.app.screen
    await _esperar(pilot, lambda: not screen.cargando and screen.repos)
    await pilot.pause(0.1)  # margen para el worker de detectar_repo
    return screen


def _avisos(app) -> list[str]:
    return [notificacion.message for notificacion in app._notifications]


# ------------------------------------------------------------------ detección + filtro
async def test_modo_todas_sin_repo_detectado():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert screen.repo_actual is None
        assert screen.modo_repo is False
        assert len(screen.visibles) == len(screen.tareas) == 7
        assert screen.query_one("#cab-toggle").display is False
        assert screen.query_one("#cab-titulo").content.plain.startswith("Client Tasks")


async def test_modo_todas_usa_el_titulo_del_project_del_backend():
    # El header en modo todas muestra `backend.titulo_project` (el nombre real del
    # GitHub Project cuando el backend es real), no un string fijo.
    app = TareasApp(BackendDemo(project_title="Ops Board"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert screen.query_one("#cab-titulo").content.plain.startswith("Ops Board")


async def test_modo_repo_detectado_filtra_la_lista():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert screen.repo_actual == "vela/landing"
        assert screen.modo_repo is True
        assert len(screen.tareas) == 7
        assert len(screen.visibles) == 2
        assert all(t.repo == "vela/landing" for t in screen.visibles)
        assert screen.query_one("#cab-toggle").display is True
        assert screen.query_one("#cab-titulo").content.plain.startswith("vela/landing")


# ------------------------------------------------------------------ toggle
async def test_toggle_por_tecla():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert screen.modo_repo is True

        await pilot.press("t")
        await pilot.pause()
        assert screen.modo_repo is False
        assert len(screen.visibles) == 7

        await pilot.press("t")
        await pilot.pause()
        assert screen.modo_repo is True
        assert len(screen.visibles) == 2


async def test_toggle_por_click():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert screen.modo_repo is True

        await pilot.click("#cab-toggle")
        await pilot.pause()
        assert screen.modo_repo is False
        assert len(screen.visibles) == 7


async def test_toggle_ausente_fuera_de_repo():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        # sin repo detectado, la tecla t queda oculta+deshabilitada en el footer
        assert screen.check_action("toggle_repo", ()) is False
        screen.action_toggle_repo()
        assert screen.modo_repo is False


# ------------------------------------------------------------------ nueva tarea
async def test_nueva_en_modo_repo_sale_sin_picker():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        await _listo(pilot)

        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NuevaScreen)
        assert modal.repo_elegido == "vela/landing"
        assert modal.query_one("#nueva-filtro").display is False
        assert modal.query_one("#nueva-repos").display is False
        assert "vela/landing" in modal.query_one("#nueva-repo-fijo").content

        # clic en la etiqueta fija: revela el picker normal para elegir otro repo
        await pilot.click("#nueva-repo-fijo")
        await pilot.pause()
        assert modal.query_one("#nueva-filtro").display is True
        assert modal.query_one("#nueva-repos").display is True
        assert not modal.query("#nueva-repo-fijo")


async def test_nueva_en_modo_todas_mantiene_el_picker():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        await _listo(pilot)

        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NuevaScreen)
        assert modal.repo_prefijado is None
        assert not modal.query("#nueva-repo-fijo")
        assert modal.query_one("#nueva-filtro").display is True
        assert modal.query_one("#nueva-repos").display is True


# ------------------------------------------------------------------ estado vacío
async def test_vacio_en_modo_repo_muestra_hint_del_toggle():
    app = TareasApp(vacio_demo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#vacio").content.plain
        assert "nothing pending in vela/landing" in texto
        assert "all" in texto


async def test_vacio_en_modo_todas_sin_cambios():
    app = TareasApp(vacio_demo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#vacio").content.plain
        assert "no pending tasks" in texto


# ------------------------------------------------------------------ pane chico / responsive
@pytest.mark.parametrize("size", TAMANOS)
async def test_cabecera_no_desborda_con_repo_largo(size):
    # Deliberadamente más largo que cualquiera de los anchos probados, incluido 160.
    repo_largo = "organizacion-" + "x" * 90 + "/repositorio-" + "y" * 90
    app = TareasApp(BackendDemo(repo_actual=repo_largo))
    async with app.run_test(size=size) as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#cab-titulo").content.plain
        assert len(texto) <= size[0]
        assert "…" in texto  # tan largo que siempre debe truncarse


@pytest.mark.parametrize("size", TAMANOS)
async def test_modales_con_hint_caben_en_pane_chico(size):
    """Los 4 modales con su línea de hint quedan dentro del viewport, en 80x15
    incluido: nada se corta ni desborda."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=size) as pilot:
        await _listo(pilot)

        await pilot.press("d")
        await pilot.pause()
        hint = pilot.app.screen.query_one("#fecha-hint")
        assert hint.display
        assert hint.region.y + hint.region.height <= size[1]
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("n")
        await pilot.pause()
        hint = pilot.app.screen.query_one("#nueva-hint")
        assert hint.display
        assert hint.region.y + hint.region.height <= size[1]
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("x")
        await pilot.pause()
        hint = pilot.app.screen.query_one("#confirma-hint")
        assert hint.display
        assert hint.region.y + hint.region.height <= size[1]
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()
        hint = pilot.app.screen.query_one("#det-hint")
        assert hint.display
        assert hint.region.y + hint.region.height <= size[1]


@pytest.mark.parametrize("size", TAMANOS)
@pytest.mark.parametrize(
    "tecla,contenedor",
    [
        ("n", "#dlg-nueva"),
        ("d", "#dlg-fecha"),
        # Los 4 diálogos, no solo los dos con quick-picks: los botones llevan su atajo
        # en el label ("[x·close task]"), así que cualquiera puede pasarse de fila.
        ("enter", "#dlg-detalle"),
        ("x", "#dlg-confirma"),
    ],
)
async def test_ningun_chip_se_sale_de_su_fila(size, tecla, contenedor):
    """En 80x15 el último quick-pick se comía el borde del diálogo."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=size) as pilot:
        await _listo(pilot)
        await pilot.press(tecla)
        await pilot.pause()
        dialogo = pilot.app.screen.query_one(contenedor)
        for chip in dialogo.query(Button):
            assert chip.region.right <= dialogo.content_region.right
            assert chip.region.x >= dialogo.content_region.x


# ------------------------------------------------------------------ contraste del cursor
async def test_la_fila_bajo_el_cursor_se_lee_entera_sin_dim():
    """El bug que motivó `TablaTareas`: una celda en `dim` se difuminaba sobre el fondo
    ámbar del cursor hasta quedar ilegible. Bajo el cursor no debe sobrevivir ningún
    `dim`, y todo el texto de la fila comparte el color del cursor.

    Desde el cambio de contraste ya ninguna celda se pinta en `dim` -repo y vencimientos
    pasaron al color 7-, así que `TablaTareas` quedó de guarda por si alguna vuelve."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        assert tabla.has_focus

        await pilot.press("j")  # fila 1: acme/web#12, vencida, con marca ↻
        await pilot.pause()
        assert tabla.cursor_row == 1

        color_cursor = tabla.get_component_rich_style("datatable--cursor").color
        bajo_cursor = [s for s in tabla.render_line(1) if s.text.strip()]
        assert bajo_cursor
        assert all(not s.style.dim for s in bajo_cursor)
        assert {s.style.color for s in bajo_cursor} == {color_cursor}

        # control: fuera del cursor cada fila conserva SUS colores (la jerarquía), en vez
        # de quedar aplanada al color del cursor.
        otra_fila = [s for s in tabla.render_line(0) if s.text.strip()]
        assert otra_fila
        assert {s.style.color for s in otra_fila} != {color_cursor}


# ------------------------------------------------------------------ respiración adaptativa
@pytest.mark.parametrize(
    "size,holgado", [((80, 15), False), ((110, 24), True), ((160, 45), True)]
)
async def test_los_modales_respiran_solo_con_pantalla_alta(size, holgado):
    app = TareasApp(BackendDemo())
    async with app.run_test(size=size) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert modal.query_one("#dlg-nueva").has_class("holgado") is holgado
        separadores = modal.query(".respiro")
        assert separadores
        assert all(r.display is holgado for r in separadores)


async def _alto_detalle(size, cuerpo: str) -> int:
    app = TareasApp(BackendDemo())
    async with app.run_test(size=size) as pilot:
        screen = await _listo(pilot)
        screen.tareas = [replace(t, cuerpo=cuerpo) for t in screen.tareas]
        await pilot.press("enter")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, DetalleScreen)
        return modal.query_one("#dlg-detalle").size.height


async def test_el_detalle_se_ajusta_al_cuerpo_y_no_ocupa_la_pantalla():
    corto = await _alto_detalle((160, 45), "One line.")
    largo = await _alto_detalle((160, 45), "\n\n".join(f"Line {i}" for i in range(60)))
    assert corto <= 14  # un body de una línea no abre un modal gigante
    assert corto < largo <= 45


# ------------------------------------------------------------------ quick-picks numéricos
async def test_atajo_numerico_en_fecha_aplica_y_guarda_de_inmediato():
    # mesa/intranet#9 es la única tarea demo sin vencimiento -> el campo abre vacío,
    # asi que el digito dispara el quick-pick en vez de escribirse literal.
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("G")
        tarea = screen.seleccionada
        assert tarea.vence is None

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(pilot.app.screen, FechaScreen)

        await pilot.press("3")  # 3 = "+3 days"
        await pilot.pause()
        assert isinstance(pilot.app.screen, ListaScreen)
        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence == date.today() + timedelta(days=3)


async def test_click_en_chip_relabelado_sigue_funcionando():
    # Los chips ahora muestran "2·tomorrow" etc, pero el id (#qp-manana) y el click
    # no cambiaron: guarda contra que el nuevo label rompa el mouse-first existente.
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("G")
        tarea = screen.seleccionada

        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#qp-manana")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ListaScreen)
        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence == date.today() + timedelta(days=1)


async def test_atajo_numerico_en_fecha_no_pisa_edicion_con_valor_precargado():
    # La primera fila (orden por vencimiento) ya tiene fecha: el input abre con
    # texto (preseleccionado por Textual, select_on_focus), así que el dígito debe
    # escribirse como cualquier tecla normal -reemplazando la selección-, nunca
    # disparar el quick-pick.
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("g")
        tarea = screen.seleccionada
        assert tarea.vence is not None

        await pilot.press("d")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FechaScreen)
        campo = modal.query_one("#fecha-input", Input)
        assert campo.value  # precargado con la fecha actual de la tarea

        await pilot.press("1")
        await pilot.pause()
        assert pilot.app.screen is modal  # sigue abierto: no se disparó el quick-pick
        assert campo.value == "1"  # tecla normal, reemplaza la selección precargada


async def test_atajo_numerico_en_nueva_marca_fecha_sin_crear():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NuevaScreen)
        assert modal.query_one("#nueva-titulo", Input).has_focus

        await pilot.press("t", "e", "s", "t")
        await pilot.press("enter")  # título -> notas
        await pilot.pause()
        assert modal.query_one("#nueva-notas", Input).has_focus
        await pilot.press("enter")  # notas -> fecha
        await pilot.pause()
        campo_fecha = modal.query_one("#nueva-fecha", Input)
        assert campo_fecha.has_focus

        await pilot.press("2")  # 2 = "tomorrow"
        await pilot.pause()
        assert pilot.app.screen is modal  # marca la fecha, no crea (nada dismisseado)
        assert campo_fecha.value == (date.today() + timedelta(days=1)).isoformat()


async def test_atajo_numerico_no_intercepta_mientras_se_escribe_el_titulo():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        titulo = modal.query_one("#nueva-titulo", Input)
        await pilot.press("5")
        await pilot.pause()
        assert titulo.value == "5"
        assert pilot.app.screen is modal


# ------------------------------------------------------------------ notas y creación
async def _crear_desde_modal(
    pilot, screen, *, titulo, notas="", fecha="", repeticiones=0, tecla="ctrl+enter"
):
    """Abre «nueva» en modo repo, llena los campos y confirma con `tecla`."""
    await pilot.press("n")
    await pilot.pause()
    modal = pilot.app.screen
    assert isinstance(modal, NuevaScreen)
    modal.query_one("#nueva-titulo", Input).value = titulo
    modal.query_one("#nueva-notas", Input).value = notas
    modal.query_one("#nueva-fecha", Input).value = fecha
    for _ in range(repeticiones):
        await pilot.press("ctrl+r")
    await pilot.press(tecla)
    await _esperar(pilot, lambda: any(t.titulo == titulo for t in screen.tareas))
    return next(t for t in screen.tareas if t.titulo == titulo)


async def test_nueva_sin_notas_deja_el_cuerpo_vacio():
    """Adiós al placeholder heredado del fzf: sin notas el issue nace con body vacío."""
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        creada = await _crear_desde_modal(pilot, screen, titulo="No body here")
        assert creada.cuerpo == ""
        assert creada.repeat is None


async def test_las_notas_del_modal_son_el_cuerpo_del_issue():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        creada = await _crear_desde_modal(
            pilot, screen, titulo="With notes", notas="ask for the invoice first"
        )
        assert creada.cuerpo == "ask for the invoice first"


async def test_ctrl_s_tambien_crea():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        creada = await _crear_desde_modal(
            pilot, screen, titulo="Saved with ctrl+s", tecla="ctrl+s"
        )
        assert creada.titulo == "Saved with ctrl+s"


async def test_el_hint_de_nueva_no_promete_ctrl_enter():
    """`ctrl+enter` no es distinguible de `enter` en la mayoría de las terminales
    (sin el protocolo de teclado de Kitty de punta a punta llegan como el mismo
    byte `\\r`), así que ni el hint ni el botón deben prometerlo: `ctrl+s` es el
    atajo que de verdad dispara `crear` en cualquier terminal, y hoy vive en el
    label del botón (`[^s·create]`), no en el hint."""
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        dialogo = pilot.app.screen
        texto = str(dialogo.query_one("#nueva-hint").content)
        labels = " ".join(str(b.label) for b in dialogo.query(Button))
        assert "^s·create" in labels
        assert "^enter" not in texto
        assert "^enter" not in labels


async def test_ctrl_enter_crea_desde_cualquier_campo():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        modal.query_one("#nueva-titulo", Input).value = "From the date field"
        modal.query_one("#nueva-fecha", Input).focus()
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await _esperar(
            pilot, lambda: any(t.titulo == "From the date field" for t in screen.tareas)
        )
        assert any(t.titulo == "From the date field" for t in screen.tareas)


async def test_el_error_ocupa_la_fila_del_hint():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test(size=(80, 15)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        await pilot.press("ctrl+enter")  # sin título
        await pilot.pause()
        assert pilot.app.screen is modal  # no se creó nada
        assert modal.query_one("#nueva-error").display is True
        assert modal.query_one("#nueva-hint").display is False
        assert "title is required" in str(modal.query_one("#nueva-error").content)


async def test_ctrl_enter_guarda_la_fecha():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("G")
        tarea = screen.seleccionada
        await pilot.press("d")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FechaScreen)
        objetivo = date.today() + timedelta(days=11)
        modal.query_one("#fecha-input", Input).value = objetivo.isoformat()
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ListaScreen)
        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence == objetivo


# ------------------------------------------------------------------ tareas repetitivas
async def test_el_chip_de_repeticion_cicla_con_ctrl_r():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        chip = modal.query_one("#nueva-repeat", Button)
        assert modal.repeticion == "none"
        assert "repeat: none" in str(chip.label)

        for esperado in ("daily", "weekly", "biweekly", "monthly", "none"):
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert modal.repeticion == esperado
            assert f"repeat: {esperado}" in str(chip.label)


async def test_el_chip_de_repeticion_no_baila_ni_corta_ningun_valor():
    """El chip tiene ancho fijo: Textual no re-mide un Button con width:auto cuando le
    cambia el label, así que un ancho automático cortaría "biweekly" y haría bailar la
    fila a cada ciclo."""
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        chip = pilot.app.screen.query_one("#nueva-repeat", Button)
        anchos = set()
        for _ in range(6):
            anchos.add(chip.size.width)
            pintado = "".join(segmento.text for segmento in chip.render_line(0))
            assert str(chip.label).strip() in pintado  # entra entero, sin wrap ni recorte
            await pilot.press("ctrl+r")
            await pilot.pause()
        assert len(anchos) == 1


async def test_el_chip_de_repeticion_tambien_cicla_con_click():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        await pilot.click("#nueva-repeat")
        await pilot.pause()
        assert modal.repeticion == "daily"


async def test_repeticion_sin_fecha_avisa_y_no_se_aplica():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        creada = await _crear_desde_modal(
            pilot, screen, titulo="Repeat without date", repeticiones=1
        )
        assert creada.repeat is None
        assert any("repeat needs a due date" in m for m in _avisos(pilot.app))


async def test_repeticion_con_fecha_queda_guardada_en_el_cuerpo():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        objetivo = date.today() + timedelta(days=7)
        creada = await _crear_desde_modal(
            pilot,
            screen,
            titulo="Weekly report",
            notas="send it to accounting",
            fecha=objetivo.isoformat(),
            repeticiones=2,  # none -> daily -> weekly
        )
        assert creada.repeat == "weekly"
        assert creada.vence == objetivo
        assert creada.cuerpo == "send it to accounting"  # el metadato no ensucia las notas


async def test_la_lista_marca_las_repetitivas():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        marcas = [
            tabla.get_cell_at(Coordinate(fila, 1)).plain
            for fila in range(len(screen.visibles))
        ]
        assert marcas == ["↻" if t.repeat else "" for t in screen.visibles]
        assert "↻" in marcas  # la demo trae repetitivas, si no el test no probaría nada


async def test_el_detalle_muestra_la_repeticion():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("j")
        assert screen.seleccionada.repeat == "monthly"
        await pilot.press("enter")
        await pilot.pause()
        assert "↻ repeats monthly" in str(pilot.app.screen.query_one("#det-meta").content)


async def test_cerrar_una_repetitiva_crea_la_siguiente_ocurrencia():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("j")
        tarea = screen.seleccionada
        assert tarea.repeat == "monthly" and tarea.vence is not None

        await pilot.press("x")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ConfirmaScreen)
        await pilot.press("y")
        await _esperar(
            pilot,
            lambda: any(
                t.titulo == tarea.titulo and t.item_id != tarea.item_id
                for t in screen.tareas
            ),
        )

        assert all(t.item_id != tarea.item_id for t in screen.tareas)  # la vieja se cerró
        siguiente = next(t for t in screen.tareas if t.titulo == tarea.titulo)
        esperada = proxima_fecha(tarea.vence, "monthly", date.today())
        assert siguiente.vence == esperada
        assert siguiente.vence > date.today()  # nunca nace vencida
        assert siguiente.repeat == "monthly"
        assert siguiente.repo == tarea.repo
        assert siguiente.cuerpo == tarea.cuerpo
        assert any(f"↻ next: {esperada.isoformat()}" in m for m in _avisos(pilot.app))


async def test_cerrar_una_tarea_normal_no_crea_nada():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        assert tarea.repeat is None
        cuantas = len(screen.tareas)

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))
        assert len(screen.tareas) == cuantas - 1


async def test_si_falla_la_siguiente_ocurrencia_el_cierre_no_queda_en_silencio():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)

        async def explotar(tarea, hoy):
            raise ErrorGh("HTTP 502")

        screen.backend.repetir = explotar
        await pilot.press("j")
        tarea = screen.seleccionada
        assert tarea.repeat == "monthly"

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(
            pilot,
            lambda: any("couldn't create the next occurrence" in m for m in _avisos(pilot.app)),
        )
        assert any("HTTP 502" in m for m in _avisos(pilot.app))
        assert all(t.item_id != tarea.item_id for t in screen.tareas)  # el cierre sí pasó


async def test_repetitiva_sin_fecha_avisa_al_cerrar():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("j")
        tarea = screen.seleccionada
        screen.tareas = [
            replace(t, vence=None) if t.item_id == tarea.item_id else t for t in screen.tareas
        ]

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(
            pilot, lambda: any("next occurrence not created" in m for m in _avisos(pilot.app))
        )
        assert any("next occurrence not created" in m for m in _avisos(pilot.app))


# ------------------------------------------------------------------ confirmación y/n
async def test_confirmar_cierre_con_y():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ConfirmaScreen)

        await pilot.press("y")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ListaScreen)
        assert all(t.item_id != tarea.item_id for t in screen.tareas)


async def test_cancelar_cierre_con_n():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ConfirmaScreen)

        await pilot.press("n")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ListaScreen)
        assert any(t.item_id == tarea.item_id for t in screen.tareas)


# ------------------------------------------------------------------ detalle: scroll j/k
async def test_detalle_jk_scrollea_el_cuerpo():
    cuerpo_largo = "\n\n".join(f"Line {i}" for i in range(60))
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(80, 15)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        screen.tareas = [
            replace(t, cuerpo=cuerpo_largo) if t.item_id == tarea.item_id else t
            for t in screen.tareas
        ]

        await pilot.press("enter")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, DetalleScreen)
        cuerpo = modal.query_one("#det-cuerpo")
        assert cuerpo.scroll_target_y == 0

        await pilot.press("j")
        await pilot.pause()
        assert cuerpo.scroll_target_y == 1

        await pilot.press("j")
        await pilot.pause()
        assert cuerpo.scroll_target_y == 2

        await pilot.press("k")
        await pilot.pause()
        assert cuerpo.scroll_target_y == 1


# ------------------------------------------------------------------ lista: g/G
async def test_lista_g_y_g_mayuscula_van_a_los_extremos():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla")

        await pilot.press("G")
        await pilot.pause()
        assert tabla.cursor_row == len(screen.visibles) - 1

        await pilot.press("g")
        await pilot.pause()
        assert tabla.cursor_row == 0


# ------------------------------------------------------------------ header: contador de vencidas
async def test_header_muestra_contador_de_vencidas():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#cab-titulo").content.plain
        assert "7 pending" in texto
        assert "2 overdue" in texto


async def test_header_modo_repo_aplica_conteo_de_vencidas():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#cab-titulo").content.plain
        assert "2 pending" in texto
        assert "1 overdue" in texto


async def test_header_sin_vencidas_no_agrega_overdue():
    app = TareasApp(BackendDemo(repo_actual="nordic/erp"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#cab-titulo").content.plain
        assert "1 pending" in texto
        assert "overdue" not in texto


# ------------------------------------------------------------------ cursor por identidad
async def test_el_cursor_sigue_a_su_tarea_aunque_la_lista_se_reordene():
    """`_pintar_tabla` recordaba el NÚMERO de fila, pero `ordenar()` resortea por
    (vence, título): tras fechar una tarea el cursor quedaba sobre otra y un `x`
    inmediato cerraba la equivocada."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("j", "j", "j", "j")  # a media lista, lejos de los extremos
        tarea = screen.seleccionada
        assert tarea is not None and tarea.vence is not None

        atrasada = date.today() - timedelta(days=30)  # la manda a la primera fila
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(pilot.app.screen, FechaScreen)
        pilot.app.screen.query_one("#fecha-input", Input).value = atrasada.isoformat()
        await pilot.press("ctrl+enter")
        await _esperar(
            pilot,
            lambda: any(
                t.item_id == tarea.item_id and t.vence == atrasada for t in screen.tareas
            ),
        )
        await pilot.pause()

        assert screen.visibles[0].item_id == tarea.item_id  # se movió de fila…
        assert screen.seleccionada.item_id == tarea.item_id  # …y el cursor la siguió


async def test_si_la_tarea_del_cursor_desaparece_el_cursor_cae_en_una_valida():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("G")
        tarea = screen.seleccionada

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))
        await pilot.pause()

        tabla = screen.query_one("#tabla", TablaTareas)
        assert tabla.cursor_row == len(screen.visibles) - 1
        assert screen.seleccionada is not None


# ------------------------------------------------------------------ escrituras en vuelo
class BackendCierreLento(BackendDemo):
    """Demo cuyo cierre no termina hasta que se suelta el evento: deja ver la ventana
    en la que la fila sigue viva mientras corren las llamadas a `gh`."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.suelta = asyncio.Event()
        self.cierres: list[str] = []

    async def cerrar(self, tarea):
        self.cierres.append(tarea.item_id)
        await self.suelta.wait()
        await super().cerrar(tarea)


async def test_un_segundo_x_no_vuelve_a_cerrar_la_tarea_en_curso():
    """`action_cerrar` no era exclusiva y la fila seguía visible mientras corrían las 3
    llamadas a gh: un segundo `x` abría otro ConfirmaScreen sobre la MISMA tarea. Como
    `gh issue close` sobre un issue ya cerrado sale 0, el segundo cierre "funcionaba" y
    `_repetir` creaba una ocurrencia duplicada de verdad."""
    backend = BackendCierreLento()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("j")  # la mensual: duplicarla se ve en la ocurrencia siguiente
        tarea = screen.seleccionada
        assert tarea.repeat == "monthly"

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: bool(backend.cierres))
        assert backend.cierres == [tarea.item_id]

        await pilot.press("x")  # la fila sigue ahí, pero el cierre ya está en vuelo
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmaScreen)

        backend.suelta.set()
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))
        await _esperar(pilot, lambda: any(t.titulo == tarea.titulo for t in screen.tareas))
        assert backend.cierres == [tarea.item_id]
        assert len([t for t in screen.tareas if t.titulo == tarea.titulo]) == 1


async def test_la_fila_avisa_mientras_se_cierra_la_tarea():
    backend = BackendCierreLento()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: bool(backend.cierres))
        await pilot.pause()

        tabla = screen.query_one("#tabla", TablaTareas)
        fila = next(i for i, t in enumerate(screen.visibles) if t.item_id == tarea.item_id)
        assert tabla.get_cell_at(Coordinate(fila, 0)).plain == "closing…"

        backend.suelta.set()
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))


@pytest.mark.parametrize(
    "accion, pantalla",
    [
        ("action_ver", DetalleScreen),
        ("action_fecha", FechaScreen),
        ("action_cerrar", ConfirmaScreen),
        ("action_nueva", NuevaScreen),
    ],
)
async def test_dos_disparos_seguidos_no_apilan_dos_modales(accion, pantalla):
    """Un doble clic (o dos teclas casi simultáneas) agendaba dos workers ANTES de que
    el primero llegara a montar su modal: `screen_stack` terminaba con DOS pantallas
    idénticas apiladas (ver `_mostrar_modal`)."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)

        getattr(screen, accion)()
        getattr(screen, accion)()
        await pilot.pause()
        await pilot.pause()

        apiladas = [s for s in app.screen_stack if isinstance(s, pantalla)]
        assert len(apiladas) == 1

        # La que sobrevivió sigue funcional: esc la cierra y vuelve a la lista.
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is screen


# ------------------------------------------------------------------ efectos parciales
async def test_el_alta_a_medias_no_se_reporta_como_no_creada():
    """El issue ya existe en GitHub: decir "couldn't create the task" empuja a
    reintentar y duplicar."""
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)

        async def a_medias(repo, titulo, fecha, cuerpo=""):
            raise datos.ErrorParcial(
                "the new issue exists on GitHub but wasn't added to the board "
                "— check GitHub (HTTP 502)"
            )

        screen.backend.crear = a_medias
        await pilot.press("n")
        await pilot.pause()
        pilot.app.screen.query_one("#nueva-titulo", Input).value = "Half created"
        await pilot.press("ctrl+enter")
        await _esperar(
            pilot, lambda: any("wasn't added to the board" in m for m in _avisos(pilot.app))
        )
        assert any("wasn't added to the board" in m for m in _avisos(pilot.app))
        assert not any("couldn't create the task" in m for m in _avisos(pilot.app))


async def test_la_ocurrencia_a_medias_no_se_reporta_como_no_creada():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)

        async def a_medias(tarea, hoy):
            raise datos.ErrorParcial(
                "the new issue was created without a due date (HTTP 500)"
            )

        screen.backend.repetir = a_medias
        await pilot.press("j")
        assert screen.seleccionada.repeat == "monthly"

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(
            pilot, lambda: any("without a due date" in m for m in _avisos(pilot.app))
        )
        assert any("without a due date" in m for m in _avisos(pilot.app))
        assert not any(
            "couldn't create the next occurrence" in m for m in _avisos(pilot.app)
        )


# ------------------------------------------------------------------ picker de repos
class BackendReposLentos(BackendDemo):
    """Demo cuyo listado de repos no vuelve hasta que se suelta el evento."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.suelta = asyncio.Event()

    async def repos(self) -> list[str]:
        await self.suelta.wait()
        return await super().repos()


async def test_el_picker_se_llena_aunque_el_modal_abra_antes_que_la_carga():
    """`action_nueva` pasaba `self.repos` por valor y `cargar_repos` REASIGNABA el
    atributo: el modal abierto antes de que llegaran se quedaba vacío para siempre."""
    backend = BackendReposLentos()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await _esperar(pilot, lambda: not screen.cargando)
        assert screen.repos == []  # la carga de repos sigue en vuelo

        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NuevaScreen)
        lista = modal.query_one("#nueva-repos", OptionList)
        assert "loading repos" in str(lista.get_option_at_index(0).prompt)

        backend.suelta.set()
        await _esperar(pilot, lambda: bool(modal.repos))
        assert modal.repos == ["acme/web", "korta/api", "lumen/shop", "mesa/intranet", "vela/landing"]
        assert lista.option_count == 5
        assert modal.repo_elegido == "acme/web"


# ------------------------------------------------------------------ límite del Project
class BackendTruncado(BackendDemo):
    truncado = True


async def test_la_ui_avisa_cuando_el_project_llega_al_limite():
    app = TareasApp(BackendTruncado())
    async with app.run_test() as pilot:
        await _listo(pilot)
        await _esperar(pilot, lambda: any("item limit" in m for m in _avisos(pilot.app)))
        assert any("item limit" in m for m in _avisos(pilot.app))


async def test_sin_truncado_no_hay_aviso_de_limite():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        await _listo(pilot)
        assert not any("item limit" in m for m in _avisos(pilot.app))


# ------------------------------------------------------------------ red caída
class BackendCaido(BackendDemo):
    async def listar(self):
        raise ErrorGh("`gh` didn't respond in 30s (check your connection).")


async def test_un_error_al_listar_reemplaza_el_spinner_por_un_mensaje():
    """Guarda del cuelgue infinito: con el timeout de `gh`, «loading tasks…» ahora
    termina siempre en un mensaje accionable en vez de quedarse girando."""
    app = TareasApp(BackendCaido())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await _esperar(pilot, lambda: not screen.cargando)
        texto = screen.query_one("#vacio").content.plain
        assert "couldn't read the Project" in texto
        assert "didn't respond" in texto
        assert "loading tasks…" not in texto
        assert "press r or click ⟳ to retry" in texto


# ------------------------------------------------------------------ contraste secundario
def _pintado(widget) -> list:
    return [
        segmento
        for y in range(widget.size.height)
        for segmento in widget.render_line(y)
        if segmento.text.strip()
    ]


def _es_color_7(segmento) -> bool:
    """Color 7 de la paleta del terminal, NO un #ffffff fijo: si se colara un truecolor
    la app dejaría de heredar la paleta y el texto sería invisible en claro."""
    return not segmento.style.dim and segmento.style.color.number == 7


async def test_el_texto_secundario_de_la_lista_no_va_en_dim():
    """`dim` en esta paleta cae a 3,98:1 sobre fondo claro. El texto secundario que se
    LEE (el número de issue en la columna repo#issue, el timestamp del refresco) va en
    el color 7, que mide 7,38:1 en claro y 10,72:1 en oscuro sin perder jerarquía
    contra el texto normal."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        await pilot.press("j")  # el cursor pisa el color de su fila: se corre a la 1
        await pilot.pause()

        numero = [s for s in tabla.render_line(0) if s.text.strip() == "#31"]
        assert numero
        assert all(_es_color_7(s) for s in numero)

        segmentos = _pintado(screen.query_one("#cab-refrescar"))
        assert segmentos
        assert all(_es_color_7(s) for s in segmentos)


async def test_el_color_por_repo_es_estable_y_evita_rojo_y_amarillo():
    """El nombre del repo en la columna repo#issue usa un tono determinista (ver
    `color_repo`, probado suelto en `test_repeticion.py`): el mismo repo siempre pinta
    el mismo color, nunca rojo -reservado a vencido/peligro- ni amarillo -acento/
    selección-, y nunca `dim`. El `#N` de al lado sigue en color 7 (ver el test de
    arriba), sin heredar el color del repo."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        await pilot.press("j")  # el cursor pisa el color de su fila: se corre a la 1
        await pilot.pause()

        # vela/landing aparece dos veces en la demo (filas 0 y 4): mismo repo, mismo
        # color en ambas.
        landing_31 = [s for s in tabla.render_line(0) if s.text.strip() == "landing"]
        landing_3 = [s for s in tabla.render_line(4) if s.text.strip() == "landing"]
        assert landing_31 and landing_3
        assert not any(s.style.dim for s in [*landing_31, *landing_3])
        colores = {s.style.color for s in [*landing_31, *landing_3]}
        assert colores == {Style.parse(color_repo("vela/landing")).color}
        assert color_repo("vela/landing") not in {"red", "yellow"}


async def test_los_hints_del_estado_vacio_no_van_en_dim():
    app = TareasApp(vacio_demo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        segmentos = _pintado(screen.query_one("#vacio"))
        assert segmentos
        assert all(not s.style.dim for s in segmentos)
        hint = [s for s in segmentos if "+ new" in s.text]
        assert hint
        assert all(_es_color_7(s) for s in hint)


async def test_los_hints_y_los_botones_secundarios_de_los_modales_no_van_en_dim():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("d")
        await pilot.pause()
        modal = pilot.app.screen
        for selector in ("#fecha-hint", "#fecha-cancelar"):
            segmentos = _pintado(modal.query_one(selector))
            assert segmentos, selector
            assert all(_es_color_7(s) for s in segmentos), selector


async def test_la_jerarquia_de_vencimientos_se_lee_entera():
    """El vencimiento es el dato más importante de la fila y "in 19d"/"—" iban en `dim`,
    lo más lavado de la pantalla. La jerarquía queda: vencida (rojo bold) > hoy (acento
    bold) > próxima (acento) > lejana o sin fecha (color 7). Ninguna en dim."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        assert tabla.cursor_row == 0  # la fila 0 la pinta el cursor: se mira de la 1 abajo

        def vence(fila: int, esperado: str):
            columna = [s for s in tabla.render_line(fila) if s.text.strip()][0]
            assert columna.text.strip() == esperado, (fila, columna.text)
            assert not columna.style.dim, (fila, esperado)
            return columna

        vencida = vence(1, "3d ago")
        assert vencida.style.color.number == 1 and vencida.style.bold

        hoy = vence(2, "today")
        assert hoy.style.color.number == 3 and hoy.style.bold

        proxima = vence(3, "in 2d")
        assert proxima.style.color.number == 3

        for fila, esperado in ((5, "in 19d"), (6, "—")):
            assert _es_color_7(vence(fila, esperado)), esperado


async def test_los_placeholders_de_los_inputs_se_leen():
    """El theme ansi de Textual pinta los placeholders con `text-style: dim` sobre
    ansi_default (Input.DEFAULT_CSS, rama `&:ansi`): el mismo problema de contraste.
    El texto ya escrito y el cursor no se tocan."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen

        # "#nueva-filtro" tiene el foco: su primer carácter lo pinta el cursor, así que
        # se mira desde el segundo.
        for selector, aguja in (
            ("#nueva-filtro", "ilter repo"),
            ("#nueva-titulo", "what did they ask for?"),
            ("#nueva-notas", "notes, links, context (optional)"),
            ("#nueva-fecha", "YYYY-MM-DD · fri · +10d (optional)"),
        ):
            campo = modal.query_one(selector, Input)
            segmentos = [s for s in campo.render_line(0) if aguja in s.text]
            assert segmentos, selector
            assert all(_es_color_7(s) for s in segmentos), selector

        titulo = modal.query_one("#nueva-titulo", Input)
        titulo.focus()
        await pilot.pause()
        await pilot.press("h", "o", "l", "a")
        await pilot.pause()
        escrito = [s for s in titulo.render_line(0) if "hola" in s.text]
        assert escrito
        # el texto real va al color del terminal (contraste pleno), no al secundario
        assert all(not s.style.dim and s.style.color.number is None for s in escrito)


async def test_los_separadores_decorativos_siguen_en_dim():
    """Lo que NO se lee -el " · " del header- se queda en dim: es lo que mantiene la
    jerarquía sin costar legibilidad."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        segmentos = _pintado(screen.query_one("#cab-titulo"))
        assert any(s.style.dim and not s.text.strip(" ·") for s in segmentos)


# ------------------------------------------------------------------ arranque desde caché
class BackendListarLento(BackendDemo):
    """La primera lectura no vuelve hasta que se suelta el evento.

    Es la ventana donde antes solo había «loading tasks…»: `gh project item-list`
    tarda ~1 s y la pantalla no tenía nada que pintar hasta que contestaba.
    """

    def __init__(self, guardado: datos.Instantanea, **kwargs) -> None:
        super().__init__(**kwargs)
        self.suelta = asyncio.Event()
        self._guardado = guardado

    def instantanea(self) -> datos.Instantanea:
        return self._guardado

    async def listar(self):
        await self.suelta.wait()
        return await super().listar()


def _cacheadas(cuantas: int) -> list[datos.Tarea]:
    return [
        datos.Tarea(
            item_id=f"cache-{i}",
            repo="acme/web",
            numero=i,
            titulo=f"Cached task {i}",
            url=f"https://example.com/{i}",
            cuerpo="",
            vence=date.today() + timedelta(days=i),
            repeat=None,
        )
        for i in range(cuantas)
    ]


async def test_la_lista_se_pinta_del_cache_antes_de_que_conteste_la_red():
    guardado = datos.Instantanea(
        tareas=_cacheadas(3), momento=datetime.now() - timedelta(minutes=4)
    )
    backend = BackendListarLento(guardado)
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await pilot.pause()

        # la red sigue sin contestar y la lista YA está en pantalla
        assert not backend.suelta.is_set()
        assert screen.query_one("#tabla").row_count == 3
        assert [t.item_id for t in screen.visibles] == ["cache-0", "cache-1", "cache-2"]
        assert screen.query_one("#vacio").display is False

        # el header dice cuántas hay y de cuándo son, en vez de "loading…"
        cabecera = screen.query_one("#cab-titulo").content.plain
        assert "3 pending" in cabecera
        assert "loading" not in cabecera
        assert "4m ago" in screen.query_one("#cab-refrescar").content.plain

        # cuando por fin llega la red, manda ella
        backend.suelta.set()
        await _esperar(pilot, lambda: any(t.item_id.startswith("demo") for t in screen.tareas))
        assert len(screen.visibles) == 7


async def test_sin_cache_el_arranque_sigue_diciendo_loading():
    backend = BackendListarLento(datos.Instantanea())
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await pilot.pause()
        assert screen.cargando is True
        assert "loading tasks…" in screen.query_one("#vacio").content.plain
        assert "loading…" in screen.query_one("#cab-titulo").content.plain
        backend.suelta.set()
        await _esperar(pilot, lambda: not screen.cargando)


async def test_el_cache_del_repo_actual_filtra_desde_el_primer_frame():
    """Sin esto la lista se pintaba entera y saltaba al repo recién cuando volvía
    `gh repo view` (~0,3 s): un parpadeo con la lista de otro alcance."""
    guardado = datos.Instantanea(tareas=_cacheadas(3), repo_actual="acme/web")
    backend = BackendListarLento(guardado, repo_actual="acme/web")
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await pilot.pause()
        assert screen.modo_repo is True
        assert screen.repo_actual == "acme/web"
        assert screen.query_one("#cab-titulo").content.plain.startswith("acme/web")
        backend.suelta.set()


# ------------------------------------------------------------------ escrituras optimistas
class BackendRefrescoColgado(BackendDemo):
    """La primera lectura llega; el refresco que sigue a una escritura queda colgado.

    Es justo el ~1 s en el que la fila mostraba el dato viejo: la escritura ya había
    terminado, pero la UI esperaba al `item-list` completo para enterarse.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.lecturas = 0
        self.suelta = asyncio.Event()

    async def listar(self):
        self.lecturas += 1
        if self.lecturas > 1:
            await self.suelta.wait()
        return await super().listar()


async def test_cerrar_saca_la_fila_sin_esperar_al_refresco():
    backend = BackendRefrescoColgado()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        assert tarea.repeat is None
        cuantas = len(screen.tareas)

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))

        assert backend.lecturas > 1  # el refresco se disparó…
        assert not backend.suelta.is_set()  # …y sigue sin contestar
        assert len(screen.tareas) == cuantas - 1
        assert screen.query_one("#tabla").row_count == cuantas - 1
        backend.suelta.set()


async def test_la_fecha_nueva_se_ve_sin_esperar_al_refresco():
    backend = BackendRefrescoColgado()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("G")
        tarea = screen.seleccionada
        assert tarea.vence is None

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("3")  # 3 = "+3 days"
        await _esperar(
            pilot,
            lambda: any(t.item_id == tarea.item_id and t.vence for t in screen.tareas),
        )

        assert backend.lecturas > 1 and not backend.suelta.is_set()
        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence == date.today() + timedelta(days=3)
        backend.suelta.set()


async def test_la_tarea_nueva_aparece_sin_esperar_al_refresco():
    backend = BackendRefrescoColgado(repo_actual="vela/landing")
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)

        await pilot.press("n")
        await pilot.pause()
        pilot.app.screen.query_one("#nueva-titulo", Input).value = "Fresh one"
        await pilot.press("ctrl+enter")
        await _esperar(pilot, lambda: any(t.titulo == "Fresh one" for t in screen.tareas))

        assert backend.lecturas > 1 and not backend.suelta.is_set()
        creada = next(t for t in screen.tareas if t.titulo == "Fresh one")
        assert creada.repo == "vela/landing"
        backend.suelta.set()


async def test_la_siguiente_ocurrencia_aparece_sin_esperar_al_refresco():
    backend = BackendRefrescoColgado()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("j")
        tarea = screen.seleccionada
        assert tarea.repeat == "monthly"

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(
            pilot,
            lambda: any(
                t.titulo == tarea.titulo and t.item_id != tarea.item_id for t in screen.tareas
            ),
        )

        assert backend.lecturas > 1 and not backend.suelta.is_set()
        siguiente = next(
            t
            for t in screen.tareas
            if t.item_id != tarea.item_id and t.titulo == tarea.titulo
        )
        assert siguiente.vence == proxima_fecha(tarea.vence, "monthly", date.today())
        assert len([t for t in screen.tareas if t.titulo == tarea.titulo]) == 1
        backend.suelta.set()


# ------------------------------------------------------------------ cierres que rebotan
class BackendZombi(BackendDemo):
    """`cerrar` funciona pero el Project sigue devolviendo la tarea como pendiente.

    Es el comportamiento real de Projects: el Status "Done" lo pone un workflow que
    corre DESPUÉS de que `gh issue close` volvió, así que el refresco inmediato llega
    a destiempo y resucitaba la fila que el usuario acababa de ver desaparecer.
    """

    async def cerrar(self, tarea) -> None:
        return None


async def test_una_tarea_cerrada_no_revive_aunque_el_project_la_siga_mandando():
    backend = BackendZombi()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        assert tarea.repeat is None

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))

        # el refresco ya trajo la lista completa, con la cerrada incluida…
        await _esperar(pilot, lambda: screen.ultimo_ok is not None)
        await pilot.pause()
        assert tarea.item_id in screen.cerradas
        assert all(t.item_id != tarea.item_id for t in screen.tareas)  # …y sigue oculta


async def test_refrescar_a_mano_devuelve_una_tarea_reabierta_en_github():
    """Vía de escape: si la tarea se reabrió afuera, `r` deja de esconderla."""
    backend = BackendZombi()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada

        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await _esperar(pilot, lambda: all(t.item_id != tarea.item_id for t in screen.tareas))

        await pilot.press("r")
        await _esperar(pilot, lambda: any(t.item_id == tarea.item_id for t in screen.tareas))
        assert screen.cerradas == set()
        assert any(t.item_id == tarea.item_id for t in screen.tareas)


async def test_salir_con_una_lectura_en_vuelo_no_revienta():
    """Salir mientras corre un `gh` cancela su worker, pero el `finally` de `refrescar`
    seguía repintando: reventaba con NoMatches sobre la pantalla ya desmontada. Con el
    caché la ventana se abrió de par en par (la app es usable mientras la red vuelve),
    y `bin/tareas` interpreta esa caída como crash y relanza la app en bucle."""
    backend = BackendListarLento(datos.Instantanea(tareas=_cacheadas(2)))
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        assert not backend.suelta.is_set()  # la lectura sigue en vuelo al salir
    # al cerrar el contexto, `run_test` relanza cualquier excepción de worker: que no
    # haya excepción ES la prueba.


# ------------------------------------------------------------------ fantasmas del caché
CONFIG_REAL = datos.Config(
    owner="pit",
    project="1",
    campo_fecha="Due date",
    estado_hecho="Done",
    project_id="PVT_test",
    campo_fecha_id="PVTF_test",
    project_title="Client Tasks",
)


def _gh_project_zombi(repeat: str | None = None, creados: list | None = None):
    """`gh` de mentira cuyo Project NUNCA marca Done lo que se cierra.

    Es el comportamiento real: el Status lo pone un workflow de Projects que corre
    después de que `gh issue close` volvió, y puede tardar. Mientras tanto la tarea
    sigue llegando como pendiente en cada `item-list`.
    """
    cuerpo = f"<!-- tareas:repeat={repeat} -->" if repeat else ""

    def crear_issue(_variables) -> str:
        numero = 100 + len(creados if creados is not None else [])
        if creados is not None:
            creados.append(numero)
        return gh_falso.issue_creado(numero)

    return GhFalso(
        {
            "ItemsDelProject": gh_falso.pagina(
                [
                    gh_falso.item(
                        1, titulo="Renew SSL", cuerpo=cuerpo, fecha="2026-08-01"
                    )
                ]
            ),
            "CrearIssue": crear_issue,
            "AgregarItem": gh_falso.item_agregado(),
        }
    )


async def _cerrar_la_primera(pilot, screen) -> None:
    await _esperar(pilot, lambda: not screen.cargando)
    await pilot.pause()
    assert screen.visibles, "la tarea debería estar en pantalla"
    await pilot.press("x")
    await _esperar(pilot, lambda: isinstance(pilot.app.screen, ConfirmaScreen))
    await pilot.press("y")
    await _esperar(pilot, lambda: not screen.visibles)


async def test_una_tarea_cerrada_no_revive_al_reiniciar_la_app(monkeypatch):
    """El caché guardaba la lista cruda del Project -con la recién cerrada dentro- y
    `self.cerradas` vivía solo en memoria: al reabrir la app la tarea reaparecía como
    pendiente aunque su issue ya estuviera cerrado en GitHub."""
    monkeypatch.setattr(datos, "gh", _gh_project_zombi())

    app = TareasApp(datos.Backend(CONFIG_REAL))
    async with app.run_test(size=(110, 24)) as pilot:
        await _cerrar_la_primera(pilot, pilot.app.screen)

    # proceso nuevo: `self.cerradas` nace vacío y solo queda lo que haya en disco
    otra = TareasApp(datos.Backend(CONFIG_REAL))
    async with otra.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await pilot.pause()
        assert screen.tareas == [], "la cerrada revivió desde el caché"
        assert "PVTI_1" in screen.cerradas  # y se sabe por qué está oculta
        await _esperar(pilot, lambda: screen.ultimo_ok is not None)
        await pilot.pause()
        assert screen.tareas == [], "el refresco la volvió a traer"


async def test_reiniciar_no_duplica_la_ocurrencia_de_una_repetitiva(monkeypatch):
    """La consecuencia cara del fantasma: cerrarlo otra vez creaba en GitHub una
    segunda ocurrencia con la MISMA fecha, sin forma de saber cuál sobra."""
    creados: list[int] = []
    monkeypatch.setattr(datos, "gh", _gh_project_zombi(repeat="monthly", creados=creados))

    app = TareasApp(datos.Backend(CONFIG_REAL))
    async with app.run_test(size=(110, 24)) as pilot:
        await _cerrar_la_primera(pilot, pilot.app.screen)
    assert creados == [100], "la primera ocurrencia debería crearse una sola vez"

    otra = TareasApp(datos.Backend(CONFIG_REAL))
    async with otra.run_test(size=(110, 24)) as pilot:
        screen = pilot.app.screen
        await pilot.pause()
        assert screen.visibles == [], "no hay fantasma que cerrar"
    assert creados == [100], "se creó una ocurrencia duplicada"


async def test_repetir_no_crea_una_ocurrencia_que_ya_esta_en_la_lista():
    """Guarda independiente del caché: cubre cerrar dos veces la misma repetitiva
    (dos instancias abiertas, o reabierta en GitHub) sin duplicar la serie."""
    backend = BackendDemo()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.press("j")
        tarea = screen.seleccionada
        assert tarea.repeat == "monthly" and tarea.vence is not None

        proxima = proxima_fecha(tarea.vence, "monthly", date.today())
        gemela = replace(tarea, item_id="ya-existe", vence=proxima)
        screen.tareas = [*screen.tareas, gemela]

        creadas: list[str] = []

        async def registrar(t, hoy):
            creadas.append(t.item_id)
            return gemela

        backend.repetir = registrar
        await screen._repetir(tarea)
        await pilot.pause()

        assert creadas == [], "creó una ocurrencia que ya existía"
        assert any("already exists" in m for m in _avisos(pilot.app))


# ------------------------------------------------------------------ atajos por pantalla
#: Mapa completo de atajos: cada modal, los labels de sus botones y su línea de hint.
#: La regla es una sola —el atajo propio de cada botón va EN SU LABEL con «·», y `esc`
#: (cancelar/volver, que vale en todos) vive en el hint—, así que este mapa es a la vez
#: la documentación y la regresión: cambiar un label sin tocar el hint rompe el test.
ATAJOS_ESPERADOS = {
    "d": (
        "#dlg-fecha",
        ["1·today", "2·tomorrow", "3·+3 days", "4·next week", "5·+1 month",
         "[^s·save]", "[^x·clear]", "[cancel]"],
        "esc cancel",
    ),
    "enter": (
        "#dlg-detalle",
        ["[x·close task]", "[d·change date]", "[back]"],
        "j/k scroll · esc back",
    ),
    "x": (
        "#dlg-confirma",
        ["[y·yes, close]", "[n·cancel]"],
        "esc cancel",
    ),
}


@pytest.mark.parametrize("tecla", list(ATAJOS_ESPERADOS))
async def test_cada_boton_de_cada_modal_anuncia_su_atajo(tecla):
    contenedor, labels, hint = ATAJOS_ESPERADOS[tecla]
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press(tecla)
        await pilot.pause()
        dialogo = pilot.app.screen.query_one(contenedor)
        assert [str(b.label) for b in dialogo.query(Button)] == labels
        assert str(dialogo.query_one(".hint").render()) == hint


async def test_los_botones_de_nueva_anuncian_su_atajo():
    """Aparte porque el chip de repetición se rellena a ancho fijo (ver
    `_etiqueta_repeat`) y el label real trae los espacios de relleno."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        dialogo = pilot.app.screen.query_one("#dlg-nueva")
        labels = [str(b.label).rstrip() for b in dialogo.query(Button)]
        assert labels == [
            "1·today", "2·tomorrow", "3·+3 days", "4·next week", "5·+1 month",
            "↻ ^r·repeat: none", "[^s·create]", "[cancel]",
        ]
        assert str(dialogo.query_one("#nueva-hint").render()) == "esc cancel"


@pytest.mark.parametrize("hint_id,tecla", [("#fecha-hint", "d"), ("#nueva-hint", "n")])
async def test_ningun_hint_promete_ctrl_enter(hint_id, tecla):
    """`ctrl+enter` llega como un `\\r` indistinguible de enter en cualquier terminal sin
    el protocolo de Kitty de punta a punta, así que el binding no dispara: ni el hint
    ni ningún botón deben anunciarlo. El que sí llega siempre es `ctrl+s`, y hoy vive
    en el label del botón de guardar/crear (el hint ya no lo repite)."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press(tecla)
        await pilot.pause()
        dialogo = pilot.app.screen
        texto_hint = str(dialogo.query_one(hint_id).render())
        labels = " ".join(str(b.label) for b in dialogo.query(Button))
        assert "^enter" not in texto_hint
        assert "^enter" not in labels
        assert "^s" in labels


async def test_el_detalle_cierra_la_tarea_con_x():
    """Mismo atajo que en la lista: el gesto no cambia según dónde estés parado."""
    backend = BackendDemo()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        await pilot.press("enter")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, DetalleScreen))
        await pilot.press("x")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, ConfirmaScreen))
        await pilot.press("y")
        await _esperar(pilot, lambda: tarea not in screen.tareas)

        assert tarea not in screen.tareas


async def test_el_detalle_abre_la_fecha_con_d():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("enter")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, DetalleScreen))
        await pilot.press("d")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, FechaScreen))

        assert isinstance(pilot.app.screen, FechaScreen)


async def test_ctrl_x_limpia_el_vencimiento():
    backend = BackendDemo()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        assert tarea.vence is not None
        await pilot.press("d")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, FechaScreen))
        await pilot.press("ctrl+x")
        await _esperar(
            pilot, lambda: screen.seleccionada is not None and not isinstance(
                pilot.app.screen, FechaScreen
            )
        )
        await pilot.pause(0.1)

        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence is None


async def test_ctrl_p_revela_el_picker_de_repos_en_modo_repo():
    """En modo repo el picker arranca oculto: `^p` (o un clic en el chip) lo abre.
    Va con ctrl porque hay Inputs con el foco comiéndose las letras sueltas."""
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert not modal.query_one("#nueva-repos", OptionList).display

        await pilot.press("ctrl+p")
        await pilot.pause()

        assert modal.query_one("#nueva-repos", OptionList).display
        assert not modal.query("#nueva-repo-fijo")


async def test_ctrl_p_no_revienta_cuando_el_picker_ya_esta_a_la_vista():
    """Fuera de modo repo no hay chip que quitar: el atajo tiene que ser inocuo."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()

        assert isinstance(pilot.app.screen, NuevaScreen)
        assert pilot.app.screen.query_one("#nueva-repos", OptionList).display


# ------------------------------------------------------------------ campos visibles
async def test_los_inputs_se_ven_como_campos_aunque_no_tengan_el_foco():
    """El marcador iba en `ansi_default` -el color del fondo-, así que un Input sin foco
    era indistinguible de una línea de texto: por eso el campo de notas pasaba
    desapercibido. Sin foco va en color 7; con foco, en el acento."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen

        for selector in ("#nueva-titulo", "#nueva-notas", "#nueva-fecha"):
            tipo, color = modal.query_one(selector, Input).styles.border_left
            assert tipo == "solid", selector
            assert color.ansi == 7, (selector, color)

        modal.query_one("#nueva-notas", Input).focus()
        await pilot.pause()
        _, color = modal.query_one("#nueva-notas", Input).styles.border_left
        assert color.ansi == 3, color  # el acento marca cuál está activo


async def test_el_detalle_separa_la_meta_del_cuerpo_con_una_regla_visible():
    """Iba en `$panel`, que es ansi_black: EL MISMO color que el fondo del diálogo
    ($surface), así que la línea estaba en el árbol y no se veía en ninguna paleta."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press("enter")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, DetalleScreen))
        cuerpo = pilot.app.screen.query_one("#det-cuerpo")
        fondo = pilot.app.screen.query_one("#dlg-detalle").styles.background

        tipo, color = cuerpo.styles.border_top
        assert tipo == "solid"
        assert color.ansi == 7
        assert color.ansi != fondo.ansi


async def test_el_vencimiento_del_detalle_conserva_su_color_semantico():
    """La columna de la lista pinta las vencidas en rojo; abrir el detalle no debería
    perder el dato que más se mira."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        # La lista viene ordenada por vencimiento: la primera es la más atrasada.
        modal = DetalleScreen(screen.visibles[0])
        atrasada = [s for s in modal._meta().render(pilot.app.console) if "overdue" in s.text]

        assert atrasada
        assert all(s.style.bold and s.style.color.name == "red" for s in atrasada)


# ------------------------------------------------------------------ tareas recién nacidas
class BackendCiego(BackendDemo):
    """El Project todavía no devuelve lo que se acaba de crear.

    Es el comportamiento REAL medido contra GitHub: un item recién agregado tarda
    entre 4 y 6 segundos en salir en la lectura (mismo retardo por GraphQL que por
    `gh project item-list`), y el refresco que dispara el alta llega mucho antes.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.invisibles: set[str] = set()

    async def crear(self, repo, titulo, fecha, cuerpo=""):
        creada = await super().crear(repo, titulo, fecha, cuerpo)
        self.invisibles.add(creada.item_id)
        return creada

    async def listar(self):
        return [t for t in await super().listar() if t.item_id not in self.invisibles]


async def _crear_tarea(pilot, titulo: str = "Nueva del cliente") -> None:
    await pilot.press("n")
    await pilot.pause()
    modal = pilot.app.screen
    modal.query_one("#nueva-titulo", Input).value = titulo
    modal.query_one("#nueva-crear", Button).press()
    await pilot.pause()


async def test_una_tarea_recien_creada_no_desaparece_con_el_refresco_del_alta():
    """El síntoma que el usuario veía como «tuve que reiniciar la app»: la fila
    aparecía y al segundo se borraba sola, porque el refresco pisaba la lista con una
    lectura que GitHub todavía no había puesto al día."""
    backend = BackendCiego()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await _crear_tarea(pilot)
        await _esperar(pilot, lambda: any(t.titulo == "Nueva del cliente" for t in screen.tareas))

        # el refresco del alta ya corrió y el Project sigue sin devolverla
        screen.refrescar()
        await _esperar(pilot, lambda: screen.ultimo_ok is not None)
        await pilot.pause(0.1)

        assert [t.titulo for t in screen.tareas].count("Nueva del cliente") == 1


async def test_la_naciente_se_suelta_en_cuanto_el_project_la_devuelve():
    """Sostenerla de más la duplicaría: la lista la traería y nosotros también."""
    backend = BackendCiego()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await _crear_tarea(pilot)
        await _esperar(pilot, lambda: screen.nacientes)

        backend.invisibles.clear()  # GitHub ya la publica
        screen.refrescar()
        await _esperar(pilot, lambda: not screen.nacientes)
        await pilot.pause(0.1)

        assert [t.titulo for t in screen.tareas].count("Nueva del cliente") == 1
        assert screen.nacientes == {}


async def test_una_naciente_no_se_queda_pegada_para_siempre(monkeypatch):
    """Si GitHub nunca la devuelve, la fila tiene que irse sola en vez de quedar como
    un fantasma que ningún refresco puede sacar."""
    monkeypatch.setattr("tareas_tui.app.VIDA_NACIENTES", 0.0)
    backend = BackendCiego()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await _crear_tarea(pilot)
        await _esperar(pilot, lambda: not screen.nacientes)
        await pilot.pause(0.1)

        assert screen.nacientes == {}
        assert not any(t.titulo == "Nueva del cliente" for t in screen.tareas)


async def test_cerrar_una_naciente_la_saca_de_la_lista():
    """Cerrar gana sobre sostenerla: si no, la cerrada volvería en cada refresco."""
    backend = BackendCiego()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await _crear_tarea(pilot)
        await _esperar(pilot, lambda: screen.nacientes)

        nueva = next(t for t in screen.tareas if t.titulo == "Nueva del cliente")
        screen.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(
            screen.visibles.index(nueva), 0
        )
        await pilot.press("x")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, ConfirmaScreen))
        await pilot.press("y")
        await _esperar(pilot, lambda: not screen.nacientes)

        screen.refrescar()
        await pilot.pause(0.2)
        assert not any(t.titulo == "Nueva del cliente" for t in screen.tareas)


# ------------------------------------------------------------------ chip de PR vinculado
def _fila_de(screen, numero: int) -> int:
    """Índice de fila de la tarea con ese número de issue."""
    return next(i for i, t in enumerate(screen.visibles) if t.numero == numero)


def _celda_titulo(screen, numero: int):
    tabla = screen.query_one("#tabla", TablaTareas)
    return tabla.get_cell_at(Coordinate(_fila_de(screen, numero), 3))


async def _abrir_detalle(pilot, screen, numero: int):
    screen.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(
        _fila_de(screen, numero), 0
    )
    await pilot.press("enter")
    await pilot.pause()
    return pilot.app.screen


def _actividad(pantalla) -> str:
    lineas = pantalla.query("#det-actividad")
    return str(lineas.first().content) if lineas else ""


async def test_el_chip_del_pr_resume_estado_y_ci_en_un_glifo():
    """La demo trae los tres casos: CI verde, CI roja y draft (ver `_DEMO_PR`)."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)

        assert _celda_titulo(screen, 3).plain.endswith("#87✓")  # abierto, CI verde
        assert _celda_titulo(screen, 48).plain.endswith("#112✗")  # abierto, CI roja
        assert _celda_titulo(screen, 21).plain.endswith("#9·")  # draft


async def test_una_tarea_sin_pr_no_paga_ningun_ruido():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        sin_pr = [t.numero for t in screen.visibles if t.pr is None]

        assert sin_pr, "la demo tiene que traer tareas sin PR o el test no prueba nada"
        for numero in sin_pr:
            celda = _celda_titulo(screen, numero)
            assert "#" not in celda.plain
            assert not celda.spans, "una fila sin PR no lleva ni un estilo de chip"


async def test_el_chip_usa_verde_de_exito_y_rojo_de_error():
    """Doctrina de color de la app: verde/rojo ANSI y color 7 (SECUNDARIO) para lo
    secundario. Nunca `dim`, que en esta paleta no llega al contraste mínimo."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        estilo = {n: _celda_titulo(screen, n).spans[-1].style for n in (3, 48, 21)}

        assert estilo[3] == "green"
        assert estilo[48] == "bold red"
        assert estilo[21] == datos.SECUNDARIO
        assert "dim" not in str(estilo.values())


@pytest.mark.parametrize("size", TAMANOS)
async def test_los_chips_quedan_alineados_y_ninguno_desborda_su_columna(size):
    """El chip va pegado al borde derecho de la columna elástica: sin columna propia
    (que la pagarían TODAS las filas) pero igual alineado entre sí."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=size) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        ancho = tabla.columns[list(tabla.columns)[3]].width
        con_chip = [
            tabla.get_cell_at(Coordinate(fila, 3)).plain
            for fila, tarea in enumerate(screen.visibles)
            if tarea.pr is not None
        ]

        assert con_chip
        assert all(len(celda) == ancho for celda in con_chip), con_chip
        assert all(
            len(tabla.get_cell_at(Coordinate(fila, 3)).plain) <= ancho
            for fila in range(len(screen.visibles))
        )


async def test_sin_lugar_para_el_titulo_el_chip_cede():
    """En una columna tan angosta que el chip se la comería entera, gana el título: una
    fila que solo dijera «#112✗» no comunica de qué tarea habla."""
    tarea = BackendDemo()._tareas[0]
    con_pr = replace(tarea, pr=datos.PrVinculado(numero=112, estado="open", ci="failure"))

    assert "#112" not in ListaScreen._celda_titulo(con_pr, 6).plain
    assert ListaScreen._celda_titulo(con_pr, 30).plain.endswith("#112✗")


async def test_el_detalle_dice_con_palabras_lo_que_el_chip_resume():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)

        assert _actividad(await _abrir_detalle(pilot, screen, 3)) == (
            "PR #87 · open · CI passing · ready to merge"
        )
        await pilot.press("escape")
        assert "PR #112 · open · CI failing" in _actividad(
            await _abrir_detalle(pilot, screen, 48)
        )
        await pilot.press("escape")
        assert _actividad(await _abrir_detalle(pilot, screen, 21)) == (
            "PR #9 · draft · CI running"
        )


async def test_el_detalle_cuenta_los_comentarios_del_issue():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)

        assert _actividad(await _abrir_detalle(pilot, screen, 48)).endswith("3 comments")
        await pilot.press("escape")
        # Sin PR pero con conversación: la línea existe igual, y en singular.
        assert _actividad(await _abrir_detalle(pilot, screen, 12)) == "1 comment"


async def test_sin_pr_ni_comentarios_el_detalle_no_gasta_una_fila():
    """En un pane de 15 filas, una línea vacía es una línea menos de descripción."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(80, 15)) as pilot:
        screen = await _listo(pilot)
        detalle = await _abrir_detalle(pilot, screen, 31)

        assert not detalle.query("#det-actividad")


@pytest.mark.parametrize(
    ("pr", "esperado"),
    [
        (datos.PrVinculado(7, "merged", "success"), "PR #7 · merged"),
        (datos.PrVinculado(7, "closed", "failure"), "PR #7 · closed unmerged"),
        (datos.PrVinculado(7, "open", "none", True), "PR #7 · open · no checks · ready to merge"),
        (datos.PrVinculado(7, "open", "success"), "PR #7 · open · CI passing"),
    ],
)
async def test_el_resumen_del_detalle_separa_el_estado_del_pr_del_de_la_ci(pr, esperado):
    """Lo que el glifo no puede distinguir -mergeado contra CI verde, cerrado contra CI
    roja- acá se dice con palabras, que es para lo que el detalle tiene lugar."""
    assert datos.resumen_pr(pr) == esperado


@pytest.mark.parametrize("size", TAMANOS)
async def test_la_linea_de_actividad_no_estira_el_detalle_fuera_de_la_pantalla(size):
    app = TareasApp(BackendDemo())
    async with app.run_test(size=size) as pilot:
        screen = await _listo(pilot)
        detalle = await _abrir_detalle(pilot, screen, 48)  # trae PR y comentarios

        assert detalle.query("#det-actividad")
        assert detalle.query_one("#dlg-detalle").outer_size.height <= size[1]


# ------------------------------------------------------------------ fechas naturales
async def _abrir_fecha(pilot, texto: str):
    """Abre el modal de vencimiento sobre la tarea seleccionada y escribe `texto`."""
    await pilot.press("d")
    await _esperar(pilot, lambda: isinstance(pilot.app.screen, FechaScreen))
    pilot.app.screen.query_one("#fecha-input", Input).value = texto
    return pilot.app.screen


@pytest.mark.parametrize(
    ("escrito", "dias"),
    [("+10d", 10), ("tomorrow", 1), ("today", 0), ("tom", 1)],
)
async def test_el_modal_de_fecha_resuelve_lo_que_se_escribe_en_ingles(escrito, dias):
    """Lo que sale del modal es SIEMPRE ISO: `interpretar_fecha` traduce, la API ni se
    entera de que existen `+10d` o `tom`."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        await _abrir_fecha(pilot, escrito)
        await pilot.press("ctrl+s")
        await _esperar(pilot, lambda: not isinstance(pilot.app.screen, FechaScreen))
        await pilot.pause(0.1)

        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence == date.today() + timedelta(days=dias)


async def test_el_modal_de_fecha_sigue_aceptando_el_formato_canonico():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tarea = screen.seleccionada
        await _abrir_fecha(pilot, "2027-03-09")
        await pilot.press("ctrl+s")
        await _esperar(pilot, lambda: not isinstance(pilot.app.screen, FechaScreen))
        await pilot.pause(0.1)

        actualizada = next(t for t in screen.tareas if t.item_id == tarea.item_id)
        assert actualizada.vence == date(2027, 3, 9)


async def test_una_fecha_que_no_se_entiende_avisa_y_no_cierra_el_modal():
    """Mismo camino de error que antes: el aviso ocupa la fila del hint y el modal se
    queda abierto para corregir, en vez de fechar cualquier cosa."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        modal = await _abrir_fecha(pilot, "asap")
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(pilot.app.screen, FechaScreen)
        error = modal.query_one("#fecha-error")
        assert error.display
        assert "invalid date" in error.content
        assert not modal.query_one("#fecha-hint").display


async def test_el_alta_resuelve_la_fecha_natural_antes_de_crear():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        creada = await _crear_desde_modal(
            pilot, screen, titulo="Call the client back", fecha="+2w"
        )

        assert creada.vence == date.today() + timedelta(weeks=2)


async def test_el_alta_con_una_fecha_ilegible_avisa_y_no_crea_nada():
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        modal.query_one("#nueva-titulo", Input).value = "Never created"
        modal.query_one("#nueva-fecha", Input).value = "el viernes"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(pilot.app.screen, NuevaScreen)
        assert "invalid date" in modal.query_one("#nueva-error").content
        assert not any(t.titulo == "Never created" for t in screen.tareas)


@pytest.mark.parametrize(
    ("selector", "tecla"), [("#fecha-input", "d"), ("#nueva-fecha", "n")]
)
async def test_los_dos_campos_de_fecha_ensenan_los_formatos_que_aceptan(selector, tecla):
    """El placeholder es la única documentación que se lee en el momento de escribir:
    si dijera solo `YYYY-MM-DD`, `fri` y `+10d` no existirían para el usuario."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        await _listo(pilot)
        await pilot.press(tecla)
        await pilot.pause()

        campo = pilot.app.screen.query_one(selector, Input)
        assert campo.placeholder.startswith(datos.EJEMPLOS_FECHA)
        # Y entra entero en su fila: un placeholder cortado enseñaría un formato falso.
        assert len(campo.placeholder) <= campo.content_size.width


# ------------------------------------------------------------------ filtro incremental
async def _filtrar(pilot, texto: str) -> None:
    """Abre el filtro con `/` y lo escribe tecla por tecla, como el usuario."""
    await pilot.press("slash")
    await pilot.pause()
    await pilot.press(*texto)
    await pilot.pause()


def _titulos(screen) -> list[str]:
    return [t.titulo for t in screen.visibles]


async def test_la_fila_del_filtro_solo_existe_mientras_se_usa():
    """En 80x15 la cabecera y el footer ya se llevan dos filas de quince: la del filtro
    se paga solo mientras está en uso."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(80, 15)) as pilot:
        screen = await _listo(pilot)
        entrada = screen.query_one("#filtro", Input)
        assert not entrada.display
        assert screen.query_one("#tabla", TablaTareas).has_focus

        await pilot.press("slash")
        await pilot.pause()
        assert entrada.display
        assert entrada.has_focus

        await pilot.press("escape")
        await pilot.pause()
        assert not entrada.display
        assert screen.query_one("#tabla", TablaTareas).has_focus


async def test_el_filtro_busca_en_el_titulo_y_en_el_repo():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)

        await _filtrar(pilot, "checkout")
        assert _titulos(screen) == ["Change the checkout payment flow"]

        await pilot.press("escape")
        await _filtrar(pilot, "vela")  # el nombre del repo, que la fila también muestra
        assert _titulos(screen) == ["Upload the new homepage photos", "Tweak the homepage copy"]


async def test_el_filtro_no_distingue_mayusculas():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "CHECKOUT")

        assert _titulos(screen) == ["Change the checkout payment flow"]


async def test_la_tabla_pinta_exactamente_lo_filtrado():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "homepage")
        tabla = screen.query_one("#tabla", TablaTareas)

        assert tabla.row_count == 2
        assert len(screen.visibles) == 2


async def test_el_filtro_se_combina_con_el_modo_repo():
    """AND, no OR: filtrar dentro de «this repo» busca solo ahí. La misma aguja fuera
    del modo repo sí encuentra la tarea del otro repo."""
    app = TareasApp(BackendDemo(repo_actual="vela/landing"))
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert screen.modo_repo

        await _filtrar(pilot, "renew")  # "Renew hosting…" vive en acme/web
        assert screen.visibles == []

        screen.action_toggle_repo()
        await pilot.pause()
        assert _titulos(screen) == ["Renew hosting and SSL certificate"]


async def test_mientras_se_escribe_las_letras_son_texto_y_no_atajos():
    """`n`, `d` y `x` abrirían modales desde la lista: con el foco en el filtro tienen
    que escribirse, o buscar «nueva» sería imposible."""
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "ndx")

        assert isinstance(pilot.app.screen, ListaScreen)
        assert screen.query_one("#filtro", Input).value == "ndx"


async def test_enter_deja_el_filtro_puesto_y_devuelve_los_atajos():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "homepage")
        await pilot.press("enter")
        await pilot.pause()

        assert screen.query_one("#tabla", TablaTareas).has_focus
        assert screen.query_one("#filtro", Input).display  # dice por qué la lista está corta
        assert len(screen.visibles) == 2

        await pilot.press("n")  # el atajo vuelve a disparar
        await pilot.pause()
        assert isinstance(pilot.app.screen, NuevaScreen)


async def test_enter_con_el_filtro_vacio_devuelve_su_fila_a_la_lista():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert not screen.query_one("#filtro", Input).display
        assert len(screen.visibles) == 7


async def test_esc_limpia_el_filtro_y_devuelve_la_lista_entera():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "homepage")
        assert len(screen.visibles) == 2

        await pilot.press("escape")
        await pilot.pause()

        assert screen.filtro == ""
        assert screen.query_one("#filtro", Input).value == ""
        assert len(screen.visibles) == 7


async def test_la_barra_vuelve_a_abrirse_para_editar_el_filtro_puesto():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "home")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        entrada = screen.query_one("#filtro", Input)
        assert entrada.has_focus
        assert entrada.value == "home"
        assert entrada.cursor_position == len("home")  # se sigue escribiendo, no de cero

        await pilot.press("p", "a", "g", "e")
        await pilot.pause()
        assert screen.filtro == "homepage"


async def test_la_cabecera_dice_cuanto_esconde_el_filtro():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "vela")
        texto = screen.query_one("#cab-titulo").content.plain

        assert "2/7 pending" in texto
        assert "1 overdue" in texto  # el conteo de vencidas también es del subconjunto


async def test_sin_coincidencias_lo_dice_el_area_vacia_de_siempre():
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "zzz")

        vacio = screen.query_one("#vacio")
        assert vacio.display
        assert not screen.query_one("#tabla", TablaTareas).display
        assert 'no tasks match "zzz"' in vacio.content.plain
        # Y la cabecera no miente: hay pendientes, solo que ninguna coincide.
        assert "no matches" in screen.query_one("#cab-titulo").content.plain


async def test_el_refresco_de_fondo_no_pisa_el_filtro():
    """El refresco cada 5 minutos repinta la lista entera: si no respetara el filtro, se
    lo llevaría por delante justo mientras el usuario busca."""
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "homepage")

        screen.refrescar()
        await _esperar(pilot, lambda: screen.ultimo_ok is not None)
        await pilot.pause(0.1)

        assert screen.filtro == "homepage"
        assert screen.query_one("#filtro", Input).display
        assert screen.query_one("#tabla", TablaTareas).row_count == 2


async def test_el_pintado_desde_cache_tampoco_pisa_el_filtro():
    """`_precargar` pinta la instantánea del disco antes de la red: pasa por el mismo
    `_pintar_tabla`, así que tiene que respetar el filtro igual que el refresco."""
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        await _filtrar(pilot, "homepage")

        screen._pintar_tabla()
        await pilot.pause()

        assert screen.query_one("#tabla", TablaTareas).row_count == 2


def _footer(screen) -> dict[str, str]:
    """Lo que el footer anuncia ahora mismo: tecla → descripción."""
    return {
        tecla: activo.binding.description
        for tecla, activo in screen.active_bindings.items()
        if activo.binding.show
    }


async def test_el_footer_no_promete_atajos_que_no_disparan_mientras_se_escribe():
    """La lección del #37: con el foco en el filtro, `n`/`d`/`x` son texto y no atajos,
    así que el footer no puede seguir anunciándolos. Y `esc` aparece solo cuando hay un
    filtro que limpiar."""
    app = TareasApp(BackendDemo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        assert _footer(screen) == {
            "enter": "view", "n": "new", "d": "date", "x": "close",
            "r": "refresh", "slash": "filter", "q": "quit",
        }

        await pilot.press("slash")
        await pilot.pause()
        assert _footer(screen) == {"enter": "apply", "escape": "clear"}

        # Con el filtro puesto y el foco de vuelta en la tabla, los atajos vuelven y
        # `esc` se queda a la vista: es la salida del filtro.
        await pilot.press("h", "o", "m", "e", "enter")
        await pilot.pause()
        assert _footer(screen)["enter"] == "view"
        assert _footer(screen)["escape"] == "clear"


# ------------------------------------------------------------------ comentarios legibles
class BackendComentariosColgados(BackendDemo):
    """Los comentarios no llegan hasta que el test suelta el evento.

    Es la ventana en la que el modal ya está abierto y `gh` todavía no contestó: lo
    que hay que poder mirar es que el detalle se lea igual mientras tanto.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.suelta = asyncio.Event()

    async def comentarios(self, tarea):
        await self.suelta.wait()
        return await super().comentarios(tarea)


class BackendSinComentarios(BackendDemo):
    """El issue existe pero `gh` no puede leer su conversación."""

    async def comentarios(self, tarea):
        raise ErrorGh("HTTP 502")


class BackendCuentaComentarios(BackendDemo):
    """Anota a qué issues se les pidieron los comentarios, y cuántas veces."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pedidos: list[int] = []

    async def comentarios(self, tarea):
        self.pedidos.append(tarea.numero)
        return await super().comentarios(tarea)


def _cabeceras(nodo) -> list[str]:
    """«autor · hace cuánto» de cada comentario montado bajo `nodo`."""
    return [
        str(bloque.query_one(".com-cabecera").content)
        for bloque in nodo.query(BloqueComentario)
    ]


def _estado_comentarios(nodo) -> str:
    """La línea de estado visible («loading comments…», «…», el fallo) o ""."""
    visibles = [e for e in nodo.query(".com-estado") if e.display]
    return str(visibles[0].content) if visibles else ""


async def test_el_detalle_se_abre_sin_esperar_a_los_comentarios():
    """La promesa de la app es no ir al navegador; abrir una tarea tampoco puede
    costar una ida y vuelta a GitHub."""
    backend = BackendComentariosColgados()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        modal = await _abrir_detalle(pilot, screen, 48)  # la demo le da 3 comentarios

        assert isinstance(modal, DetalleScreen)
        assert str(modal.query_one("#det-titulo").content).startswith("Change the checkout")
        assert _estado_comentarios(modal) == "loading comments…"
        assert not modal.query(BloqueComentario)

        backend.suelta.set()
        await _esperar(pilot, lambda: modal.query(BloqueComentario))
        await pilot.pause(0.1)

        assert _estado_comentarios(modal) == "", "la línea de carga se va con los comentarios"
        assert _cabeceras(modal) == ["elena-lumen · 1d ago", "pit · 5h ago", "elena-lumen · 40m ago"]


async def test_el_cuerpo_del_comentario_se_lee_como_markdown():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        modal = await _abrir_detalle(pilot, screen, 12)  # un comentario, con viñetas
        await _esperar(pilot, lambda: modal.query(BloqueComentario))

        from textual.widgets import Markdown

        bloque = modal.query_one(BloqueComentario)
        assert bloque.query(Markdown), "el cuerpo va en el widget Markdown, no en un Static"
        # Las viñetas del markdown se convierten en widgets propios: el texto crudo no.
        pintado = " ".join(str(hijo.content) for hijo in bloque.query(".com-cabecera"))
        assert "marco-acme" in pintado


async def test_una_tarea_sin_comentarios_no_gasta_ni_la_linea_de_carga():
    """Mismo criterio que la línea de actividad: en un pane de 15 filas, una línea
    en blanco es una línea menos de descripción."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(80, 15)) as pilot:
        screen = await _listo(pilot)
        modal = await _abrir_detalle(pilot, screen, 31)  # sin PR ni comentarios
        await pilot.pause(0.2)

        assert not modal.query("#det-comentarios")
        assert not modal.query(BloqueComentario)


async def test_si_gh_no_puede_leer_los_comentarios_el_detalle_lo_dice_y_sigue():
    """Una línea discreta, nunca un crash ni un modal a medias."""
    app = TareasApp(BackendSinComentarios())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        modal = await _abrir_detalle(pilot, screen, 48)
        await _esperar(pilot, lambda: _estado_comentarios(modal) == "couldn't load comments")

        assert _estado_comentarios(modal) == "couldn't load comments"
        assert not modal.query(BloqueComentario)
        # El resto del detalle sigue en pie y sus atajos también.
        assert modal.query_one("#det-cuerpo")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ListaScreen)


async def test_cerrar_el_detalle_antes_de_que_contesten_no_revienta():
    """Cancelar el worker no lo detiene en seco: la línea que sigue al `await` puede
    correr con el diálogo ya desmontado, y ahí pintar reventaba con NoMatches."""
    backend = BackendComentariosColgados()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        await _abrir_detalle(pilot, screen, 48)
        await pilot.press("escape")  # antes de que lleguen
        await pilot.pause()
        backend.suelta.set()  # la respuesta llega con el diálogo ya desmontado
        await pilot.pause(0.3)

        assert isinstance(pilot.app.screen, ListaScreen)
        assert not _avisos(pilot.app), "un error del worker habría salido como notificación"


async def test_los_comentarios_no_se_vuelven_a_pedir_para_la_misma_tarea():
    """La sesión los recuerda por (tarea, conteo): abrir dos veces el mismo detalle no
    gasta dos llamadas, y si el refresco trae más comentarios la clave cambia sola."""
    backend = BackendCuentaComentarios()
    app = TareasApp(backend)
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        for _ in range(2):
            modal = await _abrir_detalle(pilot, screen, 48)
            await _esperar(pilot, lambda vista=modal: vista.query(BloqueComentario))
            await pilot.press("escape")
            await pilot.pause()

        assert backend.pedidos == [48]


@pytest.mark.parametrize(
    ("segundos", "esperado"),
    [(0, "just now"), (59, "just now"), (60, "1m ago"), (3599, "59m ago"),
     (3600, "1h ago"), (86399, "23h ago"), (86400, "1d ago"), (86400 * 12, "12d ago"),
     (-30, "just now")],
)
async def test_la_antiguedad_sube_de_unidad_en_cada_techo(segundos, esperado):
    """«97m ago» obliga a dividir para entender que fue hace más de una hora; y un
    momento en el futuro (relojes desincronizados) no puede salir en negativo."""
    ahora = datetime(2026, 8, 5, 12, 0, 0)
    assert datos.hace_cuanto(ahora - timedelta(seconds=segundos), ahora) == esperado


# ------------------------------------------------------------------ panel de preview
def _panel(screen) -> PanelPreview | None:
    paneles = screen.query(PanelPreview)
    return paneles.first(PanelPreview) if paneles else None


def _preview_visible(screen) -> bool:
    panel = _panel(screen)
    return panel is not None and panel.display


async def _con_panel(pilot):
    """Espera a que el panel esté montado, visible y con la tarea del cursor pintada."""
    screen = await _listo(pilot)
    await _esperar(pilot, lambda: _preview_visible(screen))
    await pilot.pause(0.1)
    return screen


def _titulo_preview(screen) -> str:
    return screen.query_one("#prev-titulo").content.plain


@pytest.mark.parametrize(
    ("ancho", "visible"), [(80, False), (UMBRAL_PREVIEW - 1, False), (UMBRAL_PREVIEW, True)]
)
async def test_el_panel_solo_aparece_desde_el_umbral(ancho, visible):
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(ancho, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.pause(0.2)

        assert _preview_visible(screen) is visible


async def test_bajo_el_umbral_la_tabla_se_queda_con_el_pane_entero():
    """El caso de referencia no paga nada por una función que no se muestra: ni el
    ancho, ni el widget, ni el markdown-it que arrastra su Markdown."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(80, 15)) as pilot:
        screen = await _listo(pilot)
        await pilot.pause(0.2)
        tabla = screen.query_one("#tabla", TablaTareas)

        assert _panel(screen) is None, "bajo el umbral el panel ni se monta"
        assert tabla.region.width == 80
        assert tabla.columns[list(tabla.columns)[3]].width == 52


async def test_el_panel_le_deja_a_la_tabla_lo_que_no_usa():
    """El reparto es fijo: el panel se lleva ANCHO_PREVIEW y la tabla recalcula sus
    columnas con lo que queda, no con el pane de antes de repartir."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 24)) as pilot:
        screen = await _con_panel(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)

        assert _panel(screen).region.width == ANCHO_PREVIEW
        assert tabla.region.width == 140 - ANCHO_PREVIEW
        # 9 del vencimiento + 1 de ↻ + 10 de repo#N + 8 de padding = 28 fijas
        assert tabla.columns[list(tabla.columns)[3]].width == 140 - ANCHO_PREVIEW - 28


async def test_el_panel_muestra_la_tarea_seleccionada_entera():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        tabla.cursor_coordinate = Coordinate(_fila_de(screen, 48), 0)
        await pilot.pause(0.1)
        tarea = screen.seleccionada

        assert _titulo_preview(screen) == tarea.titulo
        meta = screen.query_one("#prev-meta").content.plain
        assert tarea.cliente in meta and "due today" in meta
        actividad = screen.query_one("#prev-actividad")
        assert actividad.display
        assert "PR #112 · open · CI failing" in str(actividad.content)


async def test_el_panel_sigue_al_cursor():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        vistos = []
        for fila in range(len(screen.visibles)):
            tabla.cursor_coordinate = Coordinate(fila, 0)
            await pilot.pause(0.1)
            vistos.append(_titulo_preview(screen))

        assert vistos == [t.titulo for t in screen.visibles]


async def test_el_panel_aparece_y_desaparece_en_vivo_al_cambiar_el_ancho():
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _listo(pilot)
        await pilot.pause(0.2)
        assert not _preview_visible(screen)

        await pilot.resize_terminal(140, 24)
        await pilot.pause(0.4)
        assert _preview_visible(screen)
        assert screen.query_one("#tabla", TablaTareas).region.width == 140 - ANCHO_PREVIEW

        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.4)
        assert not _preview_visible(screen)
        assert screen.query_one("#tabla", TablaTareas).region.width == 80


async def test_el_foco_nunca_va_al_panel():
    """Es solo lectura: la tabla manda, y un tab que se llevara el foco al panel
    dejaría j/k sin efecto."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        panel = _panel(screen)

        assert not [w for w in panel.query("*") if w.focusable]
        for _ in range(4):
            await pilot.press("tab")
            await pilot.pause()
            assert screen.query_one("#tabla", TablaTareas).has_focus


async def test_enter_sigue_abriendo_el_modal_con_el_panel_a_la_vista():
    """El panel evita abrir el detalle para MIRAR; el modal sigue siendo la vista
    completa y donde viven los botones de acción."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 30)) as pilot:
        await _con_panel(pilot)  # el panel ya está a la vista cuando llega el enter
        await pilot.press("enter")
        await _esperar(pilot, lambda: isinstance(pilot.app.screen, DetalleScreen))

        assert isinstance(pilot.app.screen, DetalleScreen)
        assert pilot.app.screen.query("#det-cerrar")


async def test_el_panel_convive_con_el_filtro(monkeypatch):
    """Con el filtro puesto la selección es la del subconjunto: el panel tiene que
    hablar de lo que se está viendo, no de la lista entera."""
    monkeypatch.setattr("tareas_tui.app.ESPERA_COMENTARIOS", 0.0)
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        await _filtrar(pilot, "checkout")
        await pilot.pause(0.2)

        assert _titulos(screen) == ["Change the checkout payment flow"]
        assert _titulo_preview(screen) == "Change the checkout payment flow"
        await _esperar(pilot, lambda: len(screen.query(BloqueComentario)) == 3)
        assert len(screen.query(BloqueComentario)) == 3


async def test_sin_tareas_a_la_vista_el_panel_se_esconde():
    app = TareasApp(vacio_demo())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _listo(pilot)
        await pilot.pause(0.3)

        assert not _preview_visible(screen)
        assert screen.query_one("#vacio").display


async def test_el_panel_trae_los_comentarios_de_la_seleccionada(monkeypatch):
    monkeypatch.setattr("tareas_tui.app.ESPERA_COMENTARIOS", 0.0)
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)

        tabla.cursor_coordinate = Coordinate(_fila_de(screen, 48), 0)
        await _esperar(pilot, lambda: len(screen.query(BloqueComentario)) == 3)
        assert _cabeceras(screen)[0].startswith("elena-lumen")

        # Otra tarea, otra conversación: no puede quedar ni un bloque de la anterior.
        tabla.cursor_coordinate = Coordinate(_fila_de(screen, 12), 0)
        await _esperar(pilot, lambda: len(screen.query(BloqueComentario)) == 1)
        assert _cabeceras(screen)[0].startswith("marco-acme")

        # Y una sin conversación deja el pie limpio, sin línea de estado.
        tabla.cursor_coordinate = Coordinate(_fila_de(screen, 31), 0)
        await _esperar(pilot, lambda: not screen.query(BloqueComentario))
        assert _estado_comentarios(screen.query_one("#prev-comentarios")) == ""


async def test_recorrer_la_lista_rapido_no_pide_una_conversacion_por_fila():
    """El debounce con el worker `exclusive`: bajar la lista con `j` puesto genera una
    selección por tecla, y sin esto cada una era una llamada a `gh`."""
    backend = BackendCuentaComentarios()
    app = TareasApp(backend)
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        for _ in range(len(screen.visibles) - 1):
            await pilot.press("j")  # sin pausas: el cursor nunca se queda quieto
        await pilot.pause(ESPERA_COMENTARIOS * 3)

        assert len(backend.pedidos) <= 1, backend.pedidos

        # Quedándose quieto sobre una con conversación, sí la pide (una sola vez).
        screen.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(
            _fila_de(screen, 48), 0
        )
        await _esperar(pilot, lambda: 48 in backend.pedidos)
        await pilot.pause(0.2)
        assert backend.pedidos.count(48) == 1


async def test_si_gh_falla_el_panel_lo_dice_en_una_linea(monkeypatch):
    monkeypatch.setattr("tareas_tui.app.ESPERA_COMENTARIOS", 0.0)
    app = TareasApp(BackendSinComentarios())
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        screen.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(
            _fila_de(screen, 48), 0
        )
        pie = screen.query_one("#prev-comentarios")
        await _esperar(pilot, lambda: _estado_comentarios(pie) == "couldn't load comments")

        assert _estado_comentarios(pie) == "couldn't load comments"
        assert _titulo_preview(screen) == "Change the checkout payment flow", (
            "lo local se sigue viendo aunque la red falle"
        )


async def test_el_refresco_periodico_no_borra_lo_que_el_panel_muestra(monkeypatch):
    """El panel convive con el refresco de fondo: repintar la tabla no puede dejarlo
    en blanco ni remontar los comentarios que ya estaban a la vista."""
    monkeypatch.setattr("tareas_tui.app.ESPERA_COMENTARIOS", 0.0)
    backend = BackendCuentaComentarios()
    app = TareasApp(backend)
    async with app.run_test(size=(140, 30)) as pilot:
        screen = await _con_panel(pilot)
        screen.query_one("#tabla", TablaTareas).cursor_coordinate = Coordinate(
            _fila_de(screen, 48), 0
        )
        await _esperar(pilot, lambda: len(screen.query(BloqueComentario)) == 3)

        screen.refrescar()
        await _esperar(pilot, lambda: screen.ultimo_ok is not None)
        await pilot.pause(0.3)

        assert _titulo_preview(screen) == "Change the checkout payment flow"
        assert len(screen.query(BloqueComentario)) == 3
        assert backend.pedidos == [48], "sin conversación nueva, no se vuelve a preguntar"
