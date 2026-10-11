# Research

Esta capa separa la investigación cuantitativa de `src/copybot/` y de los resultados existentes en `backtest/`. Ningún resultado de investigación habilita trading real.

## Prerregistros

- `hypotheses/trend_daily_sma200.yaml`: señal direccional diaria SMA200 para evaluación de referencia.
- `hypotheses/ml_microstructure_24h.yaml`: modelo multiclase de microestructura para Kraken Futures con clases `SHORT`, `FLAT`, `LONG`, objetivo neto de costes y criterios de decisión congelados. El estado es `preregistered_design_pending_data_gate`; aún no hay entrenamiento ni resultados.

## Documentos de operación

- `data/manifest.json`: disponibilidad documental de fuentes del plan original; no equivale a auditoría fila por fila.
- `data/kraken_futures_ml_contract.md`: contrato de datos y controles exigidos para las cinco familias de variables.
- `production_plan/ml_microstructure_24h.md`: puertas secuenciales desde datos hasta shadow y paper; no autoriza live.
- `experiments.csv`: inventario de experimentos previos según los informes resumidos disponibles. El resultado del universo volátil requiere contraste con los informes primarios porque no se pudo verificar la rama fuente como rama publicada.
- `decisions/decision_log.md`: registro de decisiones y limitaciones.

## Limitaciones actuales

- No se han descargado y auditado en este artefacto los históricos completos de Kraken Futures para `PF_XBTUSD` y `PF_ETHUSD`.
- No se han medido aquí cobertura real, gaps, duplicados, semántica de buckets ni frescura de cada variable.
- No se ha entrenado LightGBM, calculado etiquetas, backtesteado la nueva hipótesis ni ejecutado `make check` sobre el checkout de GitHub.
- GitHub devolvió `403 Resource not accessible by integration` al intentar crear la rama `research/week1-preregister`; por tanto, esta capa no se declara publicada ni tiene PR abierto.

## Regla de cambios

Todo cambio de etiqueta, universo, coste, fuente, familia de variables, umbral, parámetros principales o protocolo de validación requiere nuevo `experiment_id`. No se permite rescatar una hipótesis fallida eligiendo después la mejor variante.
