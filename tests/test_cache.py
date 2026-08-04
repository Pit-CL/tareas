"""Caché en disco: lo que permite pintar la lista sin esperar a la red.

Nada de esto toca GitHub ni el `~/.config` real (ver `conftest.py`): la corrutina
`gh` del módulo se sustituye y `XDG_CONFIG_HOME` apunta a un temporal por test.

Lo que se protege acá es que el caché sea *desechable*: si el archivo no está, está
corrupto o es de otra versión, la app tiene que comportarse exactamente como antes
de que existiera, nunca reventar.
"""

from __future__ import annotations

import json
from datetime import date

import gh_falso
import pytest
from gh_falso import GhFalso

from tareas_tui import cache, datos
from tareas_tui.config import Config
from tareas_tui.datos import Backend, BackendDemo

pytestmark = pytest.mark.asyncio

CONFIG = Config(
    owner="pit",
    project="1",
    campo_fecha="Due date",
    estado_hecho="Done",
    project_id="PVT_test",
    campo_fecha_id="PVTF_test",
    project_title="Client Tasks",
)

OTRO_PROJECT = Config(**{**CONFIG.__dict__, "project": "2"})


def _gh_items(cuantos: int = 2) -> GhFalso:
    """`gh` de mentira cuyo Project devuelve `cuantos` pendientes."""
    return GhFalso(
        {
            "ItemsDelProject": gh_falso.pagina(
                [gh_falso.item(i, cuerpo="notes" if i else "") for i in range(cuantos)]
            )
        }
    )


# ------------------------------------------------------------------ ida y vuelta
async def test_listar_deja_la_lista_en_disco_y_se_relee_identica(monkeypatch):
    """El arranque siguiente pinta esto sin esperar el `gh project item-list`."""
    monkeypatch.setattr(datos, "gh", _gh_items(2))
    tareas = await Backend(CONFIG).listar()

    guardado = Backend(CONFIG).instantanea()  # otro proceso, mismo Project
    assert guardado.tareas == tareas
    assert guardado.momento is not None


async def test_sin_caché_la_instantanea_no_trae_tareas():
    """`None` (y no lista vacía) es lo que deja la pantalla en «loading tasks…»."""
    assert Backend(CONFIG).instantanea().tareas is None


async def test_un_project_no_ve_el_cache_de_otro(monkeypatch):
    monkeypatch.setattr(datos, "gh", _gh_items(2))
    await Backend(CONFIG).listar()

    assert Backend(OTRO_PROJECT).instantanea().tareas is None


async def test_un_project_sin_pendientes_se_cachea_como_lista_vacia(monkeypatch):
    """Vacío es un dato legítimo: hay que pintarlo, no confundirlo con «no hay caché»."""
    monkeypatch.setattr(datos, "gh", _gh_items(0))
    await Backend(CONFIG).listar()

    assert Backend(CONFIG).instantanea().tareas == []


async def test_los_repos_tambien_se_cachean(monkeypatch):
    listado = json.dumps([{"nameWithOwner": "pit/web"}, {"nameWithOwner": "pit/api"}])
    monkeypatch.setattr(datos, "gh", GhFalso({"repo list": listado}))
    await Backend(CONFIG).repos()

    assert Backend(CONFIG).instantanea().repos == ["pit/api", "pit/web"]


async def test_el_repo_del_cwd_se_cachea_por_directorio(monkeypatch, tmp_path):
    monkeypatch.setattr(datos, "gh", GhFalso({"repo view": "pit/web\n"}))
    monkeypatch.chdir(tmp_path)
    assert await Backend(CONFIG).repo_actual() == "pit/web"
    assert Backend(CONFIG).instantanea().repo_actual == "pit/web"

    # Desde otro directorio ese repo no aplica: el modo repo no debe heredarse.
    otro = tmp_path / "otro"
    otro.mkdir()
    monkeypatch.chdir(otro)
    assert Backend(CONFIG).instantanea().repo_actual is None


async def test_estar_fuera_de_un_repo_no_se_cachea(monkeypatch, tmp_path):
    """Sin repo no hay filtro que aplicar: recordarlo no ahorraría nada."""
    monkeypatch.setattr(
        datos, "gh", GhFalso({"repo view": datos.ErrorGh("not a git repository")})
    )
    monkeypatch.chdir(tmp_path)
    assert await Backend(CONFIG).repo_actual() is None
    assert Backend(CONFIG).instantanea().repo_actual is None


