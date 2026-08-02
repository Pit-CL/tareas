"""Smoke test con el Pilot de textual, sobre `BackendDemo` (sin tocar GitHub).

Cubre el modo contextual por repo: detección, filtro, toggle (tecla y clic), el
modal «nueva» sin picker cuando hay un repo preseleccionado, y los atajos de
teclado: quick-picks numéricos, confirmación y/n, scroll j/k del detalle, flujo
enter sin tab en «nueva», g/G en la lista y el contador de vencidas del header.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from textual.widgets import Input

from tareas_tui.app import (
    ConfirmaScreen,
    DetalleScreen,
    FechaScreen,
    ListaScreen,
    NuevaScreen,
    TareasApp,
)
from tareas_tui.datos import BackendDemo, vacio_demo

pytestmark = pytest.mark.asyncio


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
@pytest.mark.parametrize("size", [(80, 15), (100, 20), (160, 45)])
async def test_cabecera_no_desborda_con_repo_largo(size):
    # Deliberadamente más largo que cualquiera de los anchos probados, incluido 160.
    repo_largo = "organizacion-" + "x" * 90 + "/repositorio-" + "y" * 90
    app = TareasApp(BackendDemo(repo_actual=repo_largo))
    async with app.run_test(size=size) as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#cab-titulo").content.plain
        assert len(texto) <= size[0]
        assert "…" in texto  # tan largo que siempre debe truncarse


@pytest.mark.parametrize("size", [(80, 15), (100, 20), (160, 45)])
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
        await pilot.press("enter")
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
