# backtest/funding/

Backtest de dos estrategias de arbitraje de funding neutrales al precio, evaluadas por separado:

- **A**: entre plataformas, perpetuos de Kraken Futures y Hyperliquid.
- **B**: cash and carry en Kraken: spot largo + perpetuo `PF_` corto.

Reutiliza utilidades de `backtest/` (formato del informe, drawdown, `DataError`); no importa nada
de `src/copybot`. El resultado y el veredicto quedan en [`REPORT.md`](REPORT.md) cuando se ejecute.

```bash
# 1. Universo (red): instrumentos y velas diarias de 90 días -> datos/universo.json
.venv/bin/python -m backtest.funding descargar-universo
.venv/bin/python -m backtest.funding universo          # vuelve a mostrarlo, sin red

# 2. Series horarias del universo (red): velas 1h y funding -> datos/A, datos/B, manifest.json
.venv/bin/python -m backtest.funding descargar

# 3. Ejecutar (sin red)
.venv/bin/python -m backtest.funding ejecutar --solo-desarrollo  # no toca el 30 % reservado
.venv/bin/python -m backtest.funding ejecutar                    # única pasada del reservado
```

| Ruta | Contenido |
|---|---|
| `config.py` | Reglas, umbrales, costes, capital y criterios (fijados antes de ver resultados) |
| `data.py` | Descarga y validación de Kraken Futures, Kraken spot (pares) y Hyperliquid; CSV locales |
| `universe.py` | Regla fija de universo (volumen diario medio ≥ 10 M USD en 90 días) |
| `download.py` | Orquestación de las dos descargas y carga local |
| `prepare.py` | Ventanas fijas (A: 2026-03-17 → 2026-10-10, 207 días; B: 365 días hasta las 00:00 UTC del día de la descarga), regla de datos completos y alineación horaria |
| `engine.py` | Simulación horaria: señal, entradas/salidas, funding, costes, base, liquidación, reequilibrio |
| `runner.py` | Tramos 70/30, robustez ±20 % (solo desarrollo), criterios y veredicto |
| `report.py` | Informe Markdown y CSV de posiciones |
| `cli.py` | Línea de órdenes |
| `tests/` | Tests sin red (`make test`) |

## Disciplina de validación

- 70 % desarrollo / 30 % reservado por tiempo, dentro de la ventana de cada estrategia.
- El reservado se ejecuta **una sola vez** y desde un commit congelado: `ejecutar` se niega si hay
  cambios sin commit o si ya existe `resultados/reservado_ejecutado.json`, que registra el commit.
- Robustez ±20 % de los umbrales solo sobre desarrollo (un test lo comprueba).
- Sin lookahead: la decisión de la hora `i` usa solo el funding de las 24 horas anteriores, ya
  liquidado (un test altera el futuro y comprueba que no cambia nada del pasado).

## Regla de datos

Fijada antes de ver resultados, por activo y estrategia:

- **Velas de precio**: una vela real en cada hora operable de la ventana, en todas las series del
  activo. Las velas que faltaban en la fuente y se rellenaron en la descarga para mantener la
  serie contigua no cuentan como dato. Con alguna ausente, el activo se excluye.
- **Funding**: las horas sin tasa en cualquiera de las plataformas (en la ventana o en las 24 h de
  calentamiento, contadas una vez) cuentan como funding cero, sin rellenar, también en la media
  de 24 h, si no superan el 0,5 % de las horas de la ventana. Por encima, el activo se excluye.
- El informe lista los excluidos con el motivo y las horas sin funding de cada activo incluido.
  Nunca se acorta la ventana; una estrategia sin activos válidos aparece como «no evaluable».
- **Ventana de A**: del 2026-03-17 00:00 UTC al 2026-10-10 00:00 UTC (fija). `candleSnapshot` de
  Hyperliquid solo sirve las 5000 velas de 1h más recientes y en la descarga ya no tenía las
  primeras del 2026-03-16.

## Supuestos documentados

- Transferencia entre plataformas (A): **3 USD por movimiento**, al empezar cada tramo y en cada
  reequilibrio. El informe muestra el número de transferencias y su coste total en cada estrategia.
- Reequilibrio (A): si una plataforma baja del 50 % de la media de las dos al cierre de una hora.
- **El reequilibrio no es instantáneo**: el importe sale de la plataforma con más capital al cierre
  de la hora `i` y llega al cierre de la hora `i + 2` (2 horas). Mientras viaja no cuenta como
  margen en ninguna plataforma (ni para abrir posiciones ni frente a la liquidación), sí en el
  capital total, y no se lanza otro reequilibrio hasta que llega.
- Reserva del 2 % de cada cuenta para comisiones y funding al dimensionar el nocional.
- Spot de B: índice spot de la API de gráficos de Kraken Futures como aproximación.