# ------------------------------------------------------------------ robustez
async def test_un_cache_corrupto_se_ignora_en_vez_de_tumbar_la_app(monkeypatch):
    cache.ruta().parent.mkdir(parents=True, exist_ok=True)
    cache.ruta().write_text("{ esto no es json", "utf-8")

    assert Backend(CONFIG).instantanea().tareas is None

    # y la siguiente lectura buena lo deja sano otra vez
    monkeypatch.setattr(datos, "gh", _gh_items(1))
    await Backend(CONFIG).listar()
    assert len(Backend(CONFIG).instantanea().tareas) == 1


async def test_un_cache_de_otra_version_se_descarta_entero(monkeypatch):
    cache.ruta().parent.mkdir(parents=True, exist_ok=True)
    cache.ruta().write_text(
        json.dumps({"version": cache.VERSION + 1, "pit/1": {"tareas": [{"item_id": "x"}]}}),
        "utf-8",
    )

    assert Backend(CONFIG).instantanea().tareas is None


async def test_una_tarea_ilegible_no_se_lleva_puestas_a_las_demas():
    cache.ruta().parent.mkdir(parents=True, exist_ok=True)
    cache.escribir(
        "pit/1",
        {
            "tareas": [
                {"item_id": "ok", "repo": "pit/web", "numero": 1, "titulo": "Fine",
                 "vence": "2026-09-01"},
                {"item_id": "rota"},  # sin los campos obligatorios
                "ni siquiera un dict",
            ]
        },
    )

    tareas = Backend(CONFIG).instantanea().tareas
    assert [t.item_id for t in tareas] == ["ok"]
    assert tareas[0].vence == date(2026, 9, 1)


