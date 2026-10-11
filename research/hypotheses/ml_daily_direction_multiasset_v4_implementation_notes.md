# Notas de implementación de ml-daily-direction-multiasset-v4

Interpretaciones de puntos que el YAML congelado (SHA-256 3440417ea3bd65b5662b90b701bc4a07548072f79f361f1597aa4d3840760196) no resuelve de forma explícita. Se fijan antes de calcular ningún precio, retorno, señal o resultado. No modifican la hipótesis ni los criterios; solo eliminan ambigüedades de implementación. Cualquier nota nueva se añade al final con fecha y queda registrada en decisions/decision_log.md.

## N1 (2026-10-10): etiqueta cuando la ventana de salida no es válida

- Una fila tiene etiqueta solo si son válidas (al menos 45 minutos con operaciones en [00:15, 01:15) UTC) tanto su ventana de entrada (día siguiente a la fecha de la fila) como su ventana de salida (dos días después).
- Si la ventana de salida no es válida, la fila no tiene etiqueta: no entra en entrenamiento, calibración ni cálculo de m.
- Para el libro de operaciones se mantiene lo que dice el YAML: una posición abierta cuya ventana de salida no es válida sale con el primer minuto con operaciones posterior a las 01:15 UTC y se registra como salida aplazada.
- Motivo: la etiqueta se define de proxy de TWAP a proxy de TWAP; sin un proxy válido en la salida la etiqueta no existe. Es la lectura más estricta y no usa precios fuera de la definición.

## N2 (2026-10-10): alcance efectivo del universo

- Se aplica la regla congelada sin cambios. Con la cobertura medida (data/audit/execution_window_daily.csv), ZEC y NEAR quedan prácticamente sin filas etiquetadas y el entrenamiento inicial se compone sobre todo de BTC y ETH.
- ZEC y NEAR no se sustituyen ni se eliminan de la lista: siguen en el universo y simplemente generan muy pocas filas y operaciones. Sus resultados individuales se marcan como sin evidencia suficiente.
- Todos los informes indican el número de filas etiquetadas y de operaciones por activo y partición, para que el alcance real sea visible junto a cualquier resultado.
- Limitación declarada: en los activos poco líquidos, las filas etiquetadas son una selección no aleatoria de días con actividad en las ventanas de entrada y salida.

## N3 (2026-10-10): definición exacta de las variables

Fijado antes de construir el dataset real. El código está en research/pipeline_v4/features.py.

- Fecha de fila d: la vela diaria de Kraken con inicio en d 00:00 UTC; cierra a las 00:00 de d+1, que es el corte de variables. Los huecos del calendario quedan como NaN; nunca se rellenan.
- ret_kd: log(cierre_d / cierre_{d-k}) para k = 1, 3, 7, 14, 30, 90 días de calendario.
- vol_7d y vol_30d: desviación típica de los retornos logarítmicos diarios en esas ventanas, con al menos el 80 % de los días disponibles; sin anualizar. vol_ratio_7_30 = vol_7d / vol_30d.
- range_1d: (máximo - mínimo) / cierre del día d.
- dist_smaN (N = 20, 50, 200): log(cierre / media móvil simple de N días) / vol_30d. La media exige al menos el 95 % de los cierres de la ventana.
- volume_z30: z-score de log(1 + volumen) del día d frente a los 30 días anteriores (d-30 a d-1), con al menos el 80 % disponible. volume_chg7: log(media de volumen de 7 días / la de los 7 días anteriores), con al menos 6 de 7.
- Transversales, solo entre activos elegibles ese día: xs_rank_ret7 y xs_rank_ret30 (rango percentil), xs_dispersion_ret7 (desviación típica de ret_7d, con al menos 3 activos), rel_btc_ret7 y rel_btc_ret30 (retorno del activo menos el de BTC).
- Factor de mercado: btc_ret_1d, btc_ret_7d, btc_ret_30d, btc_vol_7d, btc_vol_30d y btc_range_1d (como medida de volatilidad de 1 día).
- weekday: día de la semana de la fecha de fila.
- Total: 27 variables, por debajo del máximo de 40.
- adv30_usd (media de 30 días de volumen por cierre) se calcula solo para el slippage del coste de decisión; no es variable del modelo.
- Retorno de la etiqueta: simple, proxy de salida / proxy de entrada - 1. Corto: el negativo del retorno largo.

