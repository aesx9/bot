# backtest/

Backtest 4h de una estrategia de tendencia (SMA50) + RSI estocástico sobre los perpetuos
`PF_XBTUSD`, `PF_ETHUSD` y `PF_SOLUSD` de Kraken Futures. Módulo independiente del bot: no
importa nada de `src/copybot`. El resultado y el veredicto están en [`REPORT.md`](REPORT.md).

```bash
# 1. Descargar (única operación con red; los datos se guardan en backtest/datos/)
.venv/bin/python -m backtest descargar

# 2. Ejecutar (sin red; reproducible: mismos datos + misma semilla = mismo informe)
.venv/bin/python -m backtest ejecutar
.venv/bin/python -m backtest ejecutar --solo-desarrollo   # no toca el 30 % reservado
```

| Ruta | Contenido |
|---|---|
| `datos/` | Velas 4h (`trade`) y funding horario descargados, más `manifest.json` con hashes |
| `resultados/` | CSV de operaciones por tramo y escenario de funding |
| `REPORT.md` | Informe generado (no editar a mano) |
| `*.py` | `data` (descarga/validación), `indicators`, `signals`, `engine` (cartera), `funding_cost`, `metrics`, `validation` (corte, robustez, azar), `runner`, `report`, `cli` |
| `tests/` | Tests sin red (`make test`) |
| `funding/` | Backtest independiente de arbitraje de funding (ver [`funding/README.md`](funding/README.md)) |

## Disciplina de validación

- Las reglas y parámetros están fijados en `config.py` y no se optimizan.
- Corte cronológico 70/30. El tramo reservado (30 % final) se evalúa **una sola vez** con los
  parámetros base; la robustez ±20 % se ejecuta solo sobre desarrollo (hay un test que lo impide
  sobre el reservado y otro que comprueba que alterar el reservado no cambia nada del desarrollo).
- Para depurar sin gastar esa pasada, usar `--solo-desarrollo`.
- Los criterios de aceptación (`Criteria` en `config.py`) se fijaron antes de ver resultados y se
  evalúan con el escenario de funding pesimista.

## Funding

Kraken solo publica ≈1 año de funding (el endpoint no admite rangos). Donde no hay dato se imputa
y se informan siempre dos escenarios (central y pesimista); ver la sección 2 y 5 del informe.
