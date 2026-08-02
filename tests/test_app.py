"""Smoke test con el Pilot de textual, sobre `BackendDemo` (sin tocar GitHub).

Cubre el modo contextual por repo, los atajos de teclado, el contraste de la fila
bajo el cursor, la respiración adaptativa de los modales, el campo de notas y el
ciclo de vida de las tareas repetitivas (crear con ↻, cerrar y que nazca la
siguiente ocurrencia).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from textual.coordinate import Coordinate
from textual.widgets import Button, Input

from tareas_tui.app import (
    ConfirmaScreen,
    DetalleScreen,
    FechaScreen,
    ListaScreen,
    NuevaScreen,
    TablaTareas,
    TareasApp,
)
from tareas_tui.datos import BackendDemo, ErrorGh, proxima_fecha, vacio_demo

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
@pytest.mark.parametrize("tecla,contenedor", [("n", "#dlg-nueva"), ("d", "#dlg-fecha")])
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
    """El bug que motivó `TablaTareas`: la columna de repo va en `dim` y sobre el fondo
    ámbar del cursor se difuminaba hasta quedar ilegible. Bajo el cursor no debe
    sobrevivir ningún `dim`, y todo el texto de la fila comparte el color del cursor."""
    app = TareasApp(BackendDemo())
    async with app.run_test(size=(110, 24)) as pilot:
        screen = await _listo(pilot)
        tabla = screen.query_one("#tabla", TablaTareas)
        assert tabla.has_focus

        await pilot.press("j")  # fila 1: repetitiva, con marca ↻ y repo en dim
        await pilot.pause()
        assert tabla.cursor_row == 1

        color_cursor = tabla.get_component_rich_style("datatable--cursor").color
        bajo_cursor = [s for s in tabla.render_line(1) if s.text.strip()]
        assert bajo_cursor
        assert all(not s.style.dim for s in bajo_cursor)
        assert {s.style.color for s in bajo_cursor} == {color_cursor}

        # control: fuera del cursor el dim sigue vivo, que es lo que da la jerarquía
        otra_fila = [s for s in tabla.render_line(0) if s.text.strip()]
        assert any(s.style.dim for s in otra_fila)


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