async def test_no_poder_escribir_el_cache_no_rompe_la_lectura(monkeypatch):
    """Disco lleno o directorio sin permiso: la app sigue, solo pierde el atajo."""
    def explotar(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(datos, "gh", _gh_items(2))
    monkeypatch.setattr(cache.Path, "write_text", explotar)

    assert len(await Backend(CONFIG).listar()) == 2


# ------------------------------------------------------------------ la demo no cachea
async def test_la_demo_no_lee_ni_escribe_el_cache(monkeypatch):
    monkeypatch.setattr(datos, "gh", _gh_items(2))
    await Backend(CONFIG).listar()  # deja algo guardado bajo esa clave

    demo = BackendDemo()
    await demo.listar()
    await demo.repos()
    assert demo.instantanea().tareas is None


# ------------------------------------------------------------------ alta optimista
async def test_crear_devuelve_la_tarea_para_no_tener_que_releer_el_project(monkeypatch):
    """Sin esto el alta terminaba en otra lectura entera solo para verla aparecer."""
    monkeypatch.setattr(
        datos,
        "gh",
        GhFalso(
            {
                "CrearIssue": gh_falso.issue_creado(42),
                "AgregarItem": gh_falso.item_agregado("PVTI_nueva"),
            }
        ),
    )
    creada = await Backend(CONFIG).crear("pit/web", "Something", "2026-09-01", "notes")

    assert creada.item_id == "PVTI_nueva"
    assert creada.numero == 42  # lo devuelve la propia mutación
    assert creada.repo == "pit/web"
    assert creada.titulo == "Something"
    assert creada.vence == date(2026, 9, 1)
    assert creada.cuerpo == "notes"


async def test_crear_una_repetitiva_devuelve_la_ocurrencia_con_su_marca(monkeypatch):
    monkeypatch.setattr(
        datos,
        "gh",
        GhFalso(
            {
                "CrearIssue": gh_falso.issue_creado(43),
                "AgregarItem": gh_falso.item_agregado("PVTI_otra"),
            }
        ),
    )
    vieja = datos.Tarea(
        item_id="PVTI_vieja",
        repo="pit/web",
        numero=3,
        titulo="Weekly report",
        url="https://github.com/pit/web/issues/3",
        cuerpo="send it",
        vence=date(2026, 6, 1),
        repeat="weekly",
    )
    siguiente = await Backend(CONFIG).repetir(vieja, date(2026, 6, 10))

    assert siguiente.repeat == "weekly"
    assert siguiente.cuerpo == "send it"  # el metadato no ensucia las notas
    assert siguiente.vence > date(2026, 6, 10)


async def test_numero_de_url_tolera_lo_que_no_sea_un_issue():
    assert datos.numero_de_url("https://github.com/pit/web/issues/7") == 7
    assert datos.numero_de_url("https://github.com/pit/web/issues/7/") == 7
    assert datos.numero_de_url("no-es-una-url") == 0


async def test_un_cwd_borrado_no_tumba_el_arranque(monkeypatch, tmp_path):
    """La app vive en un pane de larga vida: renombrar o borrar el directorio desde
    el que se lanzó deja a `os.getcwd()` levantando OSError, y el caché lo consulta
    en el arranque. Sin repo se cae en modo todas, igual que cuando falla `gh`."""
    efimero = tmp_path / "efimero"
    efimero.mkdir()
    monkeypatch.chdir(efimero)
    efimero.rmdir()  # el pane sigue abierto sobre un directorio que ya no existe

    assert datos._cwd() == ""
    assert Backend(CONFIG).instantanea().repo_actual is None


async def test_sin_cwd_el_repo_detectado_no_se_cachea(monkeypatch, tmp_path):
    efimero = tmp_path / "otro-efimero"
    efimero.mkdir()
    monkeypatch.setattr(datos, "gh", GhFalso({"repo view": "pit/web\n"}))
    monkeypatch.chdir(efimero)
    efimero.rmdir()

    assert await Backend(CONFIG).repo_actual() == "pit/web"  # el dato igual se devuelve
    assert cache.leer("pit/1").get("repo_actual") is None  # pero no se guarda bajo ""


async def test_el_node_id_del_issue_sobrevive_al_cache(monkeypatch):
    """Sin esto, reabrir la app degradaba el cierre al camino lento hasta el primer
    refresco."""
    monkeypatch.setattr(datos, "gh", _gh_items(1))
    await Backend(CONFIG).listar()

    guardadas = Backend(CONFIG).instantanea().tareas
    assert [t.issue_id for t in guardadas] == ["I_0"]


async def test_un_cache_sin_node_id_se_lee_igual():
    """Entradas escritas por una versión anterior: la tarea se lee, solo que su cierre
    cae al camino de siempre."""
    cache.escribir(
        "pit/1",
        {"tareas": [{"item_id": "ok", "repo": "pit/web", "numero": 1, "titulo": "Fine",
                     "vence": "2026-09-01"}]},
    )

    tareas = Backend(CONFIG).instantanea().tareas
    assert [t.issue_id for t in tareas] == [""]


# ------------------------------------------------------------------ PR vinculado
async def test_el_pr_y_los_comentarios_sobreviven_al_cache(monkeypatch):
    """Sin esto la primera pantalla tras reiniciar salía sin chips y estos aparecían
    solos un segundo después, que es justo el parpadeo que el caché viene a evitar."""
    monkeypatch.setattr(
        datos,
        "gh",
        GhFalso(
            {
                "ItemsDelProject": gh_falso.pagina(
                    [gh_falso.item(1, prs=[gh_falso.pr(45, ci="FAILURE")], comentarios=2)]
                )
            }
        ),
    )
    tareas = await Backend(CONFIG).listar()
    guardada = Backend(CONFIG).instantanea().tareas[0]  # otro proceso, mismo Project

    assert guardada == tareas[0]
    assert guardada.pr == datos.PrVinculado(numero=45, estado="open", ci="failure", mergeable=True)
    assert guardada.comentarios == 2


async def test_un_cache_sin_pr_se_lee_igual():
    """El de una versión anterior no trae la clave: la tarea se lee sin chip y el primer
    refresco lo pone, como con cualquier dato nuevo."""
    viejo = {
        "item_id": "PVTI_1",
        "repo": "pit/web",
        "numero": 1,
        "titulo": "Task 1",
        "vence": "2026-09-01",
    }
    cache.escribir("pit/1", {"tareas": [viejo]})
    guardada = Backend(CONFIG).instantanea().tareas[0]

    assert guardada.pr is None
    assert guardada.comentarios == 0


async def test_un_pr_corrupto_en_el_cache_no_tumba_la_tarea():
    cache.escribir(
        "pit/1",
        {
            "tareas": [
                {
                    "item_id": "PVTI_1",
                    "repo": "pit/web",
                    "numero": 1,
                    "titulo": "Task 1",
                    "vence": None,
                    "pr": {"numero": "no soy un número"},
                }
            ]
        },
    )
    guardada = Backend(CONFIG).instantanea().tareas[0]

    assert guardada.titulo == "Task 1"
    assert guardada.pr is None
