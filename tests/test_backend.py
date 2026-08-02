"""Capa `gh`: timeouts, subprocesos huérfanos, efectos parciales y el límite del Project.

Nada de esto toca GitHub. Los timeouts se prueban con un `gh` falso que solo duerme
—así se ve de verdad si el subproceso queda vivo— y el resto sustituyendo la corrutina
`gh` del módulo.

Los símbolos nuevos se leen por módulo (`datos.ErrorParcial`, `datos.TIMEOUT_GH`) a
propósito: así cada regresión falla en su propio test en vez de reventar la colección
del archivo entero.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from tareas_tui import datos
from tareas_tui.config import Config
from tareas_tui.datos import Backend, ErrorGh

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

def _gh_dormilon(tmp_path, monkeypatch, segundos: int):
    """Pone en el PATH un `gh` falso que anota su PID y se duerme.

    Es un script de Python y no un `sh -c sleep`: así el proceso que matamos es el
    mismo que arrancó `create_subprocess_exec`, sin un hijo que sobreviva y ensucie la
    medición. El PID va a un archivo (en vez de buscarlo con `pgrep`) para no depender
    de procps y para que la comprobación sea exacta.
    """
    pid = tmp_path / "pid"
    falso = tmp_path / "gh"
    falso.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        f"open({str(pid)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep({segundos})\n"
    )
    falso.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return pid


def _vivo(archivo_pid) -> bool:
    """True si el `gh` falso sigue existiendo. Un zombie también cuenta como vivo: si
    matamos sin cosechar, el proceso queda igual colgando del árbol."""
    try:
        pid = int(archivo_pid.read_text())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _esperar(condicion, intentos: int = 250) -> bool:
    for _ in range(intentos):
        if condicion():
            return True
        await asyncio.sleep(0.02)
    return condicion()


# ------------------------------------------------------------------ timeout y huérfanos
async def test_gh_corta_por_timeout_y_no_deja_el_proceso_vivo(tmp_path, monkeypatch):
    """Sin timeout, con la red muerta la app se quedaba en «loading tasks…» para siempre
    y `r` no ayudaba: `gh` nunca volvía. Ahora corta con un error acotado y mata el
    subproceso en vez de dejarlo colgado."""
    pid = _gh_dormilon(tmp_path, monkeypatch, 15)
    monkeypatch.setattr(datos, "TIMEOUT_GH", 0.4)

    inicio = time.monotonic()
    with pytest.raises(ErrorGh) as err:
        await datos.gh("item-list")
    transcurrido = time.monotonic() - inicio

    assert transcurrido < 5  # cortó por timeout, no esperó los 15s del falso
    assert "didn't respond" in str(err.value)
    assert await _esperar(lambda: not _vivo(pid))


async def test_cancelar_el_worker_mata_el_gh_en_curso(tmp_path, monkeypatch):
    """Cancelar el worker NO mataba el subproceso: cada ciclo de refresco (300s) con la
    red caída dejaba un `gh` huérfano corriendo para siempre."""
    pid = _gh_dormilon(tmp_path, monkeypatch, 15)

    tarea = asyncio.create_task(datos.gh("item-list"))
    assert await _esperar(lambda: _vivo(pid)), "el `gh` falso nunca arrancó"

    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert await _esperar(lambda: not _vivo(pid))


# ------------------------------------------------------------------ efectos parciales
def _falso_gh(fallar_en: tuple[str, ...], mensaje: str = "HTTP 502"):
    """`gh` de mentira que revienta solo en el subcomando indicado."""

    async def falso(*args: str) -> str:
        if args[: len(fallar_en)] == fallar_en:
            raise ErrorGh(mensaje)
        if args[:2] == ("issue", "create"):
            return "https://github.com/pit/web/issues/7\n"
        if args[:2] == ("project", "item-add"):
            return "PVTI_test\n"
        return ""

    return falso


async def test_crear_avisa_que_el_issue_ya_existe_si_falla_el_item_add(monkeypatch):
    """`issue create` + `item-add` no son atómicos: si falla el segundo, el issue YA
    existe. Decir "couldn't create the task" empuja al usuario a reintentar y duplicar."""
    monkeypatch.setattr(datos, "gh", _falso_gh(("project", "item-add")))

    with pytest.raises(datos.ErrorParcial) as err:
        await Backend(CONFIG).crear("pit/web", "Something", "2026-09-01")

    mensaje = str(err.value)
    assert "wasn't added to the board" in mensaje
    assert "https://github.com/pit/web/issues/7" in mensaje
    assert "HTTP 502" in mensaje
    assert "couldn't create" not in mensaje


async def test_crear_avisa_que_la_tarea_quedo_sin_fecha_si_falla_el_fechar(monkeypatch):
    monkeypatch.setattr(datos, "gh", _falso_gh(("project", "item-edit"), "HTTP 500"))

    with pytest.raises(datos.ErrorParcial) as err:
        await Backend(CONFIG).crear("pit/web", "Something", "2026-09-01")

    mensaje = str(err.value)
    assert "without a due date" in mensaje
    assert "HTTP 500" in mensaje


async def test_si_falla_la_creacion_del_issue_no_hay_efecto_parcial(monkeypatch):
    """El caso honesto de "no se creó nada" no debe disfrazarse de efecto parcial."""
    monkeypatch.setattr(datos, "gh", _falso_gh(("issue", "create"), "HTTP 401"))

    with pytest.raises(ErrorGh) as err:
        await Backend(CONFIG).crear("pit/web", "Something", None)

    assert not isinstance(err.value, datos.ErrorParcial)


async def test_repetir_propaga_el_parcial_en_vez_de_decir_que_no_creo_nada(monkeypatch):
    """Si falla el `fechar` final, la ocurrencia ya nació SIN fecha y la serie queda
    rota: el mensaje tiene que decir eso, no "couldn't create the next occurrence"."""
    from datetime import date

    monkeypatch.setattr(datos, "gh", _falso_gh(("project", "item-edit"), "HTTP 500"))
    tarea = datos.Tarea(
        item_id="PVTI_vieja",
        repo="pit/web",
        numero=3,
        titulo="Weekly report",
        url="https://github.com/pit/web/issues/3",
        cuerpo="notes",
        vence=date(2026, 6, 1),
        repeat="weekly",
    )

    with pytest.raises(datos.ErrorParcial) as err:
        await Backend(CONFIG).repetir(tarea, date(2026, 6, 10))

    assert "without a due date" in str(err.value)


# ------------------------------------------------------------------ límite del Project
def _project_json(cuantos: int, hechas: int = 0) -> str:
    items = []
    for indice in range(cuantos):
        items.append(
            {
                "id": f"PVTI_{indice}",
                "status": "Done" if indice < hechas else "Todo",
                "title": f"Task {indice}",
                "content": {
                    "type": "Issue",
                    "repository": "pit/web",
                    "number": indice,
                    "title": f"Task {indice}",
                    "url": f"https://github.com/pit/web/issues/{indice}",
                    "body": "",
                },
                "due date": "2026-09-01",
            }
        )
    return json.dumps({"items": items})


async def test_listar_marca_truncado_contando_antes_de_filtrar_las_hechas(monkeypatch):
    """El `--limit` lo aplica GitHub ANTES de que descartemos las Done, así que un
    Project con muchas hechas acumuladas puede dejar pendientes fuera sin avisar."""
    monkeypatch.setattr(datos, "LIMITE_ITEMS", 3)
    usados: list[tuple[str, ...]] = []

    async def falso(*args: str) -> str:
        usados.append(args)
        return _project_json(3, hechas=2)

    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    tareas = await backend.listar()

    assert len(tareas) == 1  # solo una pendiente queda visible…
    assert backend.truncado is True  # …pero la lectura tocó el techo
    assert "--limit" in usados[0] and "3" in usados[0]


async def test_listar_no_marca_truncado_cuando_sobra_margen(monkeypatch):
    monkeypatch.setattr(datos, "LIMITE_ITEMS", 3)

    async def falso(*args: str) -> str:
        return _project_json(2)

    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()

    assert backend.truncado is False


async def test_el_limite_por_defecto_deja_margen_de_sobra():
    assert datos.LIMITE_ITEMS >= 1000


# ------------------------------------------------------------------ campo renombrado
def _items_con_clave(clave: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "id": "PVTI_1",
                    "status": "Todo",
                    "content": {
                        "type": "Issue",
                        "repository": "pit/web",
                        "number": 1,
                        "title": "Renew SSL",
                        "url": "https://github.com/pit/web/issues/1",
                        "body": "",
                    },
                    clave: "2026-09-01",
                }
            ]
        }
    )


def _gh_campo(clave_items: str, campos: list[dict] | None = None, falla_field_list=False):
    """`gh` de mentira: los items traen la fecha bajo `clave_items` y el Project
    declara `campos` (lo que devolvería `gh project field-list`)."""
    usados: list[tuple[str, ...]] = []

    async def falso(*args: str) -> str:
        usados.append(args)
        if args[:2] == ("project", "item-list"):
            return _items_con_clave(clave_items)
        if args[:2] == ("project", "field-list"):
            if falla_field_list:
                raise ErrorGh("HTTP 502")
            return json.dumps({"fields": campos or []})
        return ""

    return falso, usados


async def test_renombrar_el_campo_de_fecha_deja_de_ser_silencioso(monkeypatch):
    """Antes: el campo se renombraba en GitHub, `valor_campo` no encontraba la clave y
    TODAS las tareas llegaban con vence=None sin un solo aviso. Ahora se desempata por
    id -lo único que un rename no cambia-, se leen las fechas igual y se avisa."""
    falso, usados = _gh_campo(
        "fecha vencimiento",
        campos=[{"id": "PVTF_test", "name": "Fecha vencimiento", "type": "ProjectV2Field"}],
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    tareas = await backend.listar()

    from datetime import date

    assert tareas[0].vence == date(2026, 9, 1), "la fecha se perdió pese a estar en GitHub"
    assert backend.aviso_campo is not None
    assert "renamed" in backend.aviso_campo
    assert '"Fecha vencimiento"' in backend.aviso_campo
    assert any(a[:2] == ("project", "field-list") for a in usados)


async def test_borrar_el_campo_de_fecha_avisa_en_vez_de_callar(monkeypatch):
    falso, _ = _gh_campo(
        "otra cosa",
        campos=[{"id": "PVTF_distinto", "name": "Sprint", "type": "ProjectV2Field"}],
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    tareas = await backend.listar()

    assert tareas[0].vence is None  # no hay nada que leer, pero ya no es en silencio
    assert backend.aviso_campo is not None
    assert "is gone" in backend.aviso_campo
    assert "Sprint" in backend.aviso_campo  # dice qué campos hay ahora


async def test_un_project_sin_vencimientos_puestos_no_dispara_ningun_aviso(monkeypatch):
    """El caso legítimo: el campo existe y se llama igual, solo que nadie lo usó."""
    falso, _ = _gh_campo(
        "otra cosa",
        campos=[{"id": "PVTF_test", "name": "Due date", "type": "ProjectV2Field"}],
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()

    assert backend.aviso_campo is None


async def test_si_no_se_puede_comprobar_el_campo_no_se_inventa_un_aviso(monkeypatch):
    falso, _ = _gh_campo("otra cosa", falla_field_list=True)
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()

    assert backend.aviso_campo is None


async def test_el_campo_se_sondea_una_sola_vez_por_proceso(monkeypatch):
    """El refresco corre cada 5 minutos: repetir el `field-list` sería una llamada de
    red periódica para volver a saber lo mismo."""
    falso, usados = _gh_campo(
        "otra cosa",
        campos=[{"id": "PVTF_test", "name": "Due date", "type": "ProjectV2Field"}],
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()
    await backend.listar()
    await backend.listar()

    assert sum(1 for a in usados if a[:2] == ("project", "field-list")) == 1


async def test_el_camino_normal_no_pregunta_por_los_campos(monkeypatch):
    """Con la fecha presente en los items no hay nada que sospechar: cero red extra."""
    falso, usados = _gh_campo("due date", campos=[])
    monkeypatch.setattr(datos, "gh", falso)
    await Backend(CONFIG).listar()

    assert not any(a[:2] == ("project", "field-list") for a in usados)
