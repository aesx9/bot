"""Validación de contenido (no solo sintaxis) de ml_daily_direction_multiasset_v4.yaml.

Uso: python validate_v3.py   (requiere PyYAML)
Sale con código 1 si alguna comprobación falla.
"""
import datetime as dt, os, sys
import yaml

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_daily_direction_multiasset_v4.yaml")
raw = open(P, encoding="utf-8").read()
d = yaml.safe_load(raw)
fails = []
def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)

REQUIRED = ["id", "version", "status", "live_trading", "lineage", "universe", "effective_coverage", "data", "timing",
            "label", "features", "model", "baselines", "portfolio", "costs", "validation", "decision_criteria",
            "after_a_pass", "reproducibility"]
check(all(k in d for k in REQUIRED), "claves de primer nivel obligatorias presentes")
check(d["id"] == "ml-daily-direction-multiasset-v4", "experiment_id v4")
check("[inicio, fin)" in d["interval_convention"], "convención de intervalos [inicio, fin)")
check(d["live_trading"] is False, "live_trading: false")
check("—" not in raw, "sin rayas largas")

U = ["BTC", "ETH", "SOL", "ZEC", "XRP", "HYPE", "NEAR", "SUI", "ADA"]
check(d["universe"]["primary"]["assets"] == U, "universo principal = exactamente las 9 monedas")
cov = d["effective_coverage"]["assets"]
check(sorted(cov) == sorted(U), "effective_coverage cubre las 9 monedas")
h = cov["HYPE"]
check(h["eligible_from"] is None and h["walk_forward"] == 0 and h["holdout"] == 0, "HYPE no elegible")
check(d["effective_coverage"]["HYPE"]["paper_or_live_trading_in_v4"] == "prohibido", "HYPE no operable en v4")

v = d["validation"]
check(v["purge_and_embargo_days"] == 2, "purge_and_embargo_days = 2")
check("subventanas" in v["row_date_and_purge_definition"], "purga definida también para subventanas")
check(v["final_holdout"]["one_shot"] is True, "holdout one_shot")

c = d["model"]["calibration"]
check("único parámetro" in c["method"] and "sin calibradores por activo" in c["scope"], "una temperatura compartida")
check("180 días" in c["window"] and "seis subventanas" in c["out_of_fold_predictions"] and "30 días" in c["out_of_fold_predictions"],
      "ventana 180 días con 6 subventanas de 30")
check("no 30 por activo" in c["insufficient_support"], "30 etiquetas reales por clase, agregadas")
blocks = ["2023-01-01", "2023-07-01", "2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01"]
exp = [f"[{dt.date.fromisoformat(b) - dt.timedelta(days=180)}, {b})" for b in blocks]
check([w["calibration_window"] for w in c["windows"]] == exp, "ventanas [inicio, fin) de 180 días exactos que terminan en el inicio del bloque")
check(all((dt.date.fromisoformat(b) - dt.date.fromisoformat(e[1:11])).days == 180 for b, e in zip(blocks, exp)), "cada ventana suma 180 días")
check(v["final_holdout"]["period"] == "[2025-07-01, 2026-07-01)", "holdout [2025-07-01, 2026-07-01)")
check("vencido antes de L" in v["row_date_and_purge_definition"], "purga definida por vencimiento de la etiqueta")

a = d["model"]["action_policy"]
check("retorno bruto" in a["rule"] and "retorno bruto" in a["m_estimation"], "m definido como bruto en regla y estimación")
check("una sola vez" in a["costs_applied_once"] and "no un segundo descuento" in a["costs_applied_once"], "costes una sola vez; margen no es descuento")
check("No existe umbral de probabilidad" in a["no_probability_threshold"], "sin umbral de probabilidad")
check(d["features"]["asset_identity"]["use_as_predictor"] is False, "asset_identity fuera de los predictores")
check(d["portfolio"]["max_leverage"] == 1.0, "apalancamiento máximo 1x")
t = d["timing"]["execution"]
check(t["minimum_minutes"] == 45 and "proxy de twap" in t["reference_price"].lower(), "proxy de TWAP con mínimo 45 de 60 minutos")
check("spot_ohlcvt_1m" in d["data"]["sources"] and "perp_ohlc_1m" in d["data"]["sources"], "fuentes de 1 minuto para fase A y fase B")
co = d["costs"]
check(co["fee_schedule"]["futures_taker_fee"] == 0.0005 and co["reference_capital_usd"] == 550, "tarifa taker Futures 0,05 % y capital 550 USD")
check("Kraken Futures" in co["venue"] and "decision_cost" in co and "ledger_cost" in co, "costes de Futures en ambas fases; decisión y libro separados")
check("reutiliza exactamente" in co["phase_contract"]["phase_b"], "fase B reutiliza las posiciones de la fase A")
pr = " ".join(d["decision_criteria"]["pass_requires_all"])
check(all(x in pr for x in ["efectivo", "SMA200", "momentum", "buy-and-hold", "regresión logística", "aleatorias"]), "todos los comparadores en pass_requires_all")
check("concatenados" in d["decision_criteria"]["evaluation_series"], "serie de evaluación definida")

print("\nRESULTADO:", "VÁLIDO" if not fails else f"{len(fails)} fallos")
sys.exit(1 if fails else 0)