## N4 (2026-10-10): detalles del modelo, la calibración y el comparador logit

Fijado antes de ejecutar ningún modelo con datos reales. Código en research/pipeline_v4/ (models.py, calibration.py, policy.py, walkforward.py).

- LightGBM: parámetros congelados del YAML con la API nativa (`lightgbm.train`, 300 rondas). Se añaden `n_jobs=1` y `force_row_wise=True` solo por reproducibilidad. Los valores ausentes se dejan como NaN (LightGBM los trata de forma nativa). Clases en orden fijo SHORT, FLAT, LONG.
- Purga en la calibración: las predicciones prequential y la estimación de m solo usan filas de la ventana de calibración cuya etiqueta vence antes del inicio del bloque (la misma regla de purga con L = inicio del bloque). Así se excluyen los dos últimos días de la ventana, cuya etiqueta todavía no se conoce al empezar el bloque.
- Temperatura: un único parámetro T que minimiza el log loss multiclase sobre las predicciones prequential, buscado en log(T) entre -3 y 3 (T entre 0,05 y 20).
- Soporte mínimo: 30 etiquetas reales por clase entre las filas de la ventana con etiqueta vencida, sumando los activos. Si no se cumple, o si alguna subventana no tiene las tres clases para entrenar, el bloque es no calibrable y todas sus decisiones son FLAT.
- Decisión: EV_long = sum_c P_c * m_c_long - coste; EV_short igual con m_c_short = -m_c_long. LONG si EV_long > 0,5 * coste y EV_long >= EV_short; SHORT si EV_short > 0,5 * coste y EV_short > EV_long; si no, FLAT. Con coste ausente, FLAT.
- Las predicciones se generan para todas las filas elegibles del bloque, tengan o no etiqueta; si la ventana de entrada no es válida, la parte 3 no abre posición.
- Regresión logística (comparador): multinomial de scikit-learn, C = 1, lbfgs, hasta 2000 iteraciones; imputación por mediana y estandarización ajustadas solo con las filas de entrenamiento de cada ajuste. Sigue exactamente el mismo protocolo de entrenamiento, calibración, m y decisión que LightGBM.
- Holdout: el código no lo ejecuta salvo con `--holdout`, la presencia de data/processed/v4/HOLDOUT_UNLOCK y ninguna ejecución previa registrada en holdout_runs.jsonl.
- Versiones: numpy 2.2.6, pandas 2.3.3, scipy 1.15.3, scikit-learn 1.7.2, lightgbm 4.7.0 (research/pipeline_v4/requirements.txt).

## N5 (2026-10-10): simulación, comparadores y estadísticos de la parte 3

Fijado antes de ejecutar ninguna evaluación con datos reales, tras una revisión externa que corrigió una primera propuesta (rebalanceo gratuito, solapamiento con el holdout, salida aplazada sin límite y varianza del Deflated Sharpe sin justificar). Código en research/pipeline_v4/ (prep_marks.py, simulate.py, stats.py, evaluate.py).

