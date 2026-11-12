"""`bin/tareas`: el venv desfasado y el alta headless no pueden dejar al llamador colgado.

Reportado: tras subir el python del sistema, el venv quedó con `lib/python3.12` (con
textual) mientras el intérprete ya era 3.13. El atajo por dist-info daba «instalado»,
`import rich` fallaba con exit 1 y el bucle de reinicio relanzaba `tareas add` cada 2 s
hasta el timeout del agente.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LANZADOR = Path(__file__).resolve().parent.parent / "bin" / "tareas"
VERSION_TEXTUAL = "8.2.8"


def _venv_falso(base: Path, *, lib: str, interprete: str, salida: int) -> tuple[Path, Path]:
    """Venv con un `python` de mentira que registra cada invocación y sale con `salida`."""
    venv = base / "venv"
    (venv / "bin").mkdir(parents=True)
    registro = base / "llamadas.log"
    falso = venv / "bin" / interprete
    falso.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{registro}"\n'
        'case "$*" in\n'
        '  *"-m pip"*) exit 0 ;;\n'
        '  *"import sys"*) echo 3 ; exit 0 ;;\n'
        f"esac\nexit {salida}\n"
    )
    falso.chmod(0o755)
    (venv / "bin" / "python").symlink_to(interprete)
    dist = venv / "lib" / lib / "site-packages" / f"textual-{VERSION_TEXTUAL}.dist-info"
    dist.mkdir(parents=True)
    return venv, registro


def _correr(venv: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LANZADOR), *args],
        env={**os.environ, "TAREAS_VENV": str(venv)},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_add_que_falla_sale_sin_reintentar(tmp_path):
    venv, registro = _venv_falso(tmp_path, lib="python3.13", interprete="python3.13", salida=1)

    try:
        res = _correr(venv, "add", "x")
    except subprocess.TimeoutExpired:
        pytest.fail("`tareas add` quedó en el bucle de reinicio")

    assert res.returncode == 1
    assert "restarting" not in res.stderr
    assert registro.read_text().count("-m tareas_tui") == 1


def test_venv_con_lib_de_otra_version_se_reinstala(tmp_path):
    venv, registro = _venv_falso(tmp_path, lib="python3.12", interprete="python3.13", salida=0)

    _correr(venv, "add", "x")

    assert f"textual=={VERSION_TEXTUAL}" in registro.read_text()
