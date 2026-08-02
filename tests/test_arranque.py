"""Guardas del punto de entrada: los dos modos de falla que dejaban la app inservible.

Ambos terminaban igual de mal por culpa del bucle de reinicio de `bin/tareas`, que
relanza cada 2 s todo lo que no salga con 0, 2, 130 o 143:

* sin terminal de verdad textual no dibuja, escribe ANSI a un pipe sin parar y no
  hay quien la saque (medido: 22 MB de stderr en 20 s con `ssh host tareas`);
* un `config.toml` sin permiso de lectura escapaba como traceback y salía con 1.

Los dos tienen que salir con **2**, que es final.
"""

from __future__ import annotations

import pytest

from tareas_tui import __main__ as entrada
from tareas_tui import config as mod_config

EXENTOS_DEL_BUCLE = {0, 2, 130, 143}  # los mismos que exime bin/tareas


class _Flujo:
    def __init__(self, es_tty: bool) -> None:
        self._es_tty = es_tty

    def isatty(self) -> bool:
        return self._es_tty


@pytest.fixture
def sin_app(monkeypatch):
    """Detecta si `main()` llegó a construir la app pese a la guarda."""
    arrancadas: list[str] = []
    import tareas_tui.app as mod_app

    monkeypatch.setattr(
        mod_app.TareasApp, "run", lambda self, *a, **k: arrancadas.append("run")
    )
    return arrancadas


# ------------------------------------------------------------------ sin TTY
@pytest.mark.parametrize(
    "stdin_tty,stdout_tty",
    [(False, False), (False, True), (True, False)],
)
def test_sin_terminal_sale_en_vez_de_colgarse(monkeypatch, capsys, sin_app, stdin_tty, stdout_tty):
    monkeypatch.setattr(entrada.sys, "stdin", _Flujo(stdin_tty))
    monkeypatch.setattr(entrada.sys, "stdout", _Flujo(stdout_tty))
    monkeypatch.setenv("TAREAS_DEMO", "1")  # ni siquiera el modo demo debe arrancar

    codigo = entrada.main()

    assert codigo == 2
    assert codigo in EXENTOS_DEL_BUCLE  # bin/tareas no la relanza
    assert sin_app == [], "arrancó la TUI sin terminal"
    assert "interactive terminal" in capsys.readouterr().err


def test_con_terminal_no_se_interpone(monkeypatch, capsys, sin_app):
    monkeypatch.setattr(entrada.sys, "stdin", _Flujo(True))
    monkeypatch.setattr(entrada.sys, "stdout", _Flujo(True))
    monkeypatch.setenv("TAREAS_DEMO", "1")

    assert entrada.main() == 0
    assert sin_app == ["run"]


# ------------------------------------------------------------------ config ilegible
def _config_valida(tmp_path):
    destino = tmp_path / "tareas"
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "config.toml").write_text('owner = "pit"\nproject = 1\n', "utf-8")
    return destino / "config.toml"


def test_config_sin_permiso_de_lectura_es_error_de_configuracion(monkeypatch, tmp_path):
    """Escapaba como PermissionError crudo hasta `main()`, que solo atrapa ErrorConfig."""
    ruta = _config_valida(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    original = mod_config.Path.open

    def sin_permiso(self, *args, **kwargs):
        if self == ruta:
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(mod_config.Path, "open", sin_permiso)

    with pytest.raises(mod_config.ErrorConfig) as err:
        mod_config.cargar()
    assert "couldn't read" in str(err.value)


def test_config_ilegible_sale_con_codigo_exento_del_bucle(
    monkeypatch, tmp_path, capsys, sin_app
):
    ruta = _config_valida(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TAREAS_DEMO", raising=False)
    monkeypatch.setattr(entrada.sys, "stdin", _Flujo(True))
    monkeypatch.setattr(entrada.sys, "stdout", _Flujo(True))
    monkeypatch.setattr(entrada.shutil, "which", lambda _: "/usr/bin/gh")

    original = mod_config.Path.open

    def sin_permiso(self, *args, **kwargs):
        if self == ruta:
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(mod_config.Path, "open", sin_permiso)

    codigo = entrada.main()

    assert codigo == 2
    assert codigo in EXENTOS_DEL_BUCLE  # antes salía 1 y se relanzaba cada 2 s
    assert sin_app == []
    assert "couldn't read" in capsys.readouterr().err


def test_un_toml_invalido_sigue_saliendo_igual(monkeypatch, tmp_path, capsys, sin_app):
    """Guarda de no-regresión del error que sí estaba contemplado."""
    destino = tmp_path / "tareas"
    destino.mkdir(parents=True)
    (destino / "config.toml").write_text("esto no es = = toml", "utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TAREAS_DEMO", raising=False)
    monkeypatch.setattr(entrada.sys, "stdin", _Flujo(True))
    monkeypatch.setattr(entrada.sys, "stdout", _Flujo(True))
    monkeypatch.setattr(entrada.shutil, "which", lambda _: "/usr/bin/gh")

    assert entrada.main() == 2
    assert "not valid TOML" in capsys.readouterr().err
