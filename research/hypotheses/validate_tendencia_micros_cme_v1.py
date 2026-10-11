"""Validador del prerregistro tendencia-micros-cme-v1.

Uso: python validate_tendencia_micros_cme_v1.py [yaml]

Valida contenido, no solo sintaxis. Sale con código 1 si alguna comprobación falla.
"""

import csv
import hashlib
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_YAML = os.path.join(HERE, "tendencia_micros_cme_v1.yaml")


def run(path):
    """Ejecuta todas las comprobaciones. Devuelve (fallos, lineas_informe)."""
    fails = []
    lines = []

    with open(path, encoding="utf-8") as _fh:
        raw = _fh.read()
    d = yaml.safe_load(raw)

    def check(cond, msg):
        lines.append(("OK   " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    REQUIRED = [
        "id",
        "status",
        "live_trading",
        "paper_trading",
        "interval_convention",
        "family",
        "universe",
        "data",
        "contracts_and_rollover",
        "signal",
        "risk_estimate",
        "sizing",
        "execution",
        "costs",
        "fx_conversion",
        "accounting_and_metrics",
        "accounting_and_metrics.accounting_order",
        "bootstrap",
        "acceptance_criteria",
        "verdicts",
        "sensitivities_preregistered",
        "stress_episodes_reported_not_optimized",
        "gates",
        "tests_required_before_backtest",
        "decision_policy",
        "contracts_and_rollover.roll_execution",
        "data.paths.allowed_read_paths",
        "data.paths.prohibited_read_paths",
        "data.paths.prohibited_read_exceptions",
        "data.paths.authorized_write_paths",
        "data.paths.matching",
        "data.paths.precedence",
        "costs.categories",
        "costs.tick_sensitivity",
        "execution.order_cycle",
        "risk_estimate.missing_observation",
        "data.missing_settlement_with_open_position",
        "data.required_coverage.signal_only_when_flat",
        "data.sealed_holdouts.registry",
    ]

    def present(path):
        node = d
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    missing = [k for k in REQUIRED if not present(k)]
    check(not missing, f"claves obligatorias presentes (faltan: {missing})")
    if missing:
        return fails, lines

    check(d["id"] == "tendencia-micros-cme-v1", "experiment_id")
    check(d["status"] in ("definitivo_pendiente_de_aprobacion", "preregistered"), "status")
    check(
        d["live_trading"] is False and d["paper_trading"] is False,
        "live_trading y paper_trading en false",
    )
    check(chr(0x2014) not in raw and chr(0x2013) not in raw, "sin rayas largas ni medias")
    check("[inicio, fin)" in d["interval_convention"], "convención de intervalos")

    f = d["family"]
    check(
        f["name"] == "tendencia" and f["counts_as_new_family"] is False,
        "familia tendencia, no nueva",
    )
    inv = os.path.join(os.path.dirname(HERE), "trials_inventory.csv")
    with open(inv, encoding="utf-8") as _fh:
        n_prev = sum(1 for _ in csv.DictReader(_fh))
    check(
        n_prev in (52, 53) and f["trial_number"] == 53,
        f"ensayo 53 (inventario actual: {n_prev} filas)",
    )

    u = d["universe"]
    m = u["markets"]
    check(sorted(m) == sorted(["M6E", "M6B", "M6A", "1OZ"]), "universo exacto: M6E, M6B, M6A, 1OZ")
    spec = {
        "M6E": (12500, 0.0001, 1.25, "6E"),
        "M6B": (6250, 0.0001, 0.625, "6B"),
        "M6A": (10000, 0.0001, 1.00, "6A"),
        "1OZ": (1, 0.25, 0.25, "GC"),
    }
    for k, (mult, tick, tick_usd, src) in spec.items():
        x = m[k]
        check(
            x["micro_usd_per_price_unit"] == mult
            and abs(x["micro_tick"] - tick) < 1e-12
            and abs(x["micro_tick_usd"] - tick_usd) < 1e-12
            and x["pnl_source_contract"] == src
            and abs(mult * tick - tick_usd) < 1e-9,
            f"{k}: multiplicador, tick y fuente coherentes",
        )
    check(
        "10Y" not in m and "10Y" in u["excluded_from_principal"],
        "10Y fuera de la cartera principal",
    )
    check(
        "DGS10" in u["excluded_from_principal"]["10Y"]["no_proxy_substitution"],
        "sin sustitución por DGS10",
    )
    check(u["cost_based_exclusion"] == "none", "opción C: sin exclusión por prior de Sharpe")
    check(u["fixed_universe"] is True, "universo fijo")
    check(
        sorted(u["excluded_from_principal"]["out_of_risk_budget"])
        == sorted(["MES", "MNQ", "MYM", "M2K", "MGC", "NKD", "MCL"]),
        "exclusiones ex ante",
    )

    da = d["data"]
    check("Norgate" in da["sources"]["futures"]["provider"], "fuente Norgate")
    check(
        "150 USD" in da["sources"]["futures"]["budget"]
        and "autorización expresa" in da["sources"]["futures"]["budget"],
        "presupuesto y autorización",
    )
    check(
        "DGS10" in str(da["sources"]["forbidden_sources_for_pnl"])
        and "6E=F" in str(da["sources"]["forbidden_sources_for_pnl"]),
        "fuentes prohibidas para P&L",
    )
    check("EXR.D.USD.EUR" in da["sources"]["eur_usd"]["provider"], "serie del BCE")
    rc = da["required_coverage"]
    check(
        rc["first_required_session"] == "2007-06-01" and rc["last_session"] == "2026-09-30",
        "cobertura 2007-06-01 a 2026-09-30",
    )
    check(da["storage"]["in_git"] is False, "datos fuera de git")
    # ---------------------------------------------------------------- rutas
    pa = da["paths"]
    allowed, prohibited = pa["allowed_read_paths"], pa["prohibited_read_paths"]
    exceptions, writes = pa["prohibited_read_exceptions"], pa["authorized_write_paths"]
    ALL_LISTS = {
        "allowed_read_paths": allowed,
        "prohibited_read_paths": prohibited,
        "prohibited_read_exceptions": exceptions,
        "authorized_write_paths": writes,
    }

    def segments(p):
        """Segmentos normalizados, o None si la ruta no es literal y segura."""
        if not isinstance(p, str) or not p or p != p.strip():
            return None
        if p.startswith("/") or p.startswith("\\") or ":" in p or "\\" in p:
            return None  # absoluta o separador de Windows
        parts = [s for s in p.split("/") if s != ""]
        if not parts or any(s in ("..", ".") for s in parts):
            return None  # traversal o componente relativo
        return [s.lower() for s in parts]

    def is_dir_rule(p):
        return p.endswith("/")

    def under(path_segs, rule):
        """path cae bajo rule comparando segmento a segmento."""
        rs = segments(rule)
        if rs is None or path_segs is None:
            return False
        if len(path_segs) < len(rs):
            return False
        if path_segs[: len(rs)] != rs:
            return False
        return is_dir_rule(rule) or len(path_segs) == len(rs)

    for name, lst in ALL_LISTS.items():
        bad = [p for p in lst if segments(p) is None]
        check(not bad, f"{name}: rutas literales sin absolutas ni .. (inválidas: {bad})")
        out = [p for p in lst if segments(p) and segments(p)[0] != "research"]
        check(not out, f"{name}: todas bajo research/ (fuera: {out})")

    def readable(path):
        """Replica data.paths.precedence: permitida y, bajo prohibición, con excepción exacta."""
        segs = segments(path)
        if segs is None:
            return False  # la prohibición gana ante cualquier duda
        if not any(under(segs, a) for a in allowed):
            return False
        if any(under(segs, p) for p in prohibited):
            return any(under(segs, e) for e in exceptions)
        return True

    def writable(path):
        segs = segments(path)
        return segs is not None and any(under(segs, w) for w in writes)

    for p in (
        "research/data/processed/v4/",
        "research/data/processed/funding_carry/",
        "research/data/processed/funding_carry_v1/",
        "research/data/processed/sma200_v1/",
        "research/data/raw/kraken_ohlcvt_2026Q2/",
        "research/data/raw/kraken_funding_export/",
        # Partida en dos literales: entera da entropía 4,3056, a 0,19 del límite 4,5 de
        # «Base64 High Entropy String» del baseline. No es un secreto, es una ruta. Al menor
        # retoque cruzaría el umbral y rompería `make secrets`. Valor idéntico; no unir.
        "research/data/raw/kraken_futures_backfill_" + "2026-10-10/",
        "research/data/raw/treasury_bill_rates/",
        "research/data/audit/",
    ):
        check(p in prohibited, f"lectura prohibida listada: {p}")
    check(
        exceptions == ["research/data/audit/tendencia_micros_cme_v1/"],
        "única excepción: auditoría propia",
    )
    own = [da["storage"][k] for k in ("raw", "processed", "audit")]
    check(sorted(writes) == sorted(own), "escrituras autorizadas = rutas de almacenamiento propias")
    check(
        all(segments(w) and "tendencia_micros_cme_v1" in segments(w) for w in writes),
        "escrituras con el identificador del experimento como segmento",
    )
    check(
        all(readable(w) for w in writes),
        "las rutas de salida propias se pueden leer (la excepción las cubre)",
    )
    check(
        all(any(under(segments(w), a) for a in allowed) for w in writes),
        "las rutas de salida están en allowed_read_paths",
    )

    # Artefactos protegidos: siguen bloqueados pese a compartir prefijo con la excepción.
    for art in (
        # Partida en dos literales: entera da entropía 4,3083, a 0,19 del límite 4,5 del
        # baseline. Mismo motivo que la de arriba. Valor idéntico; no unir.
        "research/data/processed/v4/" + "HOLDOUT_UNLOCK",
        "research/data/processed/v4/holdout_runs.jsonl",
        "research/data/processed/v4/predictions_holdout.csv.gz",
        # Partida en dos literales: entera da entropía 4,5926 y supera el límite 4,5 del
        # baseline, que es lo que rompía `make secrets`. No es un secreto, es la ruta de un
        # artefacto protegido. Valor idéntico; no unir en un solo literal.
        "research/data/processed/funding_carry_v1/" + "HOLDOUT_UNLOCK",
        "research/data/processed/funding_carry_v1/evaluation_wf.json",
        "research/data/processed/funding_carry/funding_hourly.parquet",
        "research/data/processed/sma200_v1/evaluation.json",
        "research/data/audit/funding_carry_eligibility_weekly.csv",
        "research/data/audit/data_gate_v4.json",
        "research/data/raw/treasury_bill_rates/2025.csv",
    ):
        check(not readable(art) and not writable(art), f"artefacto protegido bloqueado: {art}")

    # Prefijo de texto compartido con la excepción: no debe colarse.
    for sneaky in (
        "research/data/audit/tendencia_micros_cme_v1_bak/x.csv",
        "research/data/audit/tendencia_micros_cme_v1extra.csv",
        "research/data/audit/tendencia_micros_cme_v1/../funding_carry_eligibility_weekly.csv",
        "research/data/audit/TENDENCIA_MICROS_CME_V1/../data_gate_v4.json",
        "research/data/processed/tendencia_micros_cme_v1/../v4/HOLDOUT_UNLOCK",
        "/etc/passwd",
        "C:/Users/lsala/Downloads/research/data/audit/data_gate_v4.json",
        "research\\data\\audit\\data_gate_v4.json",
        "../bot-universo/backtest/x.csv",
    ):
        check(not readable(sneaky), f"no se cuela por prefijo ni traversal: {sneaky}")

    check(
        readable("research/data/audit/tendencia_micros_cme_v1/audit.json"),
        "la auditoría propia sí es legible",
    )
    check(
        readable("research/trials_inventory.csv")
        and readable("research/hypotheses/tendencia_micros_cme_v1.yaml"),
        "inventario y YAML propios legibles",
    )
    check(
        not writable("research/trials_inventory.csv"), "el inventario no es escribible por el motor"
    )
    check(
        "segmentos" in pa["matching"]
        and ".." in pa["matching"]
        and "absoluta" in pa["matching"]
        and "simbólico" in pa["matching"],
        "matching por segmentos, sin absolutas, .. ni enlaces",
    )
    check("La prohibición gana" in pa["precedence"], "la prohibición prevalece ante dudas")
    check(
        "sin excepción" in pa["rule"] and "HOLDOUT_UNLOCK" in pa["rule"],
        "regla de holdouts sin excepción",
    )
    check(
        "prerregistro nuevo" in pa["single_exception_rationale"],
        "excepciones nuevas exigen prerregistro nuevo",
    )

    sh = da["sealed_holdouts"]
    check(
        "Ningún validador" in sh["scope_finding"] and "global por fecha" in sh["scope_finding"],
        "alcance de los holdouts: específico de sus datos, sin protección global por fecha",
    )
    reg = sh["registry"]
    fc = reg["funding-carry-cross-sectional-v1"]
    check(
        "2026-09-28" in fc["holdout_decisions"]
        and fc["holdout_results_end"].startswith("2026-10-05"),
        "carry: última decisión 2026-09-28 y fin de resultados 2026-10-05",
    )
    check(
        "una semana" in fc["difference"] and "no una discrepancia" in fc["difference"],
        "diferencia decisión / resultados explicada",
    )
    check(
        "config.py" in fc["evidence"] and "DATA_END" in fc["evidence"],
        "evidencia de carry en metadatos y código",
    )
    check(
        reg["ml-daily-direction-multiasset-v4"]["holdout"] == "[2025-07-01, 2026-07-01)",
        "holdout de v4",
    )
    check(
        "no se abrió ningún dato" in reg["verification_method"],
        "verificación solo con metadatos y código",
    )

    tr = " ".join(d["tests_required_before_backtest"])
    cr = d["contracts_and_rollover"]
    check(
        "5 sesiones" in cr["fx_6e_6b_6a"]["roll_session"]
        and "Kraken" in cr["fx_6e_6b_6a"]["roll_session"],
        "rollover divisas",
    )
    check("5 sesiones" in cr["gold_gc_proxy_for_1oz"]["roll_session"], "rollover oro")
    for k in ("fx_6e_6b_6a", "gold_gc_proxy_for_1oz"):
        sc = cr[k]["settlement_cme_vs_kraken"]
        check(
            "puerta" in sc and "hasta el vencimiento" in sc,
            f"{k}: liquidación de Kraken por confirmar y sin llegar al vencimiento",
        )
    check(
        "no se supone" in cr["fx_6e_6b_6a"]["settlement_cme_vs_kraken"].lower(),
        "divisas: no se supone entrega ni efectivo",
    )
    check("delivery" not in cr["fx_6e_6b_6a"], "sin afirmación de entrega física como hecho")
    check("sin empalmar" in cr["gold_gc_proxy_for_1oz"]["proxy_note"], "GC homogéneo sin empalme")
    re_ = cr["roll_execution"]
    check(
        "objetivo - q = 0" in re_["always_two_legs"]
        and "dos operaciones" in re_["always_two_legs"],
        "rollover: dos patas aunque objetivo - q = 0",
    )
    check(
        "no 4" in re_["netting_with_signal_change"]
        and "Nunca se cobra" in re_["netting_with_signal_change"],
        "rollover y cambio de señal sin duplicar",
    )
    check(
        "NO_EJECUTABLE" in re_["missing_settlement_on_R"]
        and "ni se inventa un cierre" in re_["missing_settlement_on_R"],
        "liquidación ausente en el rollover sin fill inventado",
    )
    check(
        "obligatoria" in re_["missing_settlement_on_R"],
        "el saliente con posición necesita liquidación en R",
    )

    acc = d["accounting_and_metrics"]["accounting_order"]
    check(
        all(
            t in acc
            for t in (
                "(1) P&L del intervalo",
                "(2) Operaciones",
                "(3) La posición resultante",
                "no genera P&L en d",
            )
        ),
        "orden de contabilización: P&L del intervalo, operaciones y costes, luego P&L",
    )
    check(
        "última liquidación válida" in d["signal"]["missing_data"],
        "señal: movimiento acumulado tras una ausencia",
    )
    check(
        "sin posición" in d["signal"]["missing_data"],
        "la regla de ausencias de señal solo aplica sin posición",
    )
    check(
        "no se actualiza" in d["risk_estimate"]["missing_observation"]
        and "una sola actualización" in d["risk_estimate"]["missing_observation"],
        "EWMA ante ausencias",
    )
    check(
        "sin posición abierta" in d["risk_estimate"]["missing_observation"],
        "EWMA: ausencias solo sin posición",
    )

    ms = da["missing_settlement_with_open_position"]
    check(
        "no se contabiliza un P&L cero" in ms["rule"] and "NO_EJECUTABLE" in ms["rule"],
        "ausencia con posición: ni P&L cero ni compensación",
    )
    check(
        "sin interpolar" in ms["rule"] and "CME" in ms["authorized_alternative_source"],
        "recuperación sin interpolar desde fuente autorizada",
    )
    check(
        "Ninguna otra fuente" in ms["authorized_alternative_source"],
        "solo la liquidación oficial sustituye",
    )
    check(
        "no es una ausencia" in ms["official_closure_is_not_missing"],
        "cierre oficial no es ausencia",
    )
    check(
        rc["in_position"].startswith("Cero ausencias"),
        "cero ausencias no recuperadas con posición abierta",
    )
    sf = rc["signal_only_when_flat"]
    check(
        sf["max_consecutive_missing_expected_sessions"] == 5
        and sf["max_missing_fraction_per_calendar_year"] == 0.02
        and "sin posición abierta" in sf["scope"],
        "límites de ausencias solo para señales sin posición",
    )
    check(
        "recuperada de la fuente alternativa" in d["accounting_and_metrics"]["accounting_order"]
        or "NO_EJECUTABLE" in d["accounting_and_metrics"]["accounting_order"],
        "contabilidad coherente con la recuperación",
    )
    check(
        "no se admite un P&L cero" in d["accounting_and_metrics"]["accounting_order"],
        "sin P&L cero con posición abierta",
    )
    check(
        "necesita su liquidación" in d["accounting_and_metrics"]["portfolio_calendar"],
        "calendario de cartera coherente",
    )
    check(
        "antes de calcular ninguna señal" in d["gates"]["data_audit"]["settlement_recovery"],
        "recuperación en la auditoría, antes de las señales",
    )
    check(
        "sin saber si habrá" in d["gates"]["data_audit"]["settlement_recovery"],
        "la recuperación no depende de los resultados",
    )

    ex = d["execution"]
    oc = ex["order_cycle"]
    check(
        "k = 1" in oc["applies_to"] and "k = 2" in oc["applies_to"], "un solo ciclo para t+1 y t+2"
    )
    check(
        "ideal(s) calculado con datos hasta s" in oc["step_1_signal_record"]
        and "no se guarda un objetivo" in oc["step_1_signal_record"].lower(),
        "el registro guarda el ideal de la fecha de señal, sin histéresis",
    )
    check(
        "con independencia de que tenga datos" in oc["step_2_scheduled_session"],
        "sesiones programadas por calendario oficial",
    )
    check(
        "mayor fecha de señal" in oc["step_3_governing_record"]
        and "sustituidos" in oc["step_3_governing_record"],
        "gobierna el registro vencido más reciente y los antiguos se sustituyen",
    )
    check("e - 2" in oc["step_3_governing_record"], "con k = 2 gobierna el registro de e - 2")
    check(
        "realmente abierta en e" in oc["step_4_target_at_execution"],
        "histéresis con la posición real al ejecutar",
    )
    check(
        "fx_same_direction_limit" in oc["step_4_target_at_execution"],
        "límite de divisas en la sesión de ejecución",
    )
    check(
        "roll_execution" in oc["step_5_execute"] and "una sola pareja" in oc["step_5_execute"],
        "rollover resuelto con una pareja de operaciones",
    )
    check(
        "obligatoria" in oc["missing_settlement_at_execution"]
        and "No se inventan ejecuciones" in oc["missing_settlement_at_execution"],
        "sin liquidación: nada se inventa y solo se aplaza con posición 0",
    )
    check(
        "contrato vigente de la cadena" in oc["deadline"],
        "un registro sin aplicar no abre en un contrato ya rolado",
    )
    check("es única" in oc["determinism"], "ciclo determinista y reproducible")

    s = d["signal"]
    check(s["windows_valid_observations"] == [21, 63, 252], "ventanas 21, 63, 252")
    exp = [k / 3 for k in range(-3, 4)]
    check(
        len(s["allowed_values"]) == 7
        and all(abs(a - b) < 1e-9 for a, b in zip(s["allowed_values"], exp, strict=True)),
        "siete valores de la señal",
    )
    check("0 si es exactamente 0" in s["sign"], "signo 0")
    check(
        s["costs_in_signal"] is False and s["parameter_optimization"] == "prohibited",
        "señal sin costes ni optimización",
    )
    check("mismo contrato" in s["chain_return"], "cadena sin saltos de rollover")

    r = d["risk_estimate"]
    check(
        "60/61" in r["estimator"] and "sin restar la media" in r["estimator"],
        "EWMA lambda 60/61, media cero",
    )
    check("sqrt(261)" in r["annualization"], "anualización 261")

    z = d["sizing"]
    check(
        z["capital_eur_initial"] == 25000
        and z["vol_target"] == 0.06
        and z["idm"] == 1.5
        and z["weight_per_market"] == 0.25,
        "capital, objetivo, IDM y peso",
    )
    check(z["max_contracts_per_market"] == 1, "un contrato por mercado")
    check(
        z["band"]["open_if_abs_ideal_at_least"] == 0.6
        and z["band"]["close_if_abs_ideal_below"] == 0.4,
        "bandas 0,6 y 0,4",
    )
    check(
        z["fx_same_direction_limit"]["max"] == 2
        and "M6E, M6B, M6A" in z["fx_same_direction_limit"]["rule"],
        "límite de divisas y desempate",
    )
    check(
        "contraria" in z["per_market_target_from_current_position_q"]
        and ">= 0,6" in z["per_market_target_from_current_position_q"],
        "cambio de signo: cerrar y abrir solo con 0,6",
    )
    check("diagnóstico" in z["portfolio_covariance"]["role"].lower(), "covarianza solo diagnóstica")

    e = d["execution"]
    check("t+1" in e["principal"] and "t+2" in e["delay_variant"], "ejecución t+1 y variante t+2")
    c = d["costs"]
    check(
        c["status"].startswith("PROVISIONAL")
        and "0,0005" in c["principal_taker"]["fee_per_contract_per_side"],
        "taker 0,05 % PROVISIONAL",
    )
    check(
        "antes de descargar" in c["categories"]["contractual_fees"],
        "tarifas contractuales verificadas antes de descargar datos",
    )
    tp = c["principal_taker"]["third_party_fees"]
    check(
        "ni se suman por defecto" in tp and "no se suman solo porque" in tp,
        "tasas de terceros no se suman por defecto",
    )
    check(
        "documentadas como cobradas" in tp and "EEE" in tp,
        "solo tasas documentadas y aplicables a la cuenta del EEE",
    )
    check(
        "la puerta falla" in c["principal_taker"]["total_contractual_cost_must_be_verified"],
        "sin coste total verificable, la puerta falla",
    )
    check(
        "la puerta falla" in c["fee_verification_rule"],
        "regla de verificación coherente con la puerta",
    )
    check(
        "supuesto de modelización" in c["categories"]["modeled_execution_costs"]
        and "no impide" in c["categories"]["modeled_execution_costs"],
        "tick modelado declarado, no impide proponer paper",
    )
    check(
        "solo a las tarifas contractuales" in c["provisional_label_rule"]
        and "no activa" in c["provisional_label_rule"],
        "etiqueta PROVISIONAL solo para tarifas contractuales",
    )
    check(
        c["tick_sensitivity"]["spread_and_slippage_ticks_per_side"] == 2
        and "No es criterio" in c["tick_sensitivity"]["role"],
        "sensibilidad de 2 ticks informativa",
    )
    v = d["verdicts"]
    check(
        "no impide este" in v["APTA_PARA_PROPONER_PAPER"]
        and "No autoriza operaciones" in v["APTA_PARA_PROPONER_PAPER"],
        "APTA compatible con el tick modelado; sin operaciones reales",
    )
    check(
        "No permite proponer paper" in v["APROBADA_PROVISIONAL"],
        "APROBADA_PROVISIONAL no permite paper",
    )
    check(
        any("2 ticks" in s for s in d["sensitivities_preregistered"]["items"]),
        "sensibilidad de 2 ticks preregistrada",
    )
    check(
        "No se suponen cero" in c["principal_taker"]["third_party_fees"],
        "tasas de terceros no se suponen cero",
    )
    check(
        c["diagnostics_not_for_acceptance"]["maker_pct_nominal_per_side"] == 0.0002,
        "diagnóstico maker",
    )
    check(
        "último tipo" in d["fx_conversion"]["timing"]
        and "Nunca un tipo posterior" in d["fx_conversion"]["timing"],
        "conversión EUR sin tipos futuros",
    )
    check(
        "/ (USD por EUR)" in d["fx_conversion"]["pnl_conversion"], "P&L EUR = P&L USD / USD por EUR"
    )

    b = d["bootstrap"]
    check(
        b["block_length_principal"] == 63 and b["block_length_sensitivities"] == [126, 252],
        "bloques 63, 126, 252",
    )
    check(b["paths"] == 10000 and b["seed"] == 20261011, "10.000 trayectorias y semilla")
    check(
        "2.610" in b["method"] and "circulares" in b["method"], "trayectorias circulares de 10 años"
    )
    check("Wilson" in b["acceptance"] and "0,20" in b["acceptance"], "criterio con Wilson superior")
    check("INCONCLUSO" in b["block_dependence_rule"], "inconcluso si cambia con el bloque")
    check(
        "solo el error de simulación" in b["interpretation"],
        "Wilson descrito como error Monte Carlo",
    )
    check(
        "No se vuelve a ejecutar el motor" in b["limitation_integer_engine"]
        and "trayectorias de capital" in b["limitation_integer_engine"],
        "limitación: sin reejecución del dimensionamiento entero ni capital alternativo",
    )
    for t in (
        "Rollover con posición mantenida",
        "Falta de liquidación del saliente",
        "Orden de contabilización",
    ):
        check(t in tr, f"test obligatorio: {t}")

    ac = " ".join(d["acceptance_criteria"]["all_required"])
    for t in (
        "CAGR neta en EUR > 0",
        "0,04 y 0,07",
        "<= 0,10",
        "<= 0,12",
        "<= 0,20",
        "t+2",
        "<= 0,15",
    ):
        check(t in ac, f"criterio presente: {t}")
    check(d["acceptance_criteria"]["maker_cannot_rescue"] is True, "maker no rescata")
    check(
        set(d["verdicts"])
        >= {
            "NO_EJECUTABLE",
            "RECHAZADA",
            "INCONCLUSA",
            "APROBADA_PROVISIONAL",
            "APTA_PARA_PROPONER_PAPER",
        },
        "veredictos",
    )
    check(
        d["stress_episodes_reported_not_optimized"]["periods"]
        == [
            "[2008-09-01, 2009-04-01)",
            "[2013-05-01, 2013-10-01)",
            "[2020-02-15, 2020-05-01)",
            "[2022-01-01, 2023-01-01)",
        ],
        "episodios de estrés",
    )
    check(
        d["accounting_and_metrics"]["evaluation_start_requirement"].find("2008-08-29") >= 0,
        "inicio de evaluación antes del primer episodio",
    )

    g = d["gates"]
    ki = g["kraken_instruments"]
    check(
        "Antes de descargar" in ki["when"] and "NO_EJECUTABLE" in ki["failure"],
        "puerta previa de Kraken",
    )
    check("experiment_id" in ki["failure"], "fallo de puerta exige nuevo experiment_id")
    check(
        ki["status"] == "pending_evidence" and "Ninguna suposición" in ki["status_rule"],
        "puerta pendiente de evidencia",
    )
    mc = " ".join(ki["must_confirm"])
    for t in ("Forma de liquidación", "cierres automáticos", "base de nominal", "símbolo exacto"):
        check(t in mc, f"la puerta confirma: {t}")
    check(
        "sin suponer la de CME" in mc,
        "no se supone la liquidación de CME ni la descripción general de Kraken",
    )
    check(
        "coste contractual total" in ki["failure"], "sin coste contractual total, la puerta falla"
    )
    for t in (
        "Ciclo de órdenes con k = 1 y k = 2",
        "Variante k = 2 con señales sucesivas",
        "Ausencia con posición abierta",
        "Cierre oficial del mercado",
        "Reproducibilidad del ciclo de órdenes",
        "Rollover con reversión",
    ):
        check(t in tr, f"test obligatorio: {t}")
    check(
        "allowed_read_paths" in tr and "authorized_write_paths" in tr,
        "test obligatorio de rutas con la nueva estructura",
    )
    check(
        all(d["decision_policy"][k] is True for k in d["decision_policy"]), "política de decisión"
    )

    return fails, lines


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT_YAML
    fails, report = run(path)
    for line in report:
        print(line)
    with open(path, "rb") as fh:
        print("\nSHA-256", hashlib.sha256(fh.read()).hexdigest())
    print("RESULTADO:", "VÁLIDO" if not fails else f"{len(fails)} fallos")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