1. Periodos sin solapamiento. Ningún retorno de un periodo depende de precios posteriores a su fin E (00:00 UTC). Walk-forward: filas de decisión [2023-01-01, 2025-06-28], último punto de valoración el 2025-06-30 (precio como muy tarde a las 02:15 de ese día) y E = 2025-07-01. Holdout: filas [2025-07-01, 2026-06-28], último punto el 2026-06-30 y E = 2026-07-01. Las filas del 29 y 30 de junio de cada periodo no generan señales porque su horizonte cruzaría E. Al final de cada periodo todas las posiciones se cierran en el último punto de valoración, con sus costes de salida. El código comprueba que ningún instante de valoración usado es posterior o igual a E. La serie final concatena las dos series, cada una calculada por separado.
2. Capital y unidades. 550 USD de referencia para dimensionar y normalizar; no se reinvierte el P&L. Al abrir, peso = mín(20 %, 10 % / volatilidad anualizada de 30 días con datos hasta la fila). Mientras la dirección se mantiene se conservan las unidades: el peso de mercado fluctúa y no hay reequilibrio. Cambio de dirección: cierre y apertura, con sus costes. Si la exposición bruta supera el 100 %, todas las posiciones se reducen en proporción, con orden y coste. Ningún reequilibrio es gratuito.
3. Precios. mark = proxy de TWAP de [00:15, 01:15) si es válido; si no, cierre del primer minuto con operaciones con inicio en [01:15, 02:15) (espera máxima de 60 minutos). Si tampoco existe, el día no tiene precio válido: no se rellena, no se opera ni se revalora, la posición abierta se mantiene hasta el siguiente precio válido y el caso se cuenta como valoración aplazada en el informe. Las aperturas exigen proxy válido; si no lo hay, la entrada no se ejecuta y se cuenta. Los ficheros de Kraken solo contienen minutos con operaciones; un minuto ausente significa que no hubo operaciones. Todos estos precios son proxies de ejecución, no ejecuciones reales garantizadas.
4. Costes. Por lado: comisión taker 0,05 % más slippage con el nocional realmente negociado, sobre ese nocional, en cada orden (aperturas, cierres, giros, reducciones por exposición y rebalanceos).
5. Cartera equiponderada (comparador "buy-and-hold equiponderado del universo disponible cada día"). Como el YAML dice "cada día", se define como cartera larga equiponderada entre los activos elegibles, rebalanceada cada día al peso 1/N con sus costes reales de rotación. Se denomina cartera equiponderada con rebalanceo diario, no buy-and-hold puro.
6. Momentum y SMA200: signo del retorno de 30 días y signo de la distancia a la SMA200 de la fila, con las mismas reglas de apertura, unidades, precios y costes que el modelo.
7. Señales aleatorias emparejadas: 1.000 simulaciones. En cada activo y cada bloque, la secuencia de acciones del modelo y sus pesos de apertura se desplazan juntos un número aleatorio de días (desplazamiento circular) sobre las filas elegibles. Se conservan rachas (salvo en el punto de corte), proporción LONG/SHORT y pesos, y se rompe la sincronización con el mercado. Órdenes y costes se recalculan al simular. El percentil del modelo se mide sobre el rendimiento neto anualizado.
8. Deflated Sharpe. Fórmula de Bailey y López de Prado (2014), implementada en stats.deflated_sharpe con Sharpe diario sin anualizar, asimetría y curtosis de la serie: SR0 = sqrt(V) * ((1 - γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N e))) y probabilidad = Φ((SR - SR0) sqrt(T - 1) / sqrt(1 - γ3 SR + (γ4 - 1)/4 SR²)). N = 51 según research/trials_inventory.csv: 43 ensayos evaluados (copy trading, 20 + 20 variantes de 4h, funding A y B), 2 prerregistrados no ejecutados que el YAML exige contar (SMA200 diario y microestructura) y 6 variantes de este experimento (principal, logit, márgenes 0 y 1, etiquetas de 0 y 20 pb). La definición estándar usa la varianza del Sharpe entre ensayos, pero no se conservan los Sharpe de los 45 ensayos anteriores. Se usa una adaptación conservadora, etiquetada como tal: V = máximo de (a) la varianza del Sharpe diario entre las 6 variantes de la v4, (b) la varianza entre las 1.000 señales aleatorias emparejadas y (c) 1/(T - 1). El umbral de 0,95 no cambia.
9. Variantes de sensibilidad: márgenes 0 y 1 se recalculan desde los valores esperados del modelo principal; las etiquetas de 0 y 20 pb se reetiquetan desde el retorno bruto y el coste ya calculados y se reentrenan con el mismo protocolo. Solo se informan y entran en el cálculo de V; no sustituyen al modelo principal.
10. "También con costes adversos" (texto de la hipótesis) se mide con el multiplicador de costes de equilibrio, P25 >= 1,5, como establece la lista de criterios.
11. Alpha: MCO del retorno neto diario del modelo sobre el retorno de BTC (entre puntos de valoración) y el retorno neto del momentum, con errores de Newey-West de 10 retardos y p unilateral con aproximación normal.
12. Concentración: si el beneficio neto total es <= 0, los criterios de año y activo se dan por no cumplidos. Una operación es una posición abierta; todas se cierran como muy tarde al final de su periodo.
13. Los criterios de aprobación del YAML no cambian.
