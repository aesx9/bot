"""Pruebas del validador de tendencia-micros-cme-v1 con YAML sintéticos.

Son copias alteradas del prerregistro.

No usan datos de precios. Llaman a la API run(path) del validador, sin crear
procesos hijo.
Uso, desde hypotheses/: python -m unittest test_validate_tendencia_micros_cme_v1
"""

import copy
import os
import tempfile
import unittest

import validate_tendencia_micros_cme_v1 as validador
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(HERE, "tendencia_micros_cme_v1.yaml")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


BASE = yaml.safe_load(read(YAML_PATH))


def run(text):
    """Escribe el YAML y devuelve (codigo_de_salida, informe) de la API."""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "mut.yaml")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        fails, report = validador.run(p)
        return (1 if fails else 0), "\n".join(report)


def run_dict(d):
    return run(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))


def mutated(fn):
    d = copy.deepcopy(BASE)
    fn(d)
    return d


class TestValidator(unittest.TestCase):
    def assertFails(self, d, fragment):
        code, out = run_dict(d)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL", out)
        self.assertIn(fragment, out)

    def test_original_valid(self):
        code, out = run(read(YAML_PATH))
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertGreater(out.count("OK   "), 150)

    def test_roundtrip_valid(self):
        code, out = run_dict(copy.deepcopy(BASE))
        self.assertEqual(code, 0, out)

    def test_weight_changed(self):
        self.assertFails(
            mutated(lambda d: d["sizing"].update(weight_per_market=0.2)),
            "capital, objetivo, IDM y peso",
        )

    def test_cost_exclusion_prior(self):
        self.assertFails(
            mutated(lambda d: d["universe"].update(cost_based_exclusion="prior_040")), "opción C"
        )

    def test_10y_in_universe(self):
        self.assertFails(
            mutated(
                lambda d: d["universe"]["markets"].update({"10Y": d["universe"]["markets"]["M6E"]})
            ),
            "universo exacto",
        )

    def test_own_audit_blocked_without_exception(self):
        self.assertFails(
            mutated(lambda d: d["data"]["paths"].update(prohibited_read_exceptions=[])),
            "única excepción: auditoría propia",
        )

    def test_provisional_rule_reverted(self):
        self.assertFails(
            mutated(
                lambda d: d["costs"].update(
                    provisional_label_rule=(
                        "Todo resultado con algún componente de coste sin verificar"
                        " se etiqueta PROVISIONAL."
                    )
                )
            ),
            "etiqueta PROVISIONAL solo para tarifas contractuales",
        )

    def test_missing_roll_test(self):
        self.assertFails(
            mutated(
                lambda d: d.update(
                    tests_required_before_backtest=[
                        t for t in d["tests_required_before_backtest"] if "reversión" not in t
                    ]
                )
            ),
            "test obligatorio: Rollover con reversión",
        )

    def test_accounting_order_removed(self):
        self.assertFails(
            mutated(lambda d: d["accounting_and_metrics"].pop("accounting_order")),
            "accounting_order",
        )

    def test_signal_values_old_scheme(self):
        self.assertFails(
            mutated(lambda d: d["signal"].update(allowed_values=[-1.0, -1 / 3, 1 / 3, 1.0])),
            "siete valores",
        )

    def test_live_trading_true(self):
        self.assertFails(mutated(lambda d: d.update(live_trading=True)), "live_trading")

    # ------------------------------------------------------------ rutas
    def test_prohibition_wins_over_a_wider_allowed_path(self):
        """Ampliar allowed_read_paths no desbloquea: la prohibición prevalece."""

        def f(d):
            d["data"]["paths"]["allowed_read_paths"].append("research/data/audit/")

        code, out = run_dict(mutated(f))
        self.assertEqual(code, 0, out)
        self.assertIn("artefacto protegido bloqueado: research/data/audit/data_gate_v4.json", out)
        self.assertNotIn("FAIL", out)

    def test_extra_exception_for_holdout_is_rejected(self):
        def f(d):
            d["data"]["paths"]["prohibited_read_exceptions"].append("research/data/processed/v4/")

        self.assertFails(mutated(f), "única excepción: auditoría propia")

    def test_absolute_path_in_list_is_rejected(self):
        def f(d):
            d["data"]["paths"]["allowed_read_paths"].append("/etc/passwd")

        self.assertFails(mutated(f), "rutas literales sin absolutas")

    def test_traversal_in_list_is_rejected(self):
        def f(d):
            d["data"]["paths"]["allowed_read_paths"].append(
                "research/data/audit/tendencia_micros_cme_v1/../"
            )

        self.assertFails(mutated(f), "rutas literales sin absolutas")

    def test_path_outside_research_is_rejected(self):
        def f(d):
            d["data"]["paths"]["allowed_read_paths"].append("backtest/resultados/")

        self.assertFails(mutated(f), "todas bajo research/")

    def test_write_outside_own_storage_is_rejected(self):
        def f(d):
            d["data"]["paths"]["authorized_write_paths"].append("research/data/processed/v4/")

        self.assertFails(mutated(f), "escrituras autorizadas = rutas de almacenamiento propias")

    def test_missing_matching_rule_is_rejected(self):
        self.assertFails(
            mutated(
                lambda d: d["data"]["paths"].update(matching="Comparación por prefijo de texto.")
            ),
            "matching por segmentos",
        )

    # ------------------------------------- coherencia de contabilidad
    def test_zero_pnl_on_gap_with_position_is_rejected(self):
        def f(d):
            d["accounting_and_metrics"]["accounting_order"] = (
                "(1) P&L del intervalo: si falta S_d, el P&L de ese mercado en d"
                " es 0 y el movimiento se recoge entero en su siguiente liquidación"
                " válida. (2) Operaciones de la liquidación de d: se ejecutan a S_d."
                " (3) La posición resultante solo genera P&L desde el intervalo"
                " siguiente; una apertura a S_d "
                "no genera P&L en d."
            )

        self.assertFails(mutated(f), "sin P&L cero con posición abierta")

    def test_coverage_limits_applied_in_position_is_rejected(self):
        def f(d):
            d["data"]["required_coverage"]["in_position"] = "Hasta 5 sesiones seguidas sin dato."

        self.assertFails(mutated(f), "cero ausencias no recuperadas con posición abierta")

    def test_alternative_source_must_be_official(self):
        def f(d):
            d["data"]["missing_settlement_with_open_position"]["authorized_alternative_source"] = (
                "Cualquier proveedor con precios diarios del contrato."
            )

        self.assertFails(mutated(f), "recuperación sin interpolar desde fuente autorizada")

    # ------------------------------------------- puertas y comisiones
    def test_auto_adding_cme_nfa_fees_is_rejected(self):
        def f(d):
            d["costs"]["principal_taker"]["third_party_fees"] = (
                "No se suponen cero. Si Kraken no confirma que su comisión las"
                " incluye, se suman las tasas "
                "publicadas por CME y NFA para el producto."
            )

        self.assertFails(mutated(f), "tasas de terceros no se suman por defecto")

    def test_gate_status_must_be_pending_evidence(self):
        self.assertFails(
            mutated(lambda d: d["gates"]["kraken_instruments"].update(status="passed")),
            "puerta pendiente de evidencia",
        )

    def test_assuming_physical_delivery_is_rejected(self):
        def f(d):
            d["contracts_and_rollover"]["fx_6e_6b_6a"]["delivery"] = "Entrega física."

        self.assertFails(mutated(f), "sin afirmación de entrega física como hecho")

    # --------------------------------------------- ciclo de órdenes
    def test_order_cycle_must_use_official_calendar(self):
        def f(d):
            d["execution"]["order_cycle"]["step_2_scheduled_session"] = (
                "e_k(s) es la k-ésima sesión con liquidación posterior a s."
            )

        self.assertFails(mutated(f), "sesiones programadas por calendario oficial")

    def test_order_cycle_must_use_real_position(self):
        def f(d):
            d["execution"]["order_cycle"]["step_4_target_at_execution"] = (
                "El objetivo se calcula en s con ideal(s) y la posición de s, aplicando "
                "sizing.per_market_target_from_current_position_q y sizing.fx_same_direction_limit."
            )

        self.assertFails(mutated(f), "histéresis con la posición real al ejecutar")

    def test_order_cycle_must_supersede_old_records(self):
        def f(d):
            d["execution"]["order_cycle"]["step_3_governing_record"] = (
                "En cada sesión esperada e se ejecutan por orden todos los"
                " registros vencidos pendientes."
            )

        self.assertFails(mutated(f), "gobierna el registro vencido más reciente")

    def test_em_dash(self):
        code, out = run(
            read(YAML_PATH).replace("Determinar si", "Determinar " + chr(0x2014) + " si", 1)
        )
        self.assertEqual(code, 1, out)
        self.assertIn("sin rayas largas", out)


