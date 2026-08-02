"""Smoke test con el Pilot de textual, sobre `BackendDemo` (sin tocar GitHub).

Cubre el modo contextual por repo: detección, filtro, toggle (tecla y clic) y el
modal «nueva» sin picker cuando hay un repo preseleccionado.
"""

from __future__ import annotations

import pytest

from tareas_tui.app import ListaScreen, NuevaScreen, TareasApp
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
        assert screen.query_one("#cab-titulo").content.plain.startswith("tareas de clientes")


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
        assert "sin pendientes en vela/landing" in texto
        assert "todas" in texto


async def test_vacio_en_modo_todas_sin_cambios():
    app = TareasApp(vacio_demo())
    async with app.run_test() as pilot:
        screen = await _listo(pilot)
        texto = screen.query_one("#vacio").content.plain
        assert "sin tareas pendientes" in texto


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
