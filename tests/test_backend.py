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

import gh_falso
import pytest
from gh_falso import GhFalso

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
def _falso_gh(operacion: str, mensaje: str = "HTTP 502"):
    """`gh` de mentira que revienta solo en la operación indicada."""
    return GhFalso({operacion: ErrorGh(mensaje)})


async def test_crear_avisa_que_el_issue_ya_existe_si_falla_el_item_add(monkeypatch):
    """Crear el issue y agregarlo al board no son atómicos: si falla lo segundo, el
    issue YA existe. Decir "couldn't create the task" empuja a reintentar y duplicar."""
    monkeypatch.setattr(datos, "gh", _falso_gh("AgregarItem"))

    with pytest.raises(datos.ErrorParcial) as err:
        await Backend(CONFIG).crear("pit/web", "Something", "2026-09-01")

    mensaje = str(err.value)
    assert "wasn't added to the board" in mensaje
    assert "https://github.com/pit/web/issues/7" in mensaje
    assert "HTTP 502" in mensaje
    assert "couldn't create" not in mensaje


async def test_crear_avisa_que_la_tarea_quedo_sin_fecha_si_falla_el_fechar(monkeypatch):
    monkeypatch.setattr(datos, "gh", _falso_gh("FecharItem", "HTTP 500"))

    with pytest.raises(datos.ErrorParcial) as err:
        await Backend(CONFIG).crear("pit/web", "Something", "2026-09-01")

    mensaje = str(err.value)
    assert "without a due date" in mensaje
    assert "HTTP 500" in mensaje


async def test_si_falla_la_creacion_del_issue_no_hay_efecto_parcial(monkeypatch):
    """El caso honesto de "no se creó nada" no debe disfrazarse de efecto parcial."""
    monkeypatch.setattr(datos, "gh", _falso_gh("CrearIssue", "HTTP 401"))

    with pytest.raises(ErrorGh) as err:
        await Backend(CONFIG).crear("pit/web", "Something", None)

    assert not isinstance(err.value, datos.ErrorParcial)


async def test_repetir_propaga_el_parcial_en_vez_de_decir_que_no_creo_nada(monkeypatch):
    """Si falla el `fechar` final, la ocurrencia ya nació SIN fecha y la serie queda
    rota: el mensaje tiene que decir eso, no "couldn't create the next occurrence"."""
    from datetime import date

    monkeypatch.setattr(datos, "gh", _falso_gh("FecharItem", "HTTP 500"))
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
def _pagina(cuantos: int, hechas: int = 0, cursor: str | None = None) -> str:
    return gh_falso.pagina(
        [
            gh_falso.item(indice, estado="Done" if indice < hechas else "Todo")
            for indice in range(cuantos)
        ],
        cursor=cursor,
    )


async def test_listar_marca_truncado_contando_antes_de_filtrar_las_hechas(monkeypatch):
    """El techo se aplica ANTES de que descartemos las Done, así que un Project con
    muchas hechas acumuladas puede dejar pendientes fuera sin avisar."""
    monkeypatch.setattr(datos, "LIMITE_ITEMS", 3)
    falso = GhFalso({"ItemsDelProject": _pagina(3, hechas=2)})
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    tareas = await backend.listar()

    assert len(tareas) == 1  # solo una pendiente queda visible…
    assert backend.truncado is True  # …pero la lectura tocó el techo


async def test_listar_no_marca_truncado_cuando_sobra_margen(monkeypatch):
    monkeypatch.setattr(datos, "LIMITE_ITEMS", 3)
    monkeypatch.setattr(datos, "gh", GhFalso({"ItemsDelProject": _pagina(2)}))
    backend = Backend(CONFIG)
    await backend.listar()

    assert backend.truncado is False


async def test_el_limite_por_defecto_deja_margen_de_sobra():
    assert datos.LIMITE_ITEMS >= 1000