# ---------------------------------------------------------------------------
# Modelo de referencia del ciclo de órdenes (execution.order_cycle) con datos
# sintéticos. No es el motor del experimento ni toca precios reales: solo
# comprueba que las reglas escritas en el YAML son inequívocas y deterministas.
# ---------------------------------------------------------------------------


class NoEjecutable(Exception):
    pass


def hysteresis(ideal, q, open_at=0.6, close_below=0.4):
    """sizing.per_market_target_from_current_position_q con la q real."""
    sign = 0 if ideal == 0 else (1 if ideal > 0 else -1)
    if q == 0:
        return sign if abs(ideal) >= open_at else 0
    if sign == q:
        return q if abs(ideal) >= close_below else 0
    return sign if abs(ideal) >= open_at else 0


def contracts_traded(q, target, is_roll):
    """contracts_traded_on_R / reversos: |q| en el saliente + |target| en el entrante."""
    if is_roll:
        return abs(q) + abs(target)
    return abs(target - q)


def run_cycle(sessions, ideals, k, rolls=()):
    """Devuelve (trazas, q_final). sessions = calendario oficial esperado.

    ideals: {sesion: ideal} solo en sesiones con liquidación; una sesión que no
    aparece en ideals es una ausencia de liquidación.
    """
    idx = {s: i for i, s in enumerate(sessions)}
    records, applied, trace, q = {}, set(), [], 0
    for e in sessions:
        has_settlement = e in ideals
        is_roll = e in rolls
        if q != 0 and not has_settlement:
            raise NoEjecutable(f"ausencia con posicion abierta en {e}")
        # registro gobernante: el vencido más reciente
        matured = [
            s for s in records if idx[s] + k < len(sessions) and idx[sessions[idx[s] + k]] <= idx[e]
        ]
        governing = max(matured, key=lambda s: idx[s]) if matured else None
        target = q
        if governing is not None and governing not in applied:
            if has_settlement:
                target = hysteresis(records[governing], q)
                applied.add(governing)
                # los anteriores quedan sustituidos y no se ejecutan nunca
                applied.update(s for s in matured if idx[s] < idx[governing])
            else:
                governing = None  # aplazado, sin aplicar
        if has_settlement and (target != q or is_roll):
            trace.append((e, q, target, governing, contracts_traded(q, target, is_roll)))
            q = target
        if has_settlement:
            records[e] = ideals[e]
    return trace, q


