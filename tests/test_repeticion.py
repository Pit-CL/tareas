"""Matemática de las tareas repetitivas, sin UI ni GitHub de por medio.

Dos cosas que dan mal si se hacen a ojo y por eso están cubiertas a conciencia: el
recorte de fin de mes (31 de enero + 1 mes) y el catch-up de una tarea que se cierra
mucho después de vencida (la siguiente ocurrencia nunca debe nacer ya vencida).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tareas_tui.datos import (
    MARCA_REPEAT,
    avanzar,
    componer_cuerpo,
    mas_meses,
    mas_un_mes,
    proxima_fecha,
    separar_repeticion,
)


# ------------------------------------------------------------------ metadato invisible
def test_cuerpo_sin_marca_queda_igual():
    assert separar_repeticion("Just some notes.") == ("Just some notes.", None)


def test_cuerpo_vacio_o_none():
    assert separar_repeticion("") == ("", None)
    assert separar_repeticion(None) == ("", None)


def test_marca_al_final_se_extrae_y_se_saca_del_cuerpo():
    cuerpo = f"Notes here.\n\n{MARCA_REPEAT.format('weekly')}"
    assert separar_repeticion(cuerpo) == ("Notes here.", "weekly")


def test_marca_sola_deja_cuerpo_vacio():
    assert separar_repeticion(MARCA_REPEAT.format("daily")) == ("", "daily")


def test_marca_en_medio_tambien_se_saca():
    cuerpo = f"Before\n{MARCA_REPEAT.format('monthly')}\nAfter"
    limpio, repeat = separar_repeticion(cuerpo)
    assert repeat == "monthly"
    assert "tareas:repeat" not in limpio
    assert limpio == "Before\nAfter"


def test_marca_con_intervalo_desconocido_se_ignora_pero_se_limpia():
    limpio, repeat = separar_repeticion("Notes\n\n<!-- tareas:repeat=yearly -->")
    assert repeat is None
    assert limpio == "Notes"


def test_marca_none_no_es_repeticion():
    assert separar_repeticion(f"Notes\n\n{MARCA_REPEAT.format('none')}") == ("Notes", None)


def test_componer_cuerpo_pega_la_marca_despues_de_las_notas():
    assert componer_cuerpo("Notes", "weekly") == f"Notes\n\n{MARCA_REPEAT.format('weekly')}"


def test_componer_cuerpo_sin_repeticion_es_solo_las_notas():
    assert componer_cuerpo("Notes", None) == "Notes"
    assert componer_cuerpo("Notes", "none") == "Notes"
    assert componer_cuerpo("", None) == ""


def test_componer_cuerpo_sin_notas_es_solo_la_marca():
    assert componer_cuerpo("", "daily") == MARCA_REPEAT.format("daily")
    assert componer_cuerpo(None, "daily") == MARCA_REPEAT.format("daily")


@pytest.mark.parametrize("repeat", ["daily", "weekly", "biweekly", "monthly"])
def test_ida_y_vuelta_del_metadato(repeat):
    notas = "Line one\n\n- bullet"
    assert separar_repeticion(componer_cuerpo(notas, repeat)) == (notas, repeat)


# ------------------------------------------------------------------ meses calendario
def test_mas_un_mes_recorta_al_ultimo_dia():
    assert mas_un_mes(date(2026, 1, 31)) == date(2026, 2, 28)
    assert mas_un_mes(date(2028, 1, 31)) == date(2028, 2, 29)  # bisiesto
    assert mas_un_mes(date(2026, 3, 31)) == date(2026, 4, 30)


def test_mas_un_mes_cruza_el_ano():
    assert mas_un_mes(date(2026, 12, 15)) == date(2027, 1, 15)


def test_mas_meses_cuenta_desde_el_dia_original_sin_arrastrar_el_recorte():
    # 31-ene + 2 meses es 31-mar: si se recortara en cadena (31-ene -> 28-feb -> 28-mar)
    # una mensual del último día del mes se iría corriendo hacia atrás cada febrero.
    assert mas_meses(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert mas_meses(date(2026, 1, 31), 13) == date(2027, 2, 28)


# ------------------------------------------------------------------ avanzar
@pytest.mark.parametrize(
    "repeat,dias", [("daily", 1), ("weekly", 7), ("biweekly", 14)]
)
def test_avanzar_pasos_fijos(repeat, dias):
    base = date(2026, 6, 10)
    assert avanzar(base, repeat, 1) == base + timedelta(days=dias)
    assert avanzar(base, repeat, 3) == base + timedelta(days=3 * dias)


def test_avanzar_rechaza_intervalos_desconocidos():
    with pytest.raises(ValueError):
        avanzar(date(2026, 6, 10), "none", 1)
    with pytest.raises(ValueError):
        avanzar(date(2026, 6, 10), "yearly", 1)


# ------------------------------------------------------------------ próxima ocurrencia
@pytest.mark.parametrize(
    "repeat,dias", [("daily", 1), ("weekly", 7), ("biweekly", 14)]
)
def test_proxima_desde_hoy_es_un_intervalo_adelante(repeat, dias):
    hoy = date(2026, 6, 10)
    assert proxima_fecha(hoy, repeat, hoy) == hoy + timedelta(days=dias)


def test_proxima_con_base_futura_no_hace_catch_up():
    hoy = date(2026, 6, 10)
    base = date(2026, 6, 20)
    assert proxima_fecha(base, "weekly", hoy) == date(2026, 6, 27)


def test_catch_up_semanal_de_una_vencida_hace_un_mes():
    hoy = date(2026, 6, 10)
    base = date(2026, 5, 6)  # 35 días atrás, 5 semanas justas
    proxima = proxima_fecha(base, "weekly", hoy)
    assert proxima == date(2026, 6, 17)
    assert proxima > hoy


def test_catch_up_cae_justo_en_hoy_y_salta_al_siguiente():
    # base + n intervalos == hoy no sirve: la nueva ocurrencia debe quedar en el futuro.
    hoy = date(2026, 6, 10)
    assert proxima_fecha(date(2026, 5, 27), "biweekly", hoy) == date(2026, 6, 24)


def test_catch_up_diario_de_una_muy_vencida():
    hoy = date(2026, 6, 10)
    assert proxima_fecha(date(2025, 1, 1), "daily", hoy) == date(2026, 6, 11)


def test_mensual_desde_fin_de_mes_recorta():
    hoy = date(2026, 2, 15)
    assert proxima_fecha(date(2026, 1, 31), "monthly", hoy) == date(2026, 2, 28)


def test_mensual_con_catch_up_no_arrastra_el_recorte():
    # Cerrada el 10-abr una mensual que vencía el 31-ene: la siguiente es el 30-abr
    # (abril no tiene 31), no el 28-abr que daría recortar en cadena desde febrero.
    hoy = date(2026, 4, 10)
    assert proxima_fecha(date(2026, 1, 31), "monthly", hoy) == date(2026, 4, 30)


def test_mensual_cuando_hoy_es_el_dia_recortado():
    hoy = date(2026, 4, 30)
    assert proxima_fecha(date(2026, 1, 31), "monthly", hoy) == date(2026, 5, 31)


def test_mensual_cruza_el_ano():
    hoy = date(2026, 12, 20)
    assert proxima_fecha(date(2026, 12, 5), "monthly", hoy) == date(2027, 1, 5)


@pytest.mark.parametrize("repeat", ["daily", "weekly", "biweekly", "monthly"])
@pytest.mark.parametrize("atraso", [0, 1, 13, 45, 200, 400])
def test_la_proxima_siempre_queda_en_el_futuro(repeat, atraso):
    hoy = date(2026, 6, 10)
    base = hoy - timedelta(days=atraso)
    assert proxima_fecha(base, repeat, hoy) > hoy


def test_proxima_rechaza_intervalos_desconocidos():
    with pytest.raises(ValueError):
        proxima_fecha(date(2026, 6, 10), "none", date(2026, 6, 10))