# ------------------------------------------------------------------ paginación
async def test_listar_sigue_las_paginas_hasta_que_github_dice_que_no_hay_mas(monkeypatch):
    """La API devuelve como mucho 100 items por página: sin seguir el cursor, un
    Project grande perdía en silencio todo lo que no entrara en la primera."""
    paginas = [_pagina(2, cursor="cursor-1"), _pagina(2)]

    def responder(variables):
        return paginas[1] if variables.get("cursor") else paginas[0]

    falso = GhFalso({"ItemsDelProject": responder})
    monkeypatch.setattr(datos, "gh", falso)
    tareas = await Backend(CONFIG).listar()

    assert len(tareas) == 4
    assert falso.veces("ItemsDelProject") == 2
    # La primera página va SIN `after`: "" no es un cursor válido para GitHub.
    assert "cursor" not in gh_falso.variables(falso.usados[0])
    assert gh_falso.variables(falso.usados[1])["cursor"] == "cursor-1"


async def test_listar_no_pide_mas_paginas_de_las_que_caben_en_el_limite(monkeypatch):
    """Un cursor que nunca termina (o un Project enorme) no puede dejar la app pidiendo
    páginas para siempre: el techo de `LIMITE_ITEMS` corta el bucle."""
    monkeypatch.setattr(datos, "LIMITE_ITEMS", 3)
    falso = GhFalso({"ItemsDelProject": _pagina(2, cursor="siempre-hay-mas")})
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()

    assert falso.veces("ItemsDelProject") == 2  # 2 + 2 ya pasó el techo de 3
    assert backend.truncado is True


async def test_una_pagina_sin_cursor_no_desata_otra_vuelta(monkeypatch):
    """`hasNextPage` en true pero sin `endCursor` sería un bucle infinito pidiendo
    siempre la misma página."""
    falso = GhFalso({"ItemsDelProject": gh_falso.pagina([gh_falso.item(0)], cursor="")})
    monkeypatch.setattr(datos, "gh", falso)
    await Backend(CONFIG).listar()

    assert falso.veces("ItemsDelProject") == 1


# ------------------------------------------------------------------ campo renombrado
def _gh_campo(campo_items: str, campos: list[dict] | None = None, falla_field_list=False):
    """`gh` de mentira: los items traen la fecha bajo el campo `campo_items` y el
    Project declara `campos` (lo que devolvería `gh project field-list`)."""
    return GhFalso(
        {
            "ItemsDelProject": gh_falso.pagina(
                [gh_falso.item(1, titulo="Renew SSL", campo_fecha=campo_items)]
            ),
            "project field-list": (
                ErrorGh("HTTP 502")
                if falla_field_list
                else json.dumps({"fields": campos or []})
            ),
        }
    )