class TestOrderCycleReferenceModel(unittest.TestCase):
    SESSIONS = [f"d{i:02d}" for i in range(1, 13)]

    def ideals(self, default=0.0, gaps=(), **overrides):
        """Ideales sintéticos por sesión. default 0,7 mantiene un largo abierto."""
        out = {s: default for s in self.SESSIONS}
        out.update(overrides)
        for g in gaps:
            del out[g]
        return out

    def test_k1_and_k2_execute_on_scheduled_session(self):
        ideals = self.ideals(d01=0.7)
        t1, _ = run_cycle(self.SESSIONS, ideals, k=1)
        t2, _ = run_cycle(self.SESSIONS, ideals, k=2)
        self.assertEqual(t1[0][0], "d02")
        self.assertEqual(t2[0][0], "d03")

    def test_changing_delay_does_not_recalculate_the_ideal(self):
        """El registro conserva el ideal de su fecha de señal."""
        ideals = self.ideals(d01=0.7, d02=-0.9)
        t1, _ = run_cycle(self.SESSIONS, ideals, k=1)
        t2, _ = run_cycle(self.SESSIONS, ideals, k=2)
        # k=1: d02 ejecuta el ideal de d01 (+0,7) y d03 el de d02 (-0,9).
        self.assertEqual(
            [(e, tg, g) for e, _, tg, g, _ in t1][:2], [("d02", 1, "d01"), ("d03", -1, "d02")]
        )
        # k=2: en d03 gobierna d01 (+0,7), no d02; el ideal no se recalcula.
        self.assertEqual(t2[0][2:4], (1, "d01"))

    def test_scheduled_session_counted_on_official_calendar_despite_gap(self):
        """e_k(s) se cuenta por calendario aunque falte una liquidación."""
        ideals = self.ideals(gaps=("d02",), d01=0.7)  # ausencia, posición plana
        t, _ = run_cycle(self.SESSIONS, ideals, k=1)
        self.assertEqual(t[0][0], "d03")  # aplazada, no adelantada ni perdida
        self.assertEqual(t[0][3], "d01")

    def test_superseded_record_is_never_executed(self):
        # d01 vence en d03 y d02 en d04; con el hueco en d03 el registro de
        # d01 queda sustituido por el de d02 y no se ejecuta nunca.
        ideals = self.ideals(gaps=("d03",), d01=0.7, d02=-0.9)
        t, _ = run_cycle(self.SESSIONS, ideals, k=2)
        executed = [(e, g) for e, _, _, g, _ in t]
        self.assertEqual(executed[0], ("d04", "d02"))
        self.assertNotIn("d01", [g for _, g in executed])

    def test_hysteresis_uses_real_position_not_assumed(self):
        """Con q = 0 un ideal de 0,5 no abre; con q != 0 del mismo signo, mantiene."""
        self.assertEqual(hysteresis(0.5, 0), 0)
        self.assertEqual(hysteresis(0.5, 1), 1)
        self.assertEqual(hysteresis(0.3, 1), 0)
        self.assertEqual(hysteresis(-0.5, 1), 0)  # signo contrario sin llegar a 0,6: efectivo
        self.assertEqual(hysteresis(-0.7, 1), -1)  # reverso

    def test_deferred_order_then_sees_flat_position(self):
        """Si la orden se aplaza con q = 0, al ejecutarse la histéresis usa q = 0."""
        ideals = self.ideals(gaps=("d02",), d01=0.7, d03=0.5)
        t, _ = run_cycle(self.SESSIONS, ideals, k=1)
        self.assertEqual((t[0][0], t[0][1], t[0][2]), ("d03", 0, 1))

    def test_gap_with_open_position_is_not_executable(self):
        # default 0,7 mantiene el largo abierto; el hueco de d05 llega con q = 1.
        ideals = self.ideals(default=0.7, gaps=("d05",))
        with self.assertRaises(NoEjecutable):
            run_cycle(self.SESSIONS, ideals, k=1)

    def test_gap_while_flat_only_defers(self):
        ideals = self.ideals(default=0.0, gaps=("d05",))
        t, q = run_cycle(self.SESSIONS, ideals, k=1)
        self.assertEqual((t, q), ([], 0))

    def test_reversal_trades_two_contracts(self):
        ideals = self.ideals(default=0.7, d04=-0.8)
        t, _ = run_cycle(self.SESSIONS, ideals, k=1)
        rev = [row for row in t if row[0] == "d05"][0]
        self.assertEqual((rev[1], rev[2], rev[4]), (1, -1, 2))

    def test_roll_with_held_position_trades_two_contracts(self):
        self.assertEqual(contracts_traded(1, 1, is_roll=True), 2)  # cierre + apertura
        self.assertEqual(contracts_traded(1, 1, is_roll=False), 0)  # sin rollover, nada
        self.assertEqual(contracts_traded(1, -1, is_roll=True), 2)  # rollover con reverso, no 4
        self.assertEqual(contracts_traded(1, 0, is_roll=True), 1)  # solo cierre
        self.assertEqual(contracts_traded(0, 1, is_roll=True), 1)  # solo apertura

    def test_roll_appears_in_trace_even_without_exposure_change(self):
        ideals = self.ideals(default=0.7)
        t, _ = run_cycle(self.SESSIONS, ideals, k=1, rolls=("d06",))
        roll = [row for row in t if row[0] == "d06"][0]
        self.assertEqual((roll[1], roll[2], roll[4]), (1, 1, 2))

    def test_roll_coinciding_with_reversal_trades_two_not_four(self):
        ideals = self.ideals(default=0.7, d05=-0.8)
        t, _ = run_cycle(self.SESSIONS, ideals, k=1, rolls=("d06",))
        roll = [row for row in t if row[0] == "d06"][0]
        self.assertEqual((roll[1], roll[2], roll[4]), (1, -1, 2))

    def test_cycle_is_deterministic(self):
        ideals = self.ideals(default=0.7, gaps=("d03",), d04=-0.8, d07=0.5, d09=0.9)
        a = run_cycle(self.SESSIONS, ideals, k=2, rolls=("d08",))
        b = run_cycle(self.SESSIONS, ideals, k=2, rolls=("d08",))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
