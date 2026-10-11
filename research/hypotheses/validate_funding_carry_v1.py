"""Validador del prerregistro funding-carry-cross-sectional-v1. Uso: python validate_funding_carry_v1.py <yaml>"""
import sys, yaml, hashlib
p = sys.argv[1]; d = yaml.safe_load(open(p, encoding="utf-8")); err = []
def need(cond, msg):
    if not cond: err.append(msg)
need(d.get("id") == "funding-carry-cross-sectional-v1", "id")
need(d.get("live_trading") is False, "live_trading debe ser false")
need(d.get("status") in ("proposal_pending_gates", "preregistered"), "status")
need((d.get("status") == "preregistered") == (d.get("frozen") is True), "status/frozen incoherentes")
u = d["universe"]; e = u["eligibility_at_decision_time_T"]
need(u["min_eligible"] == 20, "min_eligible")
need("1.000.000" in e["liquidity"] and "30 velas" in e["liquidity"], "liquidez")
need("[T-169h, T-1h)" in e["funding_coverage"] and "160" in e["funding_coverage"], "cobertura")
need(u["liquidity_filter_uses_funding"] is False, "liquidez sin funding")
sg = d["signal"]; need("[T-169h, T-1h)" in sg["score"], "ventana del score")
need("floor(N/5)" in sg["selection"], "quintil"); need(sg["parameter_optimization"] == "prohibited", "optimización")
pf = d["portfolio"]; need(pf["reference_capital_usd"] == 550, "capital")
c = d["costs"]; need(abs(c["fee_taker"] - 0.0005) < 1e-12, "comisión"); need(c["maker_rebates"] is False, "rebates")
ex = d["execution_and_pnl"]
for k in ("retirement_exit", "funding_cash_flow", "side_balance_after_execution", "execution_failure"):
    need(k in ex, f"falta {k}")
need("a = 2 %" in ex["retirement_exit"] and "a = 5 %" in ex["retirement_exit"], "retiradas 2/5 %")
need("-q x absolute_rate(ts)" in ex["funding_cash_flow"], "flujo de funding")
sm = d["sample"]; need(sm["start_recorded"] == "2024-03-11", "inicio"); need("2025-10-06" in sm["holdout"] and "2026-09-28" in sm["holdout"], "holdout")
rp = d["benchmarks"]["random_portfolios"]; need(rp["n"] == 1000 and rp["seed"] == 20261010, "aleatorias n/semilla")
for k in ("k_L", "k_S", "f_LS", "f_SL"): need(k in rp["primary"], f"aleatorias: {k}")
st = d["statistics"]; need(st["bootstrap_blocks_days"] == [10, 20, 40], "bloques"); need("N = 52" in st["multiple_testing"], "N ensayos")
dr = d["decision_rules"]; need(any("8,5 %" in x for x in dr["promising_all_required"]), "umbral 8,5 %")
need(not any("6 %" in x for x in dr["promising_all_required"]), "queda un 6 %")
need("not_evaluable" in dr and "5 %" in dr["inconclusive"], "reglas no evaluable / inconclusa")
need("coupon equivalent" in dr["economic_reference_reported"] and "subasta" in dr["economic_reference_reported"], "serie de letras")
g = d["gates_before_freeze"]; need(g["gate_3_account_eligibility"]["blocks_freeze_or_backtest"] is False, "puerta 3")
print("SHA-256", hashlib.sha256(open(p, "rb").read()).hexdigest())
if err:
    print("ERRORES:", err); sys.exit(1)
print("OK", d["status"], "version", d["version"])
