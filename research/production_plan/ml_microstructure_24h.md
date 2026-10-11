# Plan de producción — modelo Kraken Futures de microestructura a 24 h

**Estado actual: NO APTO PARA PRODUCCIÓN.** Este documento describe puertas secuenciales de investigación y validación. No activa órdenes reales ni cambia `src/copybot/`.

## Principios no negociables

1. El modelo propone una clase/score; el motor de riesgo existente conserva la autoridad sobre exposición y órdenes.
2. Fallo, retraso o ambigüedad de datos/modelo significa `FLAT` y ninguna orden nueva.
3. Un backtest positivo no autoriza trading real. La configuración permanece con ejecución real deshabilitada y sin permisos de retirada en API keys.
4. Las reglas, columnas de entrada, horarios, costes, umbrales o parámetros que cambien después de ver resultados generan un nuevo ID y un nuevo registro de experimento.
5. No tocar `src/copybot/` ni resultados de `backtest/` durante la fase de investigación. La integración se propone en un PR separado, revisable y sin merge automático.

## Fase 0 — factibilidad y contrato de datos

**Salida obligatoria:** auditoría de `PF_XBTUSD` y `PF_ETHUSD`, por métrica e intervalo, usando `research/data/kraken_futures_ml_contract.md`.

- Verificar los símbolos contra el catálogo oficial de mercados en el momento de descargar.
- Descargar velas de precio y series agregadas de interés abierto, flujo agresor/CVD, funding, order book/spreads/liquidez y liquidaciones.
- Determinar, no suponer, desde cuándo existe cada serie, resolución real, semántica de timestamp y si la serie es histórica o solo actual.
- Verificar seis años completos de historia común y los umbrales de cobertura del prerregistro.
- Probar paginación, duplicados, gaps, cambios de unidades/signo y reproducibilidad por hashes.
- Si una familia primaria falla, detener el experimento como `not_testable_as_specified`. Un modelo reducido requiere otro ID y prerregistro.

**No empezar a entrenar durante esta fase.** Consultar un endpoint y obtener un HTTP 200 no constituye cobertura verificada.

## Fase 1 — dataset point-in-time y etiquetas

- Construir una muestra diaria por contrato con decisión a las 00:15 UTC y cutoff de features a las 00:00 UTC. Todas las features deben haber estado disponibles antes del cutoff.
- Normalizar los límites reales de los buckets y el instante de publicación. Excluir buckets aún no cerrados.
- Simular entrada con la primera vela trade de un minuto en o después de la decisión y salida en la primera vela disponible en o después de la entrada +24 h; descartar y registrar muestras con desalineación superior a cinco minutos.
- Calcular resultados hipotéticos netos long y short, aplicando fees taker en ambos lados, slippage por lado, y funding con signo validado. Funding imputado debe ajustarse solo con el segmento de entrenamiento y quedar marcado en el ledger.
- La etiqueta primaria es `LONG`, `SHORT` o `FLAT`, con un hurdle fijo de +10 bp de retorno neto. No modificarlo después de observar resultados.
- Mantener por separado datos raw inmutables, dataset transformado, predicciones y ledger de operaciones.

## Fase 2 — entrenamiento reproducible

- Modelo primario único: LightGBM multiclase con configuración del YAML, semilla fija y dependencias bloqueadas.
- Baselines: regresión logística multiclase con las mismas features, clase mayoritaria, efectivo, buy-and-hold equiponderado BTC/ETH y SMA200 predefinida.
- Todo escalado, clipping, transformación, imputación o normalización se ajusta dentro del periodo de entrenamiento. No entrenar preprocesadores sobre el dataset completo.
- Calibrar probabilidades multiclase en una ventana temporal posterior, de 6 meses, separada del ajuste del clasificador. Reportar Brier multiclase, log loss, curvas de calibración y calibración por clase.
- Umbrales fijos: operar largo si `P(LONG) >= 0.70`, corto si `P(SHORT) >= 0.70`; en cualquier otro caso, `FLAT`. Umbrales 0.65 y 0.75 son únicamente sensibilidades y no sustituyen el primario.
- Guardar parámetros, versiones, SHA-256, predicciones fuera de muestra, seeds, configuración de costes y todos los intentos de experimento. No hacer grid search libre.

## Fase 3 — evaluación histórica y decisión

- Validación expansiva walk-forward en orden temporal; mínimo tres folds OOS de seis meses, cada uno precedido por una ventana temporal de calibración de seis meses.
- Purgar del entrenamiento las muestras cuyos intervalos de etiqueta se solapen con el test y aplicar un embargo de 24 horas tras el fin de las etiquetas.
- Comparar con estrategias aleatorias emparejadas por ventana, número de entradas, frecuencia long/short, exposición bruta media y horizonte; guardar 1.000 simulaciones y sus semillas. No escoger la mejor simulación aleatoria.
- Incertidumbre mediante moving-block bootstrap emparejado por fecha para BTC y ETH, 10.000 réplicas, bloque primario de 20 días y sensibilidad de 10/40 días.
- Reportar resultados combinados, por contrato y por fold; no ocultar periodos negativos ni costes imputados. Informar el número total de pruebas y un ajuste por selección múltiple/Deflated Sharpe cuando proceda.
- El bloque histórico de 365 días solo es confirmatorio si no fue inspeccionado ni influyó en decisiones de diseño. Si existe contaminación, etiquetarlo y no llamarlo “holdout virgen”.

### Criterio de pase histórico