async def test_renombrar_el_campo_de_fecha_deja_de_ser_silencioso(monkeypatch):
    """Antes: el campo se renombraba en GitHub, `valor_campo` no encontraba la clave y
    TODAS las tareas llegaban con vence=None sin un solo aviso. Ahora se desempata por
    id -lo único que un rename no cambia-, se leen las fechas igual y se avisa."""
    falso = _gh_campo(
        "Fecha vencimiento",
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
    assert falso.veces("project field-list") == 1


async def test_borrar_el_campo_de_fecha_avisa_en_vez_de_callar(monkeypatch):
    falso = _gh_campo(
        "Otra cosa",
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
    falso = _gh_campo(
        "Otra cosa",
        campos=[{"id": "PVTF_test", "name": "Due date", "type": "ProjectV2Field"}],
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()

    assert backend.aviso_campo is None


async def test_si_no_se_puede_comprobar_el_campo_no_se_inventa_un_aviso(monkeypatch):
    monkeypatch.setattr(datos, "gh", _gh_campo("Otra cosa", falla_field_list=True))
    backend = Backend(CONFIG)
    await backend.listar()

    assert backend.aviso_campo is None


async def test_el_campo_se_sondea_una_sola_vez_por_proceso(monkeypatch):
    """El refresco corre cada 5 minutos: repetir el `field-list` sería una llamada de
    red periódica para volver a saber lo mismo."""
    falso = _gh_campo(
        "Otra cosa",
        campos=[{"id": "PVTF_test", "name": "Due date", "type": "ProjectV2Field"}],
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.listar()
    await backend.listar()
    await backend.listar()

    assert falso.veces("project field-list") == 1


async def test_el_camino_normal_no_pregunta_por_los_campos(monkeypatch):
    """Con la fecha presente en los items no hay nada que sospechar: cero red extra."""
    falso = _gh_campo("Due date", campos=[])
    monkeypatch.setattr(datos, "gh", falso)
    await Backend(CONFIG).listar()

    assert falso.veces("project field-list") == 0


# ------------------------------------------------------------------ transporte GraphQL
async def test_las_variables_no_se_pasan_con_F_para_no_leer_archivos(monkeypatch):
    """`gh api graphql -F clave=@algo` lee ese archivo del disco. Por acá pasan títulos
    y notas escritos por el usuario, así que TODO va con `-f` (raw-field)."""
    falso = GhFalso()
    monkeypatch.setattr(datos, "gh", falso)
    await Backend(CONFIG).crear("pit/web", "@notas.txt", None, "@cuerpo.md")

    for args in falso.usados:
        assert "-F" not in args, f"una variable viajó con -F: {args}"
    creacion = falso.variables_de("CrearIssue")
    assert creacion["titulo"] == "@notas.txt"
    assert creacion["cuerpo"] == "@cuerpo.md"


async def test_el_alta_resuelve_el_id_del_repo_una_sola_vez(monkeypatch):
    """El id sale del `gh repo list` que ya alimenta el picker y queda en el caché de
    disco: preguntarlo en cada alta sería una llamada de red regalada."""
    falso = GhFalso()
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.crear("pit/web", "Uno", None)
    await backend.crear("pit/web", "Dos", None)

    assert falso.veces("IdDeRepo") == 1


async def test_los_repos_del_picker_dejan_sus_ids_listos_para_el_alta(monkeypatch):
    falso = GhFalso(
        {
            "repo list": json.dumps(
                [{"nameWithOwner": "pit/web", "id": "R_web"},
                 {"nameWithOwner": "pit/api", "id": "R_api"}]
            )
        }
    )
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.repos()
    await backend.crear("pit/web", "Something", None)

    assert falso.veces("IdDeRepo") == 0, "el id ya estaba, no había nada que preguntar"
    assert falso.variables_de("CrearIssue")["repo"] == "R_web"


async def test_si_github_no_devuelve_el_issue_no_se_inventa_una_tarea(monkeypatch):
    """Una respuesta sin `issue` dejaría un item_id vacío y una fila fantasma."""
    monkeypatch.setattr(datos, "gh", GhFalso({"CrearIssue": '{"data":{"createIssue":null}}'}))

    with pytest.raises(ErrorGh) as err:
        await Backend(CONFIG).crear("pit/web", "Something", None)

    assert not isinstance(err.value, datos.ErrorParcial)  # no quedó nada a medias
    assert "didn't return" in str(err.value)


async def test_si_el_board_no_devuelve_el_item_se_reporta_como_parcial(monkeypatch):
    """Acá el issue YA existe: decir "couldn't create" llevaría a duplicarlo."""
    monkeypatch.setattr(
        datos, "gh", GhFalso({"AgregarItem": '{"data":{"addProjectV2ItemById":null}}'})
    )

    with pytest.raises(datos.ErrorParcial) as err:
        await Backend(CONFIG).crear("pit/web", "Something", None)

    assert "wasn't added to the board" in str(err.value)


async def test_limpiar_el_vencimiento_usa_su_propia_mutacion(monkeypatch):
    """`updateProjectV2ItemFieldValue` no acepta una fecha vacía: hay que limpiar."""
    falso = GhFalso()
    monkeypatch.setattr(datos, "gh", falso)
    backend = Backend(CONFIG)
    await backend.fechar("PVTI_1", None)
    await backend.fechar("PVTI_1", "2026-09-01")

    assert falso.veces("LimpiarFecha") == 1
    assert falso.veces("FecharItem") == 1
    assert falso.variables_de("FecharItem")["valor"] == "2026-09-01"


# ------------------------------------------------------------------ cierre del issue
def _tarea(issue_id: str = "I_7"):
    from datetime import date

    return datos.Tarea(
        item_id="PVTI_7",
        repo="pit/web",
        numero=7,
        titulo="Renew SSL",
        url="https://github.com/pit/web/issues/7",
        cuerpo="",
        vence=date(2026, 9, 1),
        issue_id=issue_id,
    )


async def test_cerrar_usa_la_mutacion_cuando_conoce_el_node_id(monkeypatch):
    """`gh issue close` resuelve el repo en cada llamada: 1,06 s contra 0,45 s."""
    falso = GhFalso()
    monkeypatch.setattr(datos, "gh", falso)
    await Backend(CONFIG).cerrar(_tarea())

    assert falso.veces("CerrarIssue") == 1
    assert falso.veces("issue close") == 0
    assert falso.variables_de("CerrarIssue")["issue"] == "I_7"


async def test_cerrar_cae_a_gh_issue_close_con_una_tarea_sin_node_id(monkeypatch):
    """Una tarea leída de un caché anterior no lo trae: cerrarla tiene que seguir
    funcionando igual, no explotar ni quedarse sin hacer nada."""
    falso = GhFalso()
    monkeypatch.setattr(datos, "gh", falso)
    await Backend(CONFIG).cerrar(_tarea(issue_id=""))

    assert falso.veces("CerrarIssue") == 0
    assert falso.usados[-1] == ("issue", "close", "7", "--repo", "pit/web")


async def test_listar_trae_el_node_id_de_cada_issue(monkeypatch):
    monkeypatch.setattr(
        datos, "gh", GhFalso({"ItemsDelProject": gh_falso.pagina([gh_falso.item(3)])})
    )
    tareas = await Backend(CONFIG).listar()

    assert [t.issue_id for t in tareas] == ["I_3"]


async def test_el_alta_devuelve_la_tarea_con_su_node_id(monkeypatch):
    """Para poder cerrarla por mutación sin esperar al primer refresco."""
    monkeypatch.setattr(datos, "gh", GhFalso({"CrearIssue": gh_falso.issue_creado(42)}))
    creada = await Backend(CONFIG).crear("pit/web", "Something", None)

    assert creada.issue_id == "I_42"


# ------------------------------------------------------------------ PR vinculado
def _con_pr(*prs: dict, comentarios: int = 0) -> GhFalso:
    """`gh` de mentira cuyo Project devuelve una tarea con esos PRs vinculados."""
    return GhFalso(
        {
            "ItemsDelProject": gh_falso.pagina(
                [gh_falso.item(1, prs=list(prs), comentarios=comentarios)]
            )
        }
    )


async def _una(monkeypatch, falso: GhFalso):
    monkeypatch.setattr(datos, "gh", falso)
    return (await Backend(CONFIG).listar())[0]


async def test_el_pr_vinculado_llega_en_la_misma_lectura_de_la_lista(monkeypatch):
    """La regla dura de este chip: cero llamadas extra en el camino de listar.

    Si algún día el PR se resolviera con una segunda consulta, el refresco pasaría de
    una ida y vuelta a una por tarea y este test lo cazaría antes que el usuario.
    """
    falso = _con_pr(gh_falso.pr(45), comentarios=3)
    tarea = await _una(monkeypatch, falso)

    assert tarea.pr is not None and tarea.pr.numero == 45
    assert tarea.comentarios == 3
    assert falso.veces("ItemsDelProject") == 1
    assert len(falso.usados) == 1, f"llamadas de más: {falso.usados}"


async def test_la_consulta_pide_los_prs_que_cerrarian_el_issue():
    """Los defaults del campo importan: sin `includeClosedPrs` un PR rechazado deja la
    fila sin chip, y con `userLinkedOnly` no aparece ninguno vinculado por `Closes #N`
    (verificado contra la API real el 2026-08-04)."""
    consulta = datos._Q_ITEMS

    assert "closedByPullRequestsReferences" in consulta
    assert "includeClosedPrs: true" in consulta
    assert "userLinkedOnly" not in consulta
    assert "comments { totalCount }" in consulta


async def test_un_pr_abierto_con_ci_verde_queda_listo_para_mergear(monkeypatch):
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(45, ci="SUCCESS")))

    assert (tarea.pr.estado, tarea.pr.ci, tarea.pr.listo) == ("open", "success", True)


@pytest.mark.parametrize("rollup", ["FAILURE", "ERROR"])
async def test_la_ci_roja_no_esta_lista_para_mergear(monkeypatch, rollup):
    """GitHub distingue FAILURE de ERROR; para el usuario las dos son «está roja»."""
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(45, ci=rollup)))

    assert (tarea.pr.ci, tarea.pr.listo) == ("failure", False)


