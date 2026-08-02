"""Aísla la suite del `~/.config` real del que la corre.

`Backend` guarda cada lectura buena en `~/.config/tareas/datos-cache.json`, y los
tests que construyen un `Backend` con un `gh` de mentira escribirían ahí datos
inventados. Apuntando `XDG_CONFIG_HOME` a un temporal cada test parte con el caché
vacío -que es justo lo que hay que probar- y la máquina queda intacta.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def config_aislada(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "tareas"
