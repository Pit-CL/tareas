"""`gh` de mentira compartido por la suite.

La capa de datos habla GraphQL directo (`gh api graphql`) contra el Project, así que
los dobles responden **por operación** y no por subcomando. Cada documento de
`datos.py` lleva nombre de operación (`ItemsDelProject`, `CrearIssue`, …) justo para
que esto no tenga que parsear GraphQL.

Nada de acá toca la red: se instala con `monkeypatch.setattr(datos, "gh", GhFalso(…))`.
"""

from __future__ import annotations

import json

#: Operaciones GraphQL que la app puede pedir (los nombres viven en `datos.py`).
OPERACIONES = (
    "ItemsDelProject",
    "ComentariosDelIssue",
    "CrearIssue",
    "AgregarItem",
    "CerrarIssue",
    "FecharItem",
    "LimpiarFecha",
    "IdDeRepo",
)


def operacion(args: tuple[str, ...]) -> str:
    """Qué pide esta llamada: el nombre de la operación GraphQL, o `"sub1 sub2"`."""
    if args[:2] != ("api", "graphql"):
        return " ".join(args[:2])
    consulta = next((a for a in args if a.startswith("query=")), "")
    return next((nombre for nombre in OPERACIONES if nombre in consulta), "")


def variables(args: tuple[str, ...]) -> dict[str, str]:
    """Las variables `-f nombre=valor` de una llamada GraphQL."""
    pares: dict[str, str] = {}
    for arg in args:
        if arg.startswith("query=") or "=" not in arg:
            continue
        nombre, _, valor = arg.partition("=")
        pares[nombre] = valor
    return pares


# ------------------------------------------------------------------ respuestas
def pr(
    numero: int = 45,
    *,
    estado: str = "OPEN",
    draft: bool = False,
    ci: str | None = "SUCCESS",
    mergeable: str = "MERGEABLE",
) -> dict:
    """Un nodo de `closedByPullRequestsReferences`, con la forma de la API real.

    `ci` es el `statusCheckRollup.state` (SUCCESS, FAILURE, ERROR, PENDING…) y `None`
    significa un PR SIN un solo check: ahí GitHub manda el rollup entero en null, que
    no es lo mismo que uno corriendo (verificado contra la API el 2026-08-04).
    """
    return {
        "number": numero,
        "state": estado,
        "isDraft": draft,
        "mergeable": mergeable,
        "commits": {
            "nodes": [{"commit": {"statusCheckRollup": None if ci is None else {"state": ci}}}]
        },
    }


def item(
    indice: int = 0,
    *,
    estado: str = "Todo",
    fecha: str | None = "2026-09-01",
    campo_fecha: str = "Due date",
    repo: str = "pit/web",
    titulo: str | None = None,
    cuerpo: str = "",
    tipo: str = "Issue",
    prs: list[dict] | None = None,
    comentarios: int = 0,
) -> dict:
    """Un nodo de item tal cual lo devuelve la API GraphQL."""
    valores: list[dict] = [{"name": estado, "field": {"name": "Status"}}]
    if fecha is not None:
        valores.append({"date": fecha, "field": {"name": campo_fecha}})
    return {
        "id": f"PVTI_{indice}",
        "fieldValues": {"nodes": valores},
        "content": {
            "__typename": tipo,
            "id": f"I_{indice}",
            "number": indice,
            "title": f"Task {indice}" if titulo is None else titulo,
            "body": cuerpo,
            "url": f"https://github.com/{repo}/issues/{indice}",
            "repository": {"nameWithOwner": repo},
            "comments": {"totalCount": comentarios},
            "closedByPullRequestsReferences": {"nodes": list(prs or [])},
        },
    }


def comentario(
    autor: str | None = "elena-lumen",
    cuerpo: str = "Looks good to me.",
    creado: str = "2026-08-04T09:00:00Z",
) -> dict:
    """Un nodo de `comments`, con la forma de la API real.

    `autor=None` es la cuenta borrada: GitHub manda `author: null`, no un login vacío.
    `createdAt` viene siempre en UTC y con «Z», que es lo que hay que saber convertir.
    """
    return {
        "author": {"login": autor} if autor else None,
        "createdAt": creado,
        "body": cuerpo,
    }


def comentarios(*nodos: dict) -> str:
    """Respuesta de `ComentariosDelIssue`, en el orden en que GitHub los devuelve."""
    return json.dumps({"data": {"node": {"comments": {"nodes": list(nodos)}}}})


def pagina(nodos: list[dict], *, cursor: str | None = None) -> str:
    """Respuesta de `ItemsDelProject`; con `cursor` declara que hay página siguiente."""
    return json.dumps(
        {
            "data": {
                "node": {
                    "items": {
                        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        "nodes": nodos,
                    }
                }
            }
        }
    )


def issue_creado(numero: int, repo: str = "pit/web") -> str:
    return json.dumps(
        {
            "data": {
                "createIssue": {
                    "issue": {
                        "id": f"I_{numero}",
                        "number": numero,
                        "url": f"https://github.com/{repo}/issues/{numero}",
                    }
                }
            }
        }
    )


def item_agregado(item_id: str = "PVTI_nueva") -> str:
    return json.dumps({"data": {"addProjectV2ItemById": {"item": {"id": item_id}}}})


def id_repo(identificador: str = "R_test") -> str:
    return json.dumps({"data": {"repository": {"id": identificador}}})


#: Camino feliz: cualquier test que no declare una operación la recibe funcionando.
POR_DEFECTO: dict[str, object] = {
    "ItemsDelProject": pagina([]),
    "ComentariosDelIssue": comentarios(),
    "issue view": '{"comments": []}',
    "CrearIssue": issue_creado(7),
    "AgregarItem": item_agregado("PVTI_test"),
    "CerrarIssue": '{"data":{}}',
    "FecharItem": '{"data":{}}',
    "LimpiarFecha": '{"data":{}}',
    "IdDeRepo": id_repo(),
    "repo list": "[]",
    "repo view": "",
}


class GhFalso:
    """`gh` de mentira con respuesta por operación y registro de lo que se pidió.

    Cada valor puede ser un `str` (la salida), una `Exception` (que se levanta) o un
    invocable que recibe las variables de la llamada y devuelve el `str`.
    """

    def __init__(self, respuestas: dict[str, object] | None = None) -> None:
        self.respuestas = {**POR_DEFECTO, **(respuestas or {})}
        self.usados: list[tuple[str, ...]] = []

    async def __call__(self, *args: str) -> str:
        self.usados.append(args)
        respuesta = self.respuestas.get(operacion(args), "")
        if callable(respuesta):
            respuesta = respuesta(variables(args))
        if isinstance(respuesta, Exception):
            raise respuesta
        return str(respuesta)

    def veces(self, operacion_pedida: str) -> int:
        return sum(1 for args in self.usados if operacion(args) == operacion_pedida)

    def variables_de(self, operacion_pedida: str) -> dict[str, str]:
        """Variables de la ÚLTIMA llamada a esa operación (`{}` si nunca se pidió)."""
        for args in reversed(self.usados):
            if operacion(args) == operacion_pedida:
                return variables(args)
        return {}