async def test_un_draft_no_esta_listo_aunque_la_ci_este_verde(monkeypatch):
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(45, draft=True, ci="SUCCESS")))

    assert (tarea.pr.estado, tarea.pr.listo) == ("draft", False)


async def test_la_ci_corriendo_no_esta_lista_pero_un_pr_sin_checks_si(monkeypatch):
    """Un rollup en PENDING es trabajo en curso; uno AUSENTE es un repo sin CI, y ahí
    hacer esperar el «ready to merge» sería esperar para siempre."""
    corriendo = await _una(monkeypatch, _con_pr(gh_falso.pr(45, ci="PENDING")))
    sin_checks = await _una(monkeypatch, _con_pr(gh_falso.pr(45, ci=None)))

    assert (corriendo.pr.ci, corriendo.pr.listo) == ("pending", False)
    assert (sin_checks.pr.ci, sin_checks.pr.listo) == ("none", True)


async def test_un_pr_con_conflictos_no_se_anuncia_como_listo(monkeypatch):
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(45, mergeable="CONFLICTING")))

    assert tarea.pr.listo is False


async def test_mergeable_desconocido_tampoco_afirma_que_esta_listo(monkeypatch):
    """GitHub responde UNKNOWN mientras lo calcula: eso no es un permiso."""
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(45, mergeable="UNKNOWN")))

    assert tarea.pr.listo is False


