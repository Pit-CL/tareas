"""`tareas add`: el alta headless que un script o un agente invoca sin abrir la TUI.

Reusa el mismo `gh` de mentira que `test_backend.py` -acá lo que cambia es la capa de
arriba (argparse, resolución de repo, formato de salida), no las mutaciones GraphQL,
que ya están cubiertas ahí. Todo pasa por `entrada._cmd_add` salvo un caso que ejercita
el ruteo real de `main()` para probar que `argv[0] == "add"` no pasa por la guarda de
TTY (headless: sin terminal, no puede haberla).
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import gh_falso
import pytest
from gh_falso import GhFalso

from tareas_tui import __main__ as entrada
from tareas_tui import config as mod_config
from tareas_tui.config import Config
from tareas_tui.datos import ErrorGh

CONFIG = Config(
    owner="pit",
    project="1",
    campo_fecha="Due date",
    estado_hecho="Done",
    project_id="PVT_test",
    campo_fecha_id="PVTF_test",
    project_title="Client Tasks",
)


def _instalar(monkeypatch, falso: GhFalso, *, gh_disponible: bool = True) -> None:
    """Deja `add` listo para correr contra un Project de mentira, sin red ni disco real."""
    monkeypatch.setattr(entrada.shutil, "which", lambda _: "/usr/bin/gh" if gh_disponible else None)
    monkeypatch.setattr(mod_config, "cargar", lambda *a, **k: CONFIG)
    import tareas_tui.datos as mod_datos

    monkeypatch.setattr(mod_datos, "gh", falso)


# ------------------------------------------------------------------ camino feliz
def test_add_crea_la_tarea_y_muestra_referencia_y_url(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Renew SSL", "--repo", "pit/web", "--due", "2026-09-01"])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert salida == "created web#7 · due 2026-09-01 · https://github.com/pit/web/issues/7\n"
    assert falso.variables_de("FecharItem")["valor"] == "2026-09-01"


def test_add_sin_fecha_no_agrega_segmento_due(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web"])

    assert codigo == 0
    assert capsys.readouterr().out == "created web#7 · https://github.com/pit/web/issues/7\n"
    assert falso.veces("FecharItem") == 0


def test_add_acepta_fecha_dd_mm_yyyy(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web", "--due", "10-09-2026"])

    assert codigo == 0
    assert "due 2026-09-10" in capsys.readouterr().out
    assert falso.variables_de("FecharItem")["valor"] == "2026-09-10"


def test_add_repo_corto_usa_el_owner_de_la_config(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something", "--repo", "web"])

    assert codigo == 0
    assert falso.variables_de("IdDeRepo") == {"owner": "pit", "nombre": "web"}


def test_add_sin_repo_detecta_el_del_cwd(monkeypatch, capsys):
    falso = GhFalso(
        {
            "repo view": "acme/site\n",
            "CrearIssue": gh_falso.issue_creado(1, repo="acme/site"),
        }
    )
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something"])

    assert codigo == 0
    assert "https://github.com/acme/site/issues/1" in capsys.readouterr().out
    assert falso.variables_de("IdDeRepo") == {"owner": "acme", "nombre": "site"}


def test_add_el_titulo_se_recorta_de_espacios(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["  Renew SSL  ", "--repo", "pit/web"])

    assert codigo == 0
    assert falso.variables_de("CrearIssue")["titulo"] == "Renew SSL"


def test_add_notas_desde_stdin(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)
    monkeypatch.setattr(entrada.sys, "stdin", io.StringIO("## Spec\n\nline one\nline two\n"))

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web", "--notes", "-"])

    assert codigo == 0
    assert falso.variables_de("CrearIssue")["cuerpo"] == "## Spec\n\nline one\nline two"


# ------------------------------------------------------------------ errores
def test_add_sin_repo_detectable_pide_pass_repo(monkeypatch, capsys):
    falso = GhFalso()  # "repo view" por defecto es "" -> sin repo detectado
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something"])

    assert codigo == 2
    assert "--repo" in capsys.readouterr().err
    assert falso.veces("CrearIssue") == 0  # no se creó nada a medias


def test_add_fecha_invalida_sale_con_2_sin_tocar_github(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    with pytest.raises(SystemExit) as salida:
        entrada._cmd_add(["Something", "--repo", "pit/web", "--due", "31-02-2026"])

    assert salida.value.code == 2
    assert "invalid date" in capsys.readouterr().err
    assert falso.usados == []  # argparse cortó antes de hablarle a `gh`


def test_add_titulo_vacio_o_solo_espacios_sale_con_2_sin_tocar_github(monkeypatch, capsys):
    # Mismo criterio que `NuevaScreen._crear` ("title is required"): una plantilla mal
    # armada no debe crear un issue en blanco en GitHub.
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["   ", "--repo", "pit/web"])

    assert codigo == 2
    assert "title is required" in capsys.readouterr().err
    assert falso.usados == []  # se corta antes de hablarle a `gh`


def test_add_efecto_parcial_se_reporta_con_la_url_y_sale_con_2(monkeypatch, capsys):
    # Mismo fallo que test_backend.py: el issue se crea pero no se agrega al board.
    falso = GhFalso({"AgregarItem": ErrorGh("HTTP 502")})
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web"])

    assert codigo == 2
    mensaje = capsys.readouterr().err
    assert "wasn't added to the board" in mensaje
    assert "https://github.com/pit/web/issues/7" in mensaje
    assert "couldn't create" not in mensaje


def test_add_sin_gh_instalado_sale_con_2(monkeypatch, capsys):
    _instalar(monkeypatch, GhFalso(), gh_disponible=False)

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web"])

    assert codigo == 2
    assert "GitHub CLI" in capsys.readouterr().err


# ------------------------------------------------------------------ ruteo desde main()
def test_main_rutea_add_sin_pedir_terminal(monkeypatch, capsys):
    """Headless de verdad: `tareas add …` no puede pasar por la guarda que exige
    terminal interactiva (esa guarda es para la TUI). `capsys` ya simula un
    stdin/stdout sin tty, así que si `main()` llegara a evaluarla fallaría con
    "needs an interactive terminal" en vez de crear la tarea."""
    falso = GhFalso()
    _instalar(monkeypatch, falso)
    monkeypatch.setattr(entrada.sys, "argv", ["tareas", "add", "Something", "--repo", "pit/web"])

    assert entrada.main() == 0
    assert "interactive terminal" not in capsys.readouterr().err


# ------------------------------------------------------------------ fechas naturales
@pytest.mark.parametrize(
    ("escrito", "dias"), [("tomorrow", 1), ("tom", 1), ("today", 0), ("+10d", 10)]
)
def test_add_acepta_las_mismas_fechas_que_la_tui(monkeypatch, capsys, escrito, dias):
    """Un agente que crea tareas no debería tener que calcular la fecha: `--due +10d`
    resuelve lo mismo que escribirlo en el modal (`datos.interpretar_fecha`)."""
    falso = GhFalso()
    _instalar(monkeypatch, falso)
    esperada = (date.today() + timedelta(days=dias)).isoformat()

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web", "--due", escrito])

    assert codigo == 0
    assert falso.variables_de("FecharItem")["valor"] == esperada
    assert f"due {esperada}" in capsys.readouterr().out


def test_add_acepta_un_dia_de_la_semana(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    codigo = entrada._cmd_add(["Something", "--repo", "pit/web", "--due", "fri"])

    assert codigo == 0
    guardada = date.fromisoformat(falso.variables_de("FecharItem")["valor"])
    assert guardada.weekday() == 4  # viernes
    assert 1 <= (guardada - date.today()).days <= 7  # el próximo, nunca hoy


def test_add_sigue_aceptando_los_dos_formatos_numericos(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    assert entrada._cmd_add(["A", "--repo", "pit/web", "--due", "2026-09-01"]) == 0
    assert falso.variables_de("FecharItem")["valor"] == "2026-09-01"
    assert entrada._cmd_add(["B", "--repo", "pit/web", "--due", "01-09-2026"]) == 0
    assert falso.variables_de("FecharItem")["valor"] == "2026-09-01"


def test_add_fecha_en_ingles_que_no_se_entiende_sale_con_2_sin_tocar_github(monkeypatch, capsys):
    falso = GhFalso()
    _instalar(monkeypatch, falso)

    with pytest.raises(SystemExit) as salida:
        entrada._cmd_add(["Something", "--repo", "pit/web", "--due", "next friday"])

    assert salida.value.code == 2
    error = capsys.readouterr().err
    assert "invalid date" in error
    assert "+10d" in error  # el mensaje enseña lo que sí entra
    assert falso.usados == []
