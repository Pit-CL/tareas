"""Lo que se puede escribir en un campo de vencimiento, sin UI ni GitHub de por medio.

`interpretar_fecha` recibe el `hoy` explícito, así que todo esto se prueba contra un
día fijo y no cambia de resultado según cuándo corra la suite. El día elegido es un
MIÉRCOLES a propósito: deja los siete días de la semana repartidos entre "esta semana"
y "la que viene", que es donde la regla de la próxima ocurrencia se puede equivocar.
"""

from __future__ import annotations

from datetime import date

import pytest

from tareas_tui.datos import interpretar_fecha

HOY = date(2026, 8, 5)  # miércoles


def leer(texto: str, hoy: date = HOY) -> date | None:
    return interpretar_fecha(texto, hoy)


# ------------------------------------------------------------------ formato canónico
def test_iso_sigue_siendo_el_formato_de_siempre():
    assert leer("2026-09-01") == date(2026, 9, 1)


def test_iso_tolera_espacios_y_caja():
    assert leer("  2026-09-01  ") == date(2026, 9, 1)


def test_iso_imposible_no_se_inventa():
    assert leer("2026-13-01") is None
    assert leer("2026-02-30") is None


# ------------------------------------------------------------------ palabras sueltas
@pytest.mark.parametrize("texto", ["today", "TODAY", " Today "])
def test_today(texto):
    assert leer(texto) == HOY


@pytest.mark.parametrize("texto", ["tomorrow", "tom", "Tomorrow", "TOM"])
def test_tomorrow_y_su_forma_corta(texto):
    assert leer(texto) == date(2026, 8, 6)


# ------------------------------------------------------------------ desplazamientos
def test_dias():
    assert leer("+10d") == date(2026, 8, 15)
    assert leer("+0d") == HOY
    assert leer("+1D") == date(2026, 8, 6)


def test_semanas():
    assert leer("+1w") == date(2026, 8, 12)
    assert leer("+3w") == date(2026, 8, 26)


def test_meses_calendario():
    assert leer("+1m") == date(2026, 9, 5)
    assert leer("+6m") == date(2027, 2, 5)


def test_meses_recortan_el_fin_de_mes_como_las_repetitivas():
    """Misma matemática que `mas_meses`: 31-ene + 1 mes cae en el último día de febrero
    y no se desborda a marzo."""
    assert leer("+1m", date(2026, 1, 31)) == date(2026, 2, 28)


def test_los_espacios_de_mas_no_rompen_el_desplazamiento():
    assert leer("+ 10 d") == date(2026, 8, 15)


@pytest.mark.parametrize("texto", ["+10y", "-5d", "10d", "+d", "++1d", "+10 días"])
def test_desplazamientos_que_no_existen(texto):
    assert leer(texto) is None


# ------------------------------------------------------------------ días de la semana
def test_dia_de_la_semana_es_la_proxima_ocurrencia():
    # HOY es miércoles: el viernes de esta misma semana.
    assert leer("fri") == date(2026, 8, 7)
    assert leer("friday") == date(2026, 8, 7)


def test_un_dia_ya_pasado_esta_semana_cae_en_la_que_viene():
    assert leer("mon") == date(2026, 8, 10)


def test_el_dia_de_hoy_significa_dentro_de_una_semana():
    """Un vencimiento que se escribe es uno que todavía no llegó: parado en miércoles,
    `wed` es el miércoles que viene y nunca hoy (para hoy está `today`)."""
    assert leer("wed") == date(2026, 8, 12)


@pytest.mark.parametrize(
    ("texto", "esperada"),
    [
        ("THU", date(2026, 8, 6)),
        ("tues", date(2026, 8, 11)),
        ("thurs", date(2026, 8, 6)),
        ("saturday", date(2026, 8, 8)),
        ("sun", date(2026, 8, 9)),
    ],
)
def test_formas_largas_intermedias_y_en_mayusculas(texto, esperada):
    assert leer(texto) == esperada


@pytest.mark.parametrize("texto", ["mo", "f", "friyay", "next friday"])
def test_dias_que_no_se_entienden(texto):
    assert leer(texto) is None


# ------------------------------------------------------------------ mes y día
def test_mes_y_dia_en_los_dos_ordenes():
    assert leer("aug 20") == date(2026, 8, 20)
    assert leer("20 aug") == date(2026, 8, 20)


def test_mes_largo_con_coma_y_en_mayusculas():
    assert leer("August 20") == date(2026, 8, 20)
    assert leer("AUG 20,") == date(2026, 8, 20)


def test_hoy_mismo_escrito_como_mes_y_dia_no_salta_de_ano():
    assert leer("aug 5") == HOY


def test_un_dia_ya_pasado_este_ano_se_entiende_el_del_ano_que_viene():
    assert leer("mar 3") == date(2027, 3, 3)
    assert leer("1 jan") == date(2027, 1, 1)


def test_mes_solo_o_dia_solo_no_alcanzan():
    assert leer("aug") is None
    assert leer("20") is None


@pytest.mark.parametrize("texto", ["feb 30", "aug 32", "aug 0", "mmm 3", "aug 20 2026"])
def test_mes_y_dia_que_no_existen(texto):
    assert leer(texto) is None


def test_29_de_febrero_solo_vale_en_un_ano_bisiesto():
    """2028 es bisiesto y 2029 no: escrito después del 29 de febrero de 2028 no hay
    año que valga, así que se pide el formato canónico en vez de inventar el 28."""
    assert leer("feb 29", date(2028, 1, 1)) == date(2028, 2, 29)
    assert leer("feb 29", date(2028, 3, 1)) is None


# ------------------------------------------------------------------ nada que entender
@pytest.mark.parametrize("texto", ["", "   ", None, "asap", "el viernes", "??"])
def test_lo_que_no_se_entiende_devuelve_none(texto):
    assert leer(texto) is None