async def test_un_pr_mergeado_se_reconoce_como_tal(monkeypatch):
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(45, estado="MERGED")))

    assert (tarea.pr.estado, tarea.pr.listo) == ("merged", False)


async def test_una_tarea_sin_pr_no_inventa_ninguno(monkeypatch):
    tarea = await _una(monkeypatch, _con_pr())

    assert tarea.pr is None
    assert tarea.comentarios == 0


async def test_entre_varios_pr_gana_el_vivo_y_no_el_intento_muerto(monkeypatch):
    """`closedByPullRequestsReferences` no promete orden, y con los cerrados incluidos
    un intento abandonado puede venir primero: el chip tiene que hablar del PR que el
    usuario está esperando."""
    tarea = await _una(
        monkeypatch,
        _con_pr(
            gh_falso.pr(40, estado="CLOSED"),
            gh_falso.pr(41, estado="MERGED"),
            gh_falso.pr(42, draft=True),
            gh_falso.pr(43),
        ),
    )

    assert tarea.pr.numero == 43


async def test_a_igual_estado_gana_el_pr_mas_nuevo(monkeypatch):
    tarea = await _una(monkeypatch, _con_pr(gh_falso.pr(40), gh_falso.pr(52)))

    assert tarea.pr.numero == 52


async def test_un_pr_ilegible_no_se_lleva_puesto_al_bueno(monkeypatch):
    """Misma regla que el resto de la capa de datos: lo que no se entiende se descarta,
    nunca tumba la lectura entera."""
    tarea = await _una(monkeypatch, _con_pr({"state": "OPEN"}, gh_falso.pr(45)))

    assert tarea.pr.numero == 45