Deben cumplirse simultáneamente todos los criterios primarios del YAML: retorno anualizado neto positivo, límite inferior del IC 95% por encima de cero, rendimiento igual o superior al percentil 90 de las señales aleatorias emparejadas, Sharpe superior al buy-and-hold equiponderado, drawdown máximo menor del 15%, robustez bajo costes adversos y volumen mínimo de operaciones por contrato. En otro caso el resultado será rechazado o inconcluso, no “casi aprobado”.

## Fase 4 — shadow mode sin órdenes

Duración mínima: 30 días consecutivos tras congelar un artefacto candidato.

- Ejecutar el pipeline en el entorno de destino pero sin crear, modificar ni cancelar órdenes.
- Comparar las features calculadas en servidor con el pipeline de referencia; conservar discrepancias por campo y timestamp.
- Registrar frescura de fuentes, llamadas fallidas, latencia, predicción y probabilidades, versión de modelo, hash de feature schema y causa de `FLAT`.
- Aprobar la fase solo si el dataset en vivo replica el contrato point-in-time, no hay fuga de datos y todos los fallos provocan abstención segura. Cualquier cambio reinicia el periodo de shadow con una nueva versión.

## Fase 5 — paper trading

Mínimo seis meses y al menos 50 operaciones cerradas; si no se alcanzan ambas condiciones, extender hasta 12 meses o declarar inconcluso. No usar saldo real ni permitir que el modelo envíe órdenes de producción.

- Simular ejecución conservadora de taker; no contar fills de maker no verificables.
- Registrar bid/ask observado, precio teórico, precio simulado, slippage, fees, funding, latencia, rechazo y posición reconciliada.
- Comparar resultados paper con expectativas OOS, coste previsto vs realizado/simulado, tasa de señales, deriva de features y calibración.
- Si coste/latencia o errores operativos invalidan la simulación, suspender la validación; no ajustar el backtest para hacerla coincidir.

## Fase 6 — decisión de despliegue, no automatización de live

Un informe humano debe revisar: cobertura de datos, reproducibilidad, resultados OOS, costes adversos, incertidumbre, operaciones paper, discrepancias y controles operativos. Incluso si todo pasa, este plan **no cambia `live_trading: false` ni constituye permiso para operar con dinero real**. Cualquier propuesta futura de live requiere autorización explícita aparte, revisión del código/riesgo y configuración separada.

## Contrato de inferencia en runtime

El servicio de inferencia debe recibir un vector con `schema_version`, `feature_as_of_utc`, `source_watermark_utc`, `contract`, `model_version` y valores de las columnas ordenadas por un manifiesto fijado. Debe devolver `P_SHORT`, `P_FLAT`, `P_LONG`, `decision`, `model_hash`, `feature_hash`, `decision_timestamp_utc` y `abstention_reason`.

Abstenerse y no mandar órdenes si ocurre cualquiera de estos casos:

- feature ausente, no finita, fuera de rango validado o con timestamp posterior al cutoff;
- datos retrasados más de 15 minutos, endpoint incompleto, reloj fuera de tolerancia o esquema distinto;
- artefacto/model hash/dependencias que no coinciden con el manifiesto aprobado;
- probabilidades no finitas, negativas, no suman aproximadamente uno o clase/orden de columnas desconocido;
- contrato no reconocido, mercado no disponible, conciliación de posición incierta, rechazo inexplicado o circuit breaker/STOP activo.

La señal nunca puede saltarse las validaciones existentes en `planner.py`/ejecutor. Las reglas de tamaño son el mínimo entre las restricciones del modelo y los límites de riesgo ya configurados; esta capa no puede relajar límites existentes. Para shadow/paper, límite de exposición bruta simulada de 1.0x total, máximo 0.5x por activo, target de volatilidad anualizada del 10% por activo y drawdown de parada del 15%; estos límites son adicionales y no sustituyen los controles existentes.

## Artefactos, seguridad y rollback

- Empaquetar código de features, modelo, calibrador, orden exacto de columnas, metadatos de entrenamiento, hashes de datos, configuración de costes, métrica OOS y lockfile de dependencias como una versión inmutable.
- `joblib` solo se carga para artefactos generados por el pipeline propio y verificados con hash; no cargar archivos de origen desconocido.
- Despliegue blue/green o versión canaria en modo shadow. Rollback inmediato a `FLAT` si hay incompatibilidad, drift severo o errores de datos.
- No introducir secretos/API keys en el artefacto o repositorio. API key separada y de mínimo privilegio; no permisos de retirada. Mantener `STOP`, límites de frecuencia/notional y reconciliación de fills existentes.
- Cualquier incidente, cambio de esquema, cambio de fee tier, modificación de features, retraining o cambio de librerías crea versión trazable; para modificar la hipótesis se requiere nuevo `experiment_id`.

## Checklist de salida antes del PR

- [ ] Contrato de datos rellenado con cobertura real y hashes.
- [ ] Prerregistro YAML revisado y congelado.
- [ ] Tests de timestamp/no-lookahead/labels/signo funding.
- [ ] Reproducción determinista en entorno limpio.
- [ ] Reportes OOS completos y ledger de operaciones.
- [ ] Contraste contra baselines, 1.000 randoms e IC por bloques.
- [ ] `make check` ejecutado en el checkout de GitHub y resultados adjuntos.
- [ ] El diff solo añade `research/` o, en PR distinto, la integración autorizada; no cambia `src/copybot/` ni `backtest/`.
- [ ] PR abierto, no fusionado; live permanece deshabilitado.
