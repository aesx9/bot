# Decision log — Semana 1

## Expectativa previa (registrada antes de evaluar la hipótesis)

Lo más probable es que la hipótesis termine **rechazada o inconclusa**. Eso será un resultado válido: el objetivo es averiguar si existe evidencia reproducible de ventaja neta, no encontrar una curva atractiva.

Cualquier cambio de regla, universo, tamaño, coste, ventana o criterio de aceptación será un experimento nuevo con un identificador nuevo. No se rescatará retrospectivamente una hipótesis negativa ajustando parámetros.

Ningún resultado histórico ni periodo de paper habilitará automáticamente el modo live. El modo live permanece desactivado.

## Decisiones de la Semana 1

- Universo fijo: BTC y ETH por liquidez, continuidad y simplicidad; no se afirma que esto elimine todo sesgo.
- Se separa Fase A (señal en spot) de Fase B (posibilidad de monetización en perpetuos Kraken).
- No se calculan señales, SMA, retornos, drawdown, Sharpe, bootstrap ni simulaciones aleatorias en esta fase.
- No se modifica `src/copybot/` ni ningún resultado existente en `backtest/`.
- El archivo `data/manifest.json` registra disponibilidad documental, formatos y controles pendientes. Los controles fila a fila no deben considerarse aprobados hasta inspeccionar los archivos originales y sus hashes.
- La rama `claude/backtest-universo-volatil` no se debe dar por fusionada ni por disponible sin confirmar su ref GitHub y su PR asociado. Si no existe, los valores de esa hipótesis deben contrastarse con el informe original antes de usarlos como fuente primaria.

## Criterios de parada

1. Si no se puede fijar una fuente histórica con timestamps, zona horaria y huecos interpretables, detener la evaluación.
2. Si el resultado neto de Fase A es negativo o el drawdown máximo es >= 15 %, rechazar.
3. Si la rentabilidad positiva es incierta o no supera el percentil 90 de señales aleatorias comparables, declarar inconcluso.
4. Solo declarar prometedora si se cumplen todos los criterios preregistrados, incluidos los costes adversos.
5. Si Fase B carece de funding verificable o de alineación temporal fiable, declarar inconclusa.
6. El resultado nunca habilita live.

## 2026-10-10: Diseño ml-daily-direction-multiasset-v1

- El usuario pide un modelo de ML que aprenda patrones históricos y decida long/short. Se diseña en diario, con datos públicos gratuitos y universo fijo: BTC, ETH, SOL, ZEC, XRP, HYPE, NEAR, SUI, ADA.
- Estado: borrador de diseño, aún no prerregistrado. Pasa a prerregistrado solo cuando el usuario lo revise y se congele sin cambios.
- Universo elegido hoy con conocimiento de rendimientos recientes: sesgo de selección reconocido. Se mitiga con universo congelado, entrada por fecha de listado, resultados por activo y subconjunto BTC/ETH/XRP/ADA.
- Benchmark principal añadido: momentum temporal simple. Si el ML no lo supera, no aporta valor.
- Apalancamiento máximo 1x durante toda la investigación.
- Comparaciones múltiples: Deflated Sharpe Ratio contando todos los ensayos previos de experiments.csv.
- Pendiente: añadir la fila correspondiente a experiments.csv (fuera de las carpetas conectadas).

## 2026-10-10: Diseño ml-daily-direction-multiasset-v1, versión 2

- Revisión de una crítica externa punto por punto con el usuario. Se descartan tres objeciones (etiquetas solapadas, redundancia de la fuerza relativa, "FLAT rentable") y se incorporan las válidas.
- Universo principal: dinámico point-in-time (top 10 por volumen de 30 días, recalculado mensualmente). Condición: solo es principal si se verifica que el archivo OHLCVT de Kraken incluye pares deslistados. Si falla, se usa el universo fijo de 9 activos con el sesgo declarado.
- Umbral 0.55 sustituido por regla de valor esperado; los retornos por clase se estiman solo en ventanas de calibración, nunca con filas de entrenamiento. Fórmula congelada antes del holdout.
- LightGBM debe superar al logit en walk-forward; si no, se registra que la complejidad no compensa, sin sustituir el modelo.
- Costes: multiplicador de equilibrio con distribución bootstrap (P25 >= 1.5) y slippage dependiente del volumen.
- Concentración: límites por año y por decil de liquidez (universo dinámico) o por activo (universo fijo).
- Paper trading de 6 a 24 meses con revisiones fijas y barreras de alpha spending, para evitar alargarlo hasta que salga bien.
- Estado: diseño v2, no prerregistrado. Siguiente paso: verificación de pares deslistados en Kraken.

## 2026-10-10: Puerta del universo dinámico (pares deslistados en Kraken)

Qué se comprobó:
- Artículo oficial de Kraken sobre el OHLCVT descargable: describe "each pair from its first trade on Kraken through 30 June 2026" y no dice si incluye pares deslistados. La documentación no resuelve la pregunta en ningún sentido.
- Una fuente secundaria aportada por el usuario afirma que los pares deslistados no aparecen en el archivo. No verificada contra los datos.
- Prueba directa: no se pudo ejecutar. El proxy de este entorno devuelve 403 para assets.kraken.com, tanto en la nube como en la VM local.
- CoinGecko (opción de fuente agregada): la API gratuita (Demo) limita el histórico a los últimos 365 días según su documentación. No sirve para un universo dinámico desde 2018 sin pagar.

Herramienta preparada:
- `data/check_kraken_delisted.py`: lee el índice del ZIP completo 2026Q2 y la última vela diaria de cada par mediante peticiones HTTP Range, sin descargar el archivo. Criterio objetivo: pares cuya última vela es anterior a 2026-04-01. Probado con un ZIP sintético partido en cinco partes; no probado contra el archivo real.
- Se prefiere este criterio a una lista de canarios: algunos candidatos (por ejemplo XMR) siguen cotizando fuera del EEE y no demostrarían nada.

Opciones evaluadas:
1. Universo dinámico con fuente agregada. Descartada por ahora: la vía gratuita revisada no cubre el periodo; además, el volumen agregado incluye wash trading (sobre todo antes de 2020) y activos que no se podían operar en Kraken, así que el universo no sería operable.
2. Universo dinámico solo desde la fecha en que el archivo de Kraken conserve pares deslistados. Válida si el script lo demuestra; acota el walk-forward.
3. Universo fijo de 9 activos con el sesgo declarado como no resuelto.

Decisión:
- Hasta ejecutar el script, el principal provisional es la opción 3.
- Si el script encuentra pares deslistados con historial suficiente, se evalúa la opción 2 y se registra aquí la fecha de inicio. Si no los encuentra, la opción 3 queda como definitiva para esta versión.
- No se completa el universo con otras fuentes sin un experiment_id nuevo.

## 2026-10-10: Regla congelada para cerrar la puerta del universo dinámico (antes de ejecutar el script)

Escrita antes de ver ningún resultado del archivo real. La salida del script solo aplica esta regla.

- Definición medida: "par con última vela antigua" es un par sin velas diarias en los 90 días previos al final del archivo (2026-06-30). No equivale a "deslistado confirmado": también incluye pares sin operaciones. Ambos casos demuestran que el archivo conserva historial de pares que ya no cotizan; la confirmación de deslistado contra anuncios de Kraken es opcional y no cambia la regla.
- Año de inicio S: el primer año a partir del cual todos los años naturales hasta 2025 tienen al menos un par con última vela antigua en ese año. Se exige continuidad año a año porque un único par muy antiguo no demuestra que el archivo conserve los deslistados de los años siguientes.
- Opción 2 (universo dinámico desde el 1 de enero de S) si se cumplen las tres condiciones: al menos 3 pares con última vela antigua; S existe; S <= 2022, lo que deja al menos 4 años de historial spot hasta el final del archivo.
- Opción 3 (universo fijo de 9 activos, sesgo declarado como no resuelto) en cualquier otro caso, incluido que el servidor no acepte peticiones Range y no se pueda completar la lectura.
- El funding no entra en este requisito: en la fase A la señal usa precios spot, y el funding es NaN antes de existir el perpetuo, como ya prevé el diseño.
- Limitación aceptada: que el archivo conserve pares deslistados no prueba que los conserve todos. Esa completitud no se puede verificar sin el historial de listados de Kraken y se declara en cualquier conclusión.
- El script `data/check_kraken_delisted.py` imprime el recuento por año, el año S y el veredicto según esta regla. Probado con un ZIP sintético; pendiente de ejecutar contra el archivo real.

## 2026-10-10: Funding como variable del modelo (decidido antes de ver datos)

- Decisión: el funding queda excluido como variable del modelo principal en ambos universos. Solo se usa como coste real en la fase B.
- Motivo: el funding solo existe desde que hay perpetuo. Como variable ausente antes de esa fecha, su ausencia marcaría la época y la antigüedad del activo, y el modelo podría aprender efectos que no se repiten en el test, donde siempre hay funding. Exigirlo como condición de entrada sesgaría el universo hacia activos veteranos.
- Excluirlo en los dos universos mantiene comparables los pipelines dinámico y fijo.
- Un modelo con funding como variable sería un experiment_id nuevo, entrenado solo en el periodo con perpetuos.
- Añadido a la regla de la puerta del universo: si el veredicto es la opción 2, la conservación de pares deslistados en los primeros años de S se registra como hipótesis favorable, no como hecho. La presencia de pares antiguos prueba que el archivo no purga sistemáticamente, pero no que los conserve todos.

## 2026-10-10: Pre-check opcional con la API pública de Kraken (antes del script del ZIP)

- Propuesta externa: consultar AssetPairs, OHLC y Trades?since=0 para pares deslistados conocidos (ICN, DAO) usando los ids crudos de AssetPairs, nunca símbolos unificados.
- Regla de interpretación, asimétrica y fijada antes de ejecutarlo:
  - Si la API devuelve datos de un par deslistado: indicio favorable. No sustituye al script del ZIP, que sigue siendo la prueba que decide.
  - Si la API no reconoce el par (por ejemplo "Unknown asset pair"): no informa sobre el ZIP. La API pública sirve mercados activos y el archivo es un producto distinto, así que un fallo aquí no permite saltarse el script ni elegir la opción 3.
- Reconstruir el universo dinámico a partir de Trades (1.000 operaciones por petición, con límites de frecuencia, cientos de pares y años de historial) no se considera viable como fuente principal. Si algún día se usa, se registra como fuente distinta con su propio coste de construcción.
- Plan de pago, solo si en el futuro hay capital real en juego: proveedor comercial con OHLCV histórico de Kraken incluyendo pares deslistados. Cobertura y precio no verificados.

## 2026-10-10: Resultado del pre-check con la API pública

- OHLC?pair=ICNXBT&interval=1440 devuelve "EQuery:Unknown asset pair" (ejecutado por el usuario en su navegador).
- AssetPairs lista pares activos y pares en retirada con status cancel_only o post_only (por ejemplo ACA, ACX, AI3); los pares ya retirados no aparecen.
- Según la regla asimétrica fijada antes, este resultado no informa sobre el archivo OHLCVT. La decisión sigue pendiente del script data/check_kraken_delisted.py.

## 2026-10-10: Resultado de la puerta del universo dinámico

Ejecución:
- `data/check_kraken_delisted.py` ejecutado el 2026-10-10 de 16:41:24 a 16:44:18 (hora local, UTC+2), con cabeceras de navegador tras un primer intento fallido con 403 por el User-Agent por defecto de urllib. Criterio, STALE_DAYS (90) y umbral de 2022 sin cambios.
- Archivo: Kraken_OHLCVT_Full_2026Q2, 8,97 GB en 5 partes, 12.037 ficheros. Lectura completa mediante peticiones Range, sin errores.
- MANIFEST.json: product ohlcvt, schema_version 1, release r1, cobertura 2013-09-11 a 2026-06-30, 1.715 pares, 12.035 ficheros, 561.943.973 filas, generado el 2026-08-17.
- Pares con velas diarias leídos: 1.713 (el manifiesto declara 1.715 pares; diferencia de 2 no investigada). `data/kraken_pairs_last_candle.csv` tiene 1.713 filas.

Resultado:
- Pares con última vela antigua (anterior a 2026-04-01): 314 de 1.713.
- Recuento por año de la última vela: 2013 a 2021: 0; 2022: 34; 2023: 25; 2024: 57; 2025: 198; 2026: 0.
- S = 2022.
- Veredicto según la regla congelada: opción 2, universo dinámico desde el 1 de enero de 2022.

Notas obligatorias:
- "Última vela antigua" significa sin velas diarias en los 90 días previos al final del archivo; no equivale a "deslistado confirmado".
- La conservación de pares deslistados en los primeros años de S es una hipótesis favorable, no un hecho. La presencia de pares antiguos prueba que el archivo no purga sistemáticamente desde 2022, pero no que conserve todos los deslistados.

Observaciones (no cambian el veredicto):
- Ningún par con historial en el archivo termina antes de 2022-09-30, aunque hay 54 pares que empiezan antes de 2018. Es compatible con que el archivo no conserve pares retirados antes de 2022; S = 2022 queda justo en el límite de la regla.
- Las últimas velas antiguas se concentran en cierres de trimestre: 2025-12-31 (105), 2025-06-30 (47), 2024-09-30 (43), 2022-12-31 (28), 2023-12-31 (25, todos los de 2023). Puede reflejar retiradas en lote o la forma en que se construyó el archivo; no se ha comprobado contra anuncios de Kraken.
- Canario ICN: el script lo da como presente, pero coincide con ICNTEUR e ICNTUSD (desde 2025-07-03), que es otro activo. El ICN original no aparece. Canario DAO: ausente. Ambos se retiraron antes de 2022, coherente con la observación anterior.

## 2026-10-10: Corrección metodológica del universo (versión 3)

Decisión: opción 3 principal; universo dinámico desde 2022 secundario. La regla original basada en pares retirados produjo formalmente la opción 2, pero el análisis a nivel de activo mostró que las retiradas de pares cruzados y las migraciones pueden confundirse con desapariciones de activos. Se considera que la regla original no acredita adecuadamente la cobertura histórica del universo. La corrección se realiza antes de evaluar los resultados de la nueva estrategia y se registra como una nueva versión, preservando el prerregistro original. El universo fijo de 9 activos mantiene un posible sesgo de supervivencia, que se declara no resuelto. El universo dinámico desde 2022 se conserva como análisis secundario y no se considera libre de ese sesgo. El control de símbolos se modifica para utilizar coincidencias exactas y una tabla explícita de alias.

Evidencia (data/asset_level_audit.py sobre kraken_pairs_last_candle.csv):
- De los 314 pares con última vela antigua, 127 son cruces retirados de activos que siguen cotizando en otro par.
- 738 activos base; 82 sin ningún par vivo al final del archivo, más 3 candidatos a migración o renombre (FTM, MATIC, RNDR) excluidos del recuento.
- Activos sin par vivo por año de su última vela: 2022: 1; 2023: 3; 2024: 4; 2025: 74. Ninguno antes de 2022, aunque Kraken retiró activos en esos años (por ejemplo ICN y DAO, ausentes del archivo).

Cambios:
- Nueva versión: hypotheses/ml_daily_direction_multiasset_v3.yaml, experiment_id ml-daily-direction-multiasset-v3, con campo lineage que enlaza con la versión anterior.
- hypotheses/ml_daily_direction_multiasset.yaml (experiment_id ml-daily-direction-multiasset-v1, versión 2) se conserva sin editar como registro de la regla original y su resultado formal.
- data/asset_aliases.csv: tabla de migraciones y alias. FTM a S, MATIC a POL y RNDR a RENDER quedan como pendientes de verificar fecha, fuente y continuidad; no se unen historiales hasta entonces. ICN e ICNT se registran como activos distintos.
- data/check_kraken_delisted.py: el canario usa coincidencia exacta del activo base en lugar de prefijo.

Alcance:
- Esta decisión no demuestra que el universo fijo sea representativo ni que el dinámico desde 2022 sea completo. Solo determina qué análisis es principal y qué limitaciones deben quedar explícitas.
- Los resultados de rentabilidad no se usarán para elegir entre universos. No se fusionan resultados ni conclusiones entre versiones.

## 2026-10-10: Cobertura efectiva por activo en v3

- La composición del universo principal se conserva en las nueve monedas originales (BTC, ETH, SOL, ZEC, XRP, HYPE, NEAR, SUI, ADA). Ninguna se sustituye ni se elige por resultados.
- Nuevo bloque effective_coverage en hypotheses/ml_daily_direction_multiasset_v3.yaml, calculado con data/kraken_pairs_last_candle.csv (corte de datos 2026-06-30) antes de calcular ninguna señal ni retorno. Las cifras describen cobertura disponible, no resultados.
- Estado por activo: BTC, ETH, ZEC y XRP elegibles desde 2018-01-01; ADA desde 2019-04-16; SOL desde 2022-01-03 con entrenamiento inicial limitado (363 días); NEAR con evidencia inicial limitada (0 días en entrenamiento inicial, 911 en walk-forward); SUI con evidencia limitada (0 y 225 días).
- HYPE: no elegible ni operable en v3. HYPEUSD tiene 154 días en el archivo, frente a los 565 que exigen las reglas de entrada ya escritas. No es una exclusión permanente: una versión futura puede reevaluarlo con más historial y un experiment_id nuevo. El símbolo se mantiene en la lista.
- Se documenta la construcción de los cinco bloques walk-forward y del holdout (2025-07-01 a 2026-06-30). Solo cuenta como generalización a un activo no visto el primer bloque en que es elegible sin filas suyas en el entrenamiento (NEAR: 2023-01-01 a 2023-06-30; SUI: 2024-07-01 a 2024-12-31). Los bloques posteriores son evaluación walk-forward ordinaria y se informan por separado.
- El límite del 50 % de beneficio por activo no sustituye a la suficiencia estadística. Los resultados por activo se informan con intervalo y número de operaciones, y SUI se marca como evidencia insuficiente para conclusiones individuales.
- La regla original de la puerta del universo y su resultado no se modifican.

Puntos abiertos para la revisión final antes de congelar:
1. Duración y posición de la ventana de calibración dentro de cada bloque. De ella dependen la calibración y los valores m de la regla de valor esperado, y no está fijada.
2. La variable categórica asset_identity: en la generalización inicial, NEAR y SUI llegan con una categoría que el modelo nunca vio. Hay que decidir si se mantiene, y cómo se trata, antes de interpretar esa prueba.
3. Verificar en el código, cuando exista, que el walk-forward respeta la separación temporal, la purga y el embargo tal como están escritos.

## 2026-10-10: Cierre de calibración y asset_identity en v3 (antes de congelar)

Decidido antes de calcular ninguna predicción ni resultado de esta versión. La regla original de selección de universo sigue intacta.

Calibración:
- Ventana de 180 días naturales inmediatamente anterior a cada bloque (fechas UTC, extremo final exclusivo). Fechas en model.calibration.windows; la del holdout es 2025-01-02 a 2025-06-30.
- Predicciones fuera de muestra por método prequential: seis subventanas de 30 días, cada una predicha por un modelo entrenado solo con filas anteriores con horizonte vencido, con la purga y el embargo de 2 días ya fijados en purge_and_embargo_days (no el embargo de 1 barra de la propuesta externa, para no tener dos valores distintos).
- Temperature scaling con un único parámetro para todos los activos, ajustado por log loss. Sin calibradores por activo ni elección del método mirando resultados.
- Si alguna clase tiene menos de 30 observaciones en la ventana o alguna subventana no se puede entrenar, el bloque es no calibrable y la acción es FLAT.
- El modelo de cada bloque se entrena con todo lo anterior al bloque, incluida la ventana de calibración. Cambia lo escrito en v2 ("excluida del ajuste del modelo"); la aproximación se declara en el YAML.

Regla de valor esperado (aclaraciones, sin cambiar la exigencia):
- Los valores m no dependen de las predicciones del modelo, así que la preocupación de v2 por el optimismo dentro de muestra no aplicaba; lo que importa es que solo usen datos anteriores al bloque.
- m se calcula como retorno bruto con todos los activos juntos y el coste de cada activo se resta una sola vez al calcular su EV. El margen de 0,5 veces el coste de ida y vuelta es un requisito adicional, no un segundo descuento.
- No existe umbral de probabilidad en v3; la propuesta externa lo mencionaba, pero la v2 ya lo había sustituido por la regla de valor esperado.

asset_identity:
- Fuera de las variables predictoras del modelo principal. Se conserva como metadato para particiones, reglas de entrada, controles de riesgo e informes por activo.
- Motivo: NEAR y SUI llegarían a su prueba de generalización con una categoría nunca vista. Un modelo con identidad de activo sería otra hipótesis.

Estado: design_v3_ready_for_final_review. Pendiente solo la verificación del código cuando exista (separación temporal, purga y embargo en cada subventana y entre bloques, ninguna etiqueta usada antes de vencer).

## 2026-10-10: Revisión final y congelación de ml-daily-direction-multiasset-v3

Revisión completa del YAML buscando contradicciones, fugas temporales y ambigüedades, sin cambiar decisiones de diseño. Correcciones de redacción aplicadas:
1. Contradicción: la regla de valor esperado llamaba "neto" a m mientras m_estimation lo define como bruto. Ahora dice bruto en ambos sitios.
2. Ambigüedad: la etiqueta hablaba de "apertura de ejecución"; ahora remite al TWAP de timing.label_alignment.
3. Ambigüedad con riesgo de fuga: se define la fecha de fila y la purga. Para cualquier límite L (bloque, holdout o subventana de calibración), solo entran en entrenamiento filas con fecha <= L - 3 días, porque la etiqueta de una fila vence dos días después de su fecha. Se aplica igual en todos los límites.
4. Ambigüedad: el mínimo de 30 se define como 30 etiquetas reales por clase entre las filas con predicción prequential de la ventana, sumando todos los activos elegibles (no 30 por activo). Se aplica también a la estimación de m.
5. Regla de holdout único: si se ejecuta de nuevo o sus resultados se usan para cambiar cualquier decisión, deja de ser confirmatorio y se registra.

Validación de contenido: hypotheses/validate_v3.py (20 comprobaciones: universo exacto, HYPE no elegible ni operable, purga de 2 días en bloques y subventanas, calibración de 180 días con 6 subventanas y temperatura única, fechas de ventanas, m bruto, costes una sola vez, sin umbral de probabilidad, asset_identity fuera de los predictores, apalancamiento 1x, live_trading false). Resultado: VÁLIDO.

Congelación:
- Archivo: hypotheses/ml_daily_direction_multiasset_v3.yaml
- Estado: preregistered
- Fecha: 2026-10-10T15:06:48Z
- SHA-256: 799049993d84eada354e06b3336be3a6cd147a9539111a876c3369d3687524e0
- SHA-256 del validador (hypotheses/validate_v3.py): 211683373f7c6d6028156cb9f45bf61c30368eea4494fdab620be3a58888db22
- live_trading: false.
- Cualquier modificación posterior de la hipótesis requiere un experiment_id nuevo. La versión anterior (hypotheses/ml_daily_direction_multiasset.yaml) sigue intacta.

Pendiente fuera del prerregistro: verificar en el código, cuando exista, la separación temporal, la purga en cada límite y que ninguna etiqueta se use antes de vencer.

## 2026-10-10: v3 sustituida por v4 antes de usar ningún dato

Una revisión externa de la v3 congelada encontró cuatro defectos de especificación que la revisión final propia no detectó. Son defectos reales, no preferencias de diseño:
1. Convención de intervalos incoherente: la calibración decía "extremo final exclusivo" pero listaba fechas finales inclusivas.
2. El precio de ejecución usaba velas de 1 minuto sin fuente de datos de 1 minuto declarada ni regla para minutos sin operaciones.
3. Contrato de costes incompleto: sin tarifa numérica, sin capital de referencia (el slippage depende del nocional) y sin separar los costes de decisión de los contabilizados.
4. Criterios de aprobación no alineados: baselines.rule exigía superar a todos los comparadores, pero pass_requires_all no incluía SMA200 ni la regresión logística, y no fijaba la serie del IC.

Decisión del usuario: la v3 se mantiene intacta con su hash (verificado sin cambios) y queda sustituida por la v4, con experiment_id ml-daily-direction-multiasset-v4. No se ha descargado, entrenado ni evaluado nada con la v3.

Decisiones del usuario incorporadas en la v4:
- Intervalos [inicio, fin) en todo el documento. Ventanas de calibración de 180 días exactos que terminan en el inicio de cada bloque; holdout [2025-07-01, 2026-07-01). Purga expresada como vencimiento de la etiqueta antes del límite (equivale a fecha de fila <= L - 3 días).
- Costes de Kraken Futures en las dos fases, porque solo se operaría en perpetuos y el spot es fuente de precios. Tarifa taker 0,05 % del nivel inicial de Futures, consultada el 2026-10-10 en kraken.com/features/fee-schedule (sin fecha de vigencia en la página). La tarifa taker spot del nivel inicial (0,80 % según la misma página) se registra solo como referencia y no se usa. Antes de calcular resultados se archiva la página con su hash.
- Capital de referencia de la simulación: 550 USD.
- Proxy de TWAP: media de los cierres de las velas de 1 minuto con operaciones en [00:15, 01:15) UTC, válido con al menos 45 de 60 minutos. Si la entrada no es válida no hay operación ni etiqueta; si la salida no es válida se sale con el primer minuto con operaciones posterior y se registra. Se elige 45 en lugar de 60 porque exigir los 60 minutos podría descartar sobre todo los días menos líquidos de las monedas menores, un descarte no aleatorio.
- Fase A: precios spot, cortos sintéticos, costes de Futures, sin funding. Fase B: las mismas posiciones, precios del perpetuo, costes de Futures y funding real. Coste de decisión (etiqueta y filtro EV) separado del coste del libro (solo sobre el nocional negociado en cada cambio de posición).
- pass_requires_all incluye todos los comparadores (efectivo, buy-and-hold, momentum, SMA200, regresión logística, señales aleatorias). La serie de evaluación es la de los cinco bloques walk-forward y el holdout concatenados; el holdout debe además ser positivo por separado. LightGBM frente al logit pasa a ser requisito de aprobación, como ya exigía baselines.rule.

Validación: hypotheses/validate_v4.py, 31 comprobaciones, resultado VÁLIDO. Estado: design_v4_ready_for_final_review, pendiente de confirmación del usuario para congelar.

## 2026-10-10: Congelación de ml-daily-direction-multiasset-v4

Visto bueno explícito del usuario tras la revisión de la v4. La revisión externa posterior resumió el documento sin señalar defectos; sus dos imprecisiones (probabilidad del Deflated Sharpe y selección de posición por valor esperado) eran del resumen, no del YAML, y no motivan cambios.

- Archivo: hypotheses/ml_daily_direction_multiasset_v4.yaml
- Estado: preregistered
- Fecha: 2026-10-10T17:04:59Z
- SHA-256: 3440417ea3bd65b5662b90b701bc4a07548072f79f361f1597aa4d3840760196
- Validador: hypotheses/validate_v4.py, resultado VÁLIDO (31 comprobaciones)
- SHA-256 del validador: 93c2a21052ce999b31084ae1ed665368b4fd09dc5dc56d93950f919da19b9733
- live_trading: false.
- Cualquier modificación posterior requiere un experiment_id nuevo. Las versiones anteriores (v1 versión 2 y v3) siguen intactas.
- Pendiente fuera del prerregistro: archivar la página de tarifas con su hash antes de calcular resultados, y verificar en el código la separación temporal, la purga en cada límite y que ninguna etiqueta se use antes de vencer.

## 2026-10-10: Archivo de tarifas pendiente; discrepancia detectada

- Al intentar archivar la página de tarifas, la versión en español vista por el usuario (www.kraken.com/features/fee-schedule) no muestra una sección de Kraken Futures con tabla por niveles. Solo muestra "Perpetuos": 0,25 % sobre el nocional al abrir y 0,25 % al cerrar, "solo en determinadas zonas geográficas".
- La v4 congelada usa 0,05 % taker (nivel inicial de Kraken Futures), tomado de la misma URL consultada desde otra región.
- Kraken Pro ofrece perpetuos a clientes del EEE (página kraken.com/pro/futures-eea-fee-waiver). Sus términos promocionales (kraken.com/legal/eea-futures-fee-waiver-promotion-terms) remiten a las "tarifas estándar de Kraken" sin dar cifras. La promoción de comisión cero no se considera para el experimento por ser temporal y condicionada.
- Decisión: no se calcula ningún resultado hasta verificar la tarifa taker de perpetuos que aplica a la cuenta del usuario en el EEE. Si difiere de 0,05 %, la v4 no representa el coste real y se registrará una v5 con la tarifa verificada; la v4 no se modifica.

## 2026-10-10: Tarifa verificada en la cuenta del usuario (cierra la discrepancia)

- Evidencia: captura de la ventana "Comisiones" de Kraken Pro en la cuenta del usuario, guardada como data/kraken_fee_tier_account_2026-10-10.png.
- SHA-256 del archivo archivado en data/: 683eeeede09c77c68b536f416eef23a08eda5b2dc93193d613283ed8bd663ec8 (la copia original subida al chat tenía 8a86fa56c9c5b8331ca8fd824731f202f3bb59fd310e5f88662ef6344973c2c3; la transferencia a la carpeta alteró los bytes, así que la referencia es el archivo de data/)
- Tarifas de futuros utilizadas como supuestos de investigación, verificadas contra la evidencia archivada: 0,0200 % maker y 0,0500 % taker. El resto del contenido de la captura (volumen negociado y nivel de comisiones de la cuenta, y tarifas de otros niveles) no es necesario para reproducir el ensayo y no se publica. La captura no se versiona: queda fuera del repositorio, referenciada por su SHA-256.
- La captura se tomó desde la pantalla de un futuro con vencimiento (BTC Futuros 30Oct26). La tabla de comisiones es la del nivel de la cuenta para futuros; se asume que aplica igual a los perpetuos. Si una operación real de perpetuo muestra otra comisión, se registrará.
- Conclusión: la tarifa taker de 0,05 % congelada en la v4 coincide con la que aplica al usuario. La v4 sigue siendo válida; no hace falta una v5. La sección "Perpetuos" al 0,25 % de la página pública corresponde a otro producto y no se usa.
- Con esto queda cumplido el archive_requirement de costs.fee_schedule.

## 2026-10-10: Paso 2, extracción y auditoría de datos (v4)

Ejecución de data/fetch_and_audit_v4.py desde data/, con salida en data/fetch_and_audit_v4.log. No se ha calculado ningún retorno, señal, etiqueta ni resultado de estrategia. El YAML congelado no se ha modificado (SHA-256 sin cambios: 3440417ea3bd65b5662b90b701bc4a07548072f79f361f1597aa4d3840760196).

- Inicio: 2026-10-10T17:21:53Z. Fin de la extracción: 2026-10-10T17:24:10Z. Fin del script (auditoría incluida): 2026-10-10T17:24:37Z. Código de salida 0.
- Fuente: Kraken_OHLCVT_Full_2026Q2 (cinco partes, 8,97 GB, 12.037 ficheros), leída por HTTP Range sin descargar el archivo completo.
- Ficheros extraídos: 17 (MANIFEST.json del archivo y los ficheros _1440 y _1 de XBTUSD, ETHUSD, SOLUSD, ZECUSD, XRPUSD, NEARUSD, SUIUSD y ADAUSD), en data/raw/kraken_ohlcvt_2026Q2 (1.124.798.102 bytes, unos 1,1 GB). Ningún fichero faltante (missing_members vacío). Hashes, tamaños y CRC32 de cada fichero en data/audit/extract_manifest.json.
- Puerta de datos (data/audit/data_gate_v4.json): PASAN los 8 activos (BTC, ETH, SOL, ZEC, XRP, NEAR, SUI, ADA). stop_experiment_BTC_or_ETH_failed: false.
- Incidencias a revisar según el script: ninguna en ningún activo (0 duplicados, 0 saltos no monótonos, 0 velas fuera de la frontera UTC y 0 filas OHLC inválidas, tanto en diario como en 1 minuto).
- Días ausentes en las velas diarias (data/audit/daily_audit.csv; no figuran en issues_to_review): BTC 12, ETH 6, ZEC 1, XRP 1, resto 0. Porcentaje de días válidos: BTC 99,742; ETH 99,849; ZEC 99,972; XRP 99,970; SOL, NEAR, SUI y ADA 100,0.
- Cobertura de la ventana de ejecución [00:15, 01:15) UTC con al menos 45 minutos con operaciones (data/audit/minute_window_coverage.csv), activo-años por debajo del 90 %:
  - BTC: 2013 a 2016 (0,0 a 2,19 %), 2017 (69,04 %), 2018 (81,10 %). Desde 2019, 92,05 % o más.
  - ETH: 2015 a 2016 (0,0 y 1,09 %), 2017 a 2020 (56,16 a 72,68 %). Desde 2021, 95,07 % o más.
  - SOL: 2021 a 2023 (47,95 a 64,93 %). Desde 2024, 98,36 % o más.
  - ZEC: todos los años, de 2016 a 2026 (0,0 a 56,35 %).
  - XRP: 2017 a 2024 (9,86 a 65,57 %). 2025 y 2026, 100 %.
  - NEAR: todos los años, de 2022 a 2026 (1,01 a 24,86 %).
  - SUI: todos los años, de 2023 a 2026 (0,0 a 54,25 %).
  - ADA: 2018 a 2020 (0,0 a 7,38 %), 2022 a 2026 (16,44 a 80,55 %). Solo 2021 supera el 90 % (93,70 %).
- La puerta de datos implementada en el script no incluye la cobertura de la ventana de ejecución; esa cobertura se registra aquí como incidencia a revisar, sin decisión sobre exclusión de activos ni periodos.
- SHA-256:
  - data/audit/extract_manifest.json: 33a76cb9fc60868cd56e816d5bc75ab800f3633a343c3019124db006a2c076a1
  - data/audit/data_gate_v4.json: 7ba8df81554afcb4041f4e5b35712622be81a84adb74bfcf2b74da1915f6f4cb
  - data/fetch_and_audit_v4.py: 1aa7e1fcf808f2b935ec63b5c84b574ed9cb4628e4930a9a58af2bd1296f0a09
- Pendiente: no existe .gitignore en research ni en la carpeta superior; data/raw/ no está ignorado. Se ha propuesto añadirlo, a la espera de aprobación del usuario.

## 2026-10-10: Recolector de datos públicos en el VPS

- VPS OVH (Ubuntu 24.04), acceso por clave SSH. Código en production_plan/collector/, instalado en /opt/kraken-collector con el usuario de sistema collector. Datos en /var/lib/kraken-collector.
- Prueba de humo superada el 2026-10-10 (AssetPairs, instrumentos, tickers, funding histórico, libro de órdenes recortado a 50 niveles, velas de 1 minuto). Primera ejecución de las tres tareas sin errores (31 peticiones, 0 errores). Los 9 perpetuos, incluido PF_HYPEUSD, aparecen en el catálogo. Espacio libre inicial: 38,5 GB.
- Temporizadores activos: diario 00:30 UTC, horario en el minuto 5, cada 5 minutos.
- Objetivo: historial point-in-time de pares y contratos, funding y velas de 1 minuto de perpetuos para la fase B, y dataset prospectivo de microestructura. Solo datos públicos, sin claves de API ni órdenes.
- Estos datos se recogen desde hoy y no forman parte del histórico de la v4; cualquier uso en un experimento se registrará con su propio experiment_id.

## 2026-10-10: Efecto de la cobertura de la ventana de ejecución en las etiquetas (solo recuento, sin retornos)

- Medido a partir de los ficheros de 1 minuto extraídos (data/audit/execution_window_daily.csv, generado con data/audit_execution_window_daily.py): minutos con operaciones en [00:15, 01:15) UTC por activo y día. No se ha calculado ningún precio, retorno ni señal.
- Días elegibles con etiqueta válida, contando como válida una fila solo si son válidas la ventana de entrada (día siguiente a la fila) y la de salida (dos días después), con el umbral de 45 minutos:

| Activo | Entreno inicial | Walk-forward | Holdout |
|---|---|---|---|
| BTC | 1659 de 1826 (90,9 %) | 908 de 912 (99,6 %) | 363 de 365 (99,5 %) |
| ETH | 1256 de 1826 (68,8 %) | 880 de 912 (96,5 %) | 363 de 365 (99,5 %) |
| ZEC | 2 de 1826 (0,1 %) | 3 de 912 (0,3 %) | 151 de 365 (41,4 %) |
| XRP | 304 de 1826 (16,6 %) | 451 de 912 (49,5 %) | 363 de 365 (99,5 %) |
| ADA | 442 de 1356 (32,6 %) | 225 de 912 (24,7 %) | 214 de 365 (58,6 %) |
| SOL | 190 de 363 (52,3 %) | 655 de 912 (71,8 %) | 363 de 365 (99,5 %) |
| NEAR | 0 de 0 | 63 de 911 (6,9 %) | 21 de 365 (5,8 %) |
| SUI | 0 de 0 | 123 de 225 (54,7 %) | 88 de 365 (24,1 %) |

Observaciones (sin decisión todavía):
1. Con la regla congelada, ZEC y NEAR quedan prácticamente sin etiquetas y el entrenamiento inicial se compone sobre todo de BTC y ETH. El universo efectivo es mucho más estrecho que el nominal.
2. La cobertura mide la actividad del spot USD de Kraken a esa hora, no la liquidez del perpetuo donde se operaría. Para la fase A la ventana es una referencia de precio; con órdenes de unos 110 USD la capacidad de ejecución no es el problema.
3. Las filas con etiqueta solo existen en días con actividad suficiente; en los activos poco líquidos es una selección no aleatoria y depende de la actividad del día de salida, que es posterior a la decisión.
4. Ambigüedad de la v4: la regla de etiqueta mide de proxy de TWAP a proxy de TWAP, define qué pasa si la ventana de entrada no es válida, pero no qué pasa con la etiqueta si la ventana de salida no es válida (la salida aplazada solo está definida para el libro). El recuento de arriba asume que la fila no tiene etiqueta.
- SHA-256 de data/audit/execution_window_daily.csv: 21c85cde0ee2ec11c57acb2fe0bd09f5f6e34502d1dbe0fc458d72449788171b

## 2026-10-10: Decisión sobre la cobertura de ejecución: seguir con la v4

- Decisión del usuario: opción 1. Se sigue con la v4 congelada sin crear una v5. La regla de 45 minutos se aplica tal cual.
- La ambigüedad de la etiqueta con ventana de salida no válida se resuelve como "sin etiqueta" (nota N1). El alcance efectivo del universo se documenta en la nota N2. Ambas en hypotheses/ml_daily_direction_multiasset_v4_implementation_notes.md, fijadas antes de calcular ningún precio, retorno o señal.
- SHA-256 de las notas: 8e4fb8fd9ac603952bc764adfdbfb16f1f6b5e23252a3362d1876166adfa3f00
- El YAML de la v4 no se modifica (hash verificado: 3440417ea3bd65b5662b90b701bc4a07548072f79f361f1597aa4d3840760196).

## 2026-10-10: Paso 3, parte 1: variables, etiquetas y purga (v4)

- Código en research/pipeline_v4/ (config.py, io_kraken.py, features.py, labels.py, splits.py, build_dataset.py) con pruebas en tests/test_part1.py.
- Nota N3 añadida a las notas de implementación antes de construir el dataset real (definición exacta de las 27 variables). SHA-256 de las notas tras N3: 7e34ceef3398c308df5bb9db7830396f35c1ac3187e80f061de27148c5ea76f3
- Pruebas (8, todas superadas, con datos sintéticos):
  1. No anticipación: alterar al azar precios y volúmenes posteriores a una fecha no cambia ninguna variable con fecha anterior o igual, ni ninguna etiqueta hasta dos días antes; la alteración sí cambia los datos posteriores.
  2. Límites de la ventana [00:15, 01:15) y umbral de 45 minutos (44 no vale, 45 sí).
  3. N1: con la ventana de salida no válida, la fila no tiene etiqueta.
  4. Purga: con límite L, el entrenamiento llega como máximo a L - 3 días y todas sus etiquetas vencen antes de L; el código comprueba en cada llamada que la purga por vencimiento y por fecha coinciden.
  5. Sin relleno de huecos diarios.
  6. Coste de decisión y asignación de clases con un ejemplo calculado a mano.
  7. Elegibilidad y variables transversales solo entre activos elegibles.
  8. Las constantes del código coinciden con el YAML congelado (incluido su hash).
- Dataset real: data/processed/v4/panel_v4.csv.gz, SHA-256 daf31d6068a0ecad84e4dd247b21393dda5aef3c229709ea294e8b47cfef6655, construido 2026-10-10T18:24:36Z. Informe de recuentos en data/processed/v4/build_report.json.
- Las filas con etiqueta por activo y partición coinciden exactamente con el recuento independiente de la entrada anterior (calculado con otro código a partir de execution_window_daily.csv).
- Variables vacías en filas elegibles: como máximo un 0,1 % (huecos diarios de BTC, ETH, ZEC y XRP).
- No se ha calculado ni mirado ninguna estadística de retornos, distribución de clases, señal ni resultado.
- SHA-256 del código: build_dataset.py 70ff4dbf0c677de27888d2c76b5d3612577c9ff8f2cfa84dc6d2e09e21acdcea; config.py 8e3514ab9f732b6591a9534c3fa0541809cebdcfd88634c7a5e159257121742b; features.py 03c4d444908c95dce35f09cc3d69b2ef16f45ea4f5ca56f4bcbe84928bf79456; io_kraken.py 9188543101e5683ee436cd14f6d19a5b0d3b749a43e565c55a9dba229d7545f9; labels.py 4bbc11b1efc0a7e97461e0c45f64218d296911c0a3d15a94f2e7bc5afbca8fc9; splits.py 3c1533e10ae75e8dced3b1e0eea2630bc7a0a6f31acb5d88ce7c1777626ccf9f; tests/test_part1.py 21414a42652445291542418d9db5d3ca526426df3501925e44a5aba0fd3891a3

## 2026-10-10: Paso 3, parte 2: modelo, calibración y decisiones (v4)

- Nota N4 añadida antes de ejecutar ningún modelo con datos reales. SHA-256 de las notas tras N4: 6be82e1fcc2c19dd392295c1832bb8c8e6f64e782b7bacdefffcb9cad9e9d7c1
- Código: research/pipeline_v4/models.py, calibration.py, policy.py, walkforward.py; dependencias fijadas en requirements.txt.
- Pruebas de la parte 2 (9, todas superadas; 17 en total con la parte 1), con datos sintéticos:
  1. Ningún ajuste (6 subventanas y modelo del bloque) usa filas posteriores a su límite menos 3 días; la ventana de calibración es [inicio - 180, inicio) en 6 subventanas de 30 días.
  2. Las predicciones solo cubren filas del bloque.
  3. Cambiar al azar etiquetas y retornos de todas las filas con fecha desde 2 días antes del inicio del bloque no cambia ninguna probabilidad, valor esperado, decisión ni temperatura del bloque.
  4. Ejecución determinista.
  5. El logit sigue el mismo protocolo.
  6. La temperatura recupera un valor conocido (T = 3) en datos simulados.
  7. Regla de decisión y valores m con un ejemplo calculado a mano.
  8. Sin soporte suficiente en una clase, el bloque no es calibrable y todo es FLAT.
  9. Bloqueo del holdout: falla sin desbloqueo y falla si ya hay una ejecución registrada.
- Ejecución real, solo los 5 bloques walk-forward, LightGBM y logit. Los 10 bloques son calibrables. Filas de entrenamiento: 3850, 4212, 4771, 5477 y 6234. Filas prequential para calibrar: 409, 358, 545, 690 y 739. Temperaturas dentro del rango, entre 1,04 y 1,56.
- Salidas: data/processed/v4/predictions_wf.csv.gz (SHA-256 551e788ffc0f7d7d639cf05a1df8759af4cd6927b4419d9caab83e917f30d776) y diagnostics_wf.json (SHA-256 d4d472357c281b69944889e217473ff9a85e8a60a4b49d08d1fec6aee1f82cdc). diagnostics_wf.json contiene los valores m y los recuentos de clases de cada ventana; no se han revisado.
- No se ha calculado ni mirado ningún P&L, log loss, acierto, distribución de decisiones ni valor m. El holdout no se ha ejecutado: no existe HOLDOUT_UNLOCK ni predicciones del holdout.
- SHA-256 del código: models.py db6d6d8eab11f2e452471909862bf0d3b842ebdee371c6bf079dd3873579412a; calibration.py 7c579b6d0b7be15e2683dca29a215957fa7fa357b23e15189847c26d6c07e211; policy.py d2138659939ff4655acffbdbad958dd4faae3c2550ca18f720ea3c66307cfa1d; walkforward.py fee488c3488de8568d7ef795cb8aa95c7dd31b2fb20eb6fd4b148a6304560c44; tests/test_part2.py ff541a0a8d7c4e3e7ae9e95e25854a75dc8f649c8fd9ae36dad7b466a0be261d; requirements.txt 4962ba78f5023a04aac35416dcb80f10bb6f6cc738a21e878296789d12eb1635

## 2026-10-10: Paso 3, parte 3: N5 corregida y registrada antes de evaluar

- Una revisión externa rechazó la primera propuesta de N5 por cuatro bloqueos: el walk-forward usaba un día de precios del holdout, el reequilibrio diario era gratuito, la salida aplazada no tenía límite de espera y la varianza del Deflated Sharpe no estaba justificada. También pidió definir la cartera equiponderada y desplazar juntos acción y peso en las señales aleatorias. Se aceptan todos los puntos.
- N5 corregida en las notas de implementación (SHA-256 tras N5: 18054daaf5d4279c9cd3c30b0ab7e1fa243aceb9e5e5be28b2c87f20d229f43c). Resumen: periodos cortados por instante de valoración (walk-forward hasta el 2025-06-30, sin precios del holdout) con cierre forzado al final; posiciones en unidades sin reequilibrio gratuito; salida aplazada con espera máxima de 60 minutos y valoraciones aplazadas contadas; cartera equiponderada con rebalanceo diario y costes; aleatorias con desplazamiento conjunto de acción y peso; Deflated Sharpe con N = 51 (research/trials_inventory.csv, SHA-256 53268b4c954e8e7177377ce394eebc17a20c7ad195f24887612509959913b735) y una adaptación conservadora de la varianza, etiquetada como tal.
- Inventario: los registros solo documentan 43 ensayos evaluados; SMA200 diario y microestructura se cuentan como prerregistrados no ejecutados porque el YAML lo exige.
- Pruebas: 35, todas superadas (8 de la parte 1, 10 de la parte 2 y 17 de la parte 3). Las nuevas cubren unidades conservadas, giro y atribución de costes por lado, entrada sin proxy válido, valoración aplazada, cierre al final del periodo, límite de exposición, coste del rebalanceo, que el walk-forward no use precios del holdout, la espera máxima de 60 minutos, el desplazamiento conjunto de las aleatorias, el inventario de 51 ensayos, la fórmula del Deflated Sharpe frente a un cálculo independiente y el reetiquetado de las variantes de sensibilidad.
- Días sin precio válido con la espera de 60 minutos (2023-01-02 a 2026-06-30): ZEC 37, NEAR 12, SUI 61; el resto 0.
- Los criterios de aprobación del YAML no cambian. Todavía no se ha ejecutado ninguna evaluación con datos reales.
- SHA-256 del código: evaluate.py 456d0f5dc421d31f058a7279d004b8778e144d6aa9dc59e1c019039e3ad2c0ce; simulate.py 1919caa529bf9c263420766434561b618c80c292f442f8e8af4584023a79ac7a; stats.py 1f2c255eb3407b29ba1242f2c8ffb04fe446549868fbc7e31b8623c8e5bdc373; prep_marks.py 17c6cf0831af6c96cea3f4e507b91fb6594b9a166661517ad5ef896f52566753; walkforward.py 5c2b1c3113d064f6f2ba58a69fd0488c0fce77f8b180547516ec52e883d11ab1; tests/test_part2.py dda83d40e8efa23407e4a33fa2d5646fb2911b8db1b902115eb0947d805ac926; tests/test_part3.py 486cb99be6c3871f89970738f83f0aaf3565816f5df035d49e5b8c411de177ac

## 2026-10-10: Resultado preliminar del walk-forward (v4, sin holdout)

- Predicciones walk-forward regeneradas con las 4 variantes entrenadas (lightgbm, logit, lightgbm_h0, lightgbm_h20); las de lightgbm y logit son idénticas a las anteriores (comprobado). SHA-256 predictions_wf.csv.gz: 3e77384fc6e4ffd9a2b3866c2f70f137b96501b2638d03375292a88f15117ec7
- Evaluación: data/processed/v4/evaluation_wf.json, SHA-256 9244135fdbd4b6dbd0bff537539c16153b4a6707738f80cbe3c90b0c4722e0ec. Periodo de valoración 2023-01-02 a 2025-06-30 (911 puntos), sin precios del holdout.
- Modelo principal (LightGBM): rendimiento neto anualizado 26,4 %, Sharpe 0,93, drawdown máximo 16,7 %, 714 operaciones, costes 24,4 % del capital frente a un bruto de 90,2 %. LONG +79,5 %, SHORT -13,8 %.
- Comparadores: cartera equiponderada 73,2 % (Sharpe 1,22, drawdown 18,7 %); momentum 36,1 % (Sharpe 0,81, drawdown 33,7 %); SMA200 11,5 % (Sharpe 0,27); logit 12,9 % (Sharpe 0,52). Aleatorias: mediana 14,3 %, percentil 90 25,6 %; el modelo queda en el percentil 91.
- Alpha diario frente a BTC y momentum: -0,005 % (p unilateral 0,55); beta a BTC 0,32. IC 95 % del rendimiento anual (bloques de 20 días): -7,3 % a 66,2 %. Deflated Sharpe: 0,20 (N = 51, V = 1/(T-1), que fue la mayor de las tres estimaciones). Multiplicador de equilibrio: P25 2,52.
- Criterios evaluables sin el holdout: cumple rendimiento > 0, percentil >= 90, Sharpe > momentum y > SMA200, LightGBM mejor que logit en log loss (0,964 frente a 0,974) y en rendimiento, P25 >= 1,5, concentración por año y activo, y 150 operaciones. No cumple IC > 0, Sharpe > equiponderada, alpha significativo, Deflated Sharpe >= 0,95, drawdown < 15 % ni LONG y SHORT >= 0.
- Consecuencia de las reglas congeladas: el drawdown de la serie concatenada no puede ser menor que el del tramo walk-forward (16,7 %), así que el veredicto final será RECHAZADO con cualquier resultado del holdout.
- Lectura: la rentabilidad se explica por exposición larga a cripto en un periodo alcista (beta 0,32, alpha nulo, el lado corto pierde) y queda por debajo de simplemente mantener la cartera equiponderada. Coincide con la expectativa previa del YAML.
- No se cambia nada del diseño a la vista de estos resultados.

## 2026-10-10: Cierre de ml-daily-direction-multiasset-v4: RECHAZADO en walk-forward, holdout no ejecutado

Veredicto: RECHAZADO en walk-forward; holdout no ejecutado. El drawdown máximo del 16,7 % incumple el límite preregistrado del 15 %. El criterio de rechazo se considera activado antes de consultar el holdout. Para preservar su valor informativo, no se genera el archivo de desbloqueo ni se ejecuta la evaluación final. El holdout permanece sin consumir por este experimento. Esta decisión no modifica los criterios ni las reglas de la hipótesis congelada.

- Decisión del usuario, tras proponer yo ejecutarlo. Comprobado al cierre: no existen data/processed/v4/HOLDOUT_UNLOCK, holdout_runs.jsonl ni predictions_holdout.csv.gz.
- Además del drawdown, en el walk-forward tampoco se cumplen: IC 95 % inferior > 0, Sharpe mayor que la cartera equiponderada (0,93 frente a 1,22), alpha significativo (p = 0,55) ni Deflated Sharpe >= 0,95 (0,20). Lectura: la rentabilidad del modelo se explica por exposición larga a cripto en un periodo alcista, sin ventaja propia frente a alternativas más sencillas.
- Precisión sobre el holdout ([2025-07-01, 2026-07-01)): no se describe como completamente virgen, porque ya se han estudiado otros backtests de cripto en periodos próximos. Queda registrado como no consumido por esta evaluación específica.
- Se conservan sin cambios los resultados (evaluation_wf.json, predictions_wf.csv.gz, diagnostics_wf.json), sus hashes y la versión exacta del código registrada en las entradas anteriores.
- No se modifican parámetros, costes ni umbrales para buscar una variante ganadora. Una hipótesis futura debe responder a una pregunta distinta o tener una justificación independiente, con su propio experiment_id y contando como un ensayo más en el inventario (research/trials_inventory.csv).

## 2026-10-10: trend-daily-sma200 v1: preparación de la ejecución (fase A)

- Decisión del usuario: ejecutar el prerregistro hypotheses/trend_daily_sma200.yaml tal como está congelado (SHA-256 a4e0e6b388c163c1bab49e69d54498323ef860f8318c84cd50cb6f6792af945c, creado antes de los resultados de la v4), con sus dos variantes (long_short principal y long_flat secundaria), antes de registrar ninguna variante nueva con momentum de 90 días.
- Independencia: es una hipótesis fijada de antemano, pero no una prueba ciega a todo lo aprendido en el proyecto, porque ya se han visto otros backtests de BTC y ETH en periodos solapados (entre ellos el SMA200 largo/corto del walk-forward de la v4 en 2023-2025). No se eligen ventanas ni parámetros a partir de resultados.
- Notas de implementación S1 escritas antes de calcular ninguna señal (hypotheses/trend_daily_sma200_implementation_notes.md, SHA-256 d7a9136fa43de70fe58cf277cea36f938b1d68e0fb7c93b7d3ec0a65df589b5c): datos ya auditados, huecos sin rellenar, definición exacta de señal y volatilidad, ejecución en la siguiente apertura, capital compuesto con costes sobre el cambio frente al peso derivado, historial completo, buy-and-hold sin reequilibrio, aleatorias por activo y año con desplazamiento conjunto, estadísticos y regla de veredicto por variante.
- Costes: los del YAML (base 0,45 % y adverso 1,0 % por lado) son supuestos del experimento. La tarifa spot real de la cuenta está pendiente de verificar y se registrará aparte sin cambiar los supuestos.
- Código: research/sma200_v1/sma200.py (SHA-256 06ad5ace41a6b375092fd85849f63fe75c549fa2feb798feedff364234952c6d). Pruebas: research/sma200_v1/tests/test_sma200.py (SHA-256 ec6b758bb0eed971d58edd3dc52b9ce6b285276aed8fdba7f51f9eedbb3bf703), 9 superadas: definición de señal y volatilidad, días sin vela, ejemplo de cálculo a mano del motor, posición arrastrada sin apertura, buy-and-hold sin reequilibrio, no anticipación con precios futuros alterados, desplazamiento conjunto de las aleatorias por año, reglas de veredicto y hash del YAML.
- Pendiente del visto bueno del usuario a S1 para ejecutar.

## 2026-10-10: trend-daily-sma200 v1: controles previos (S2) y verificación antes de ejecutar

- El usuario dio el visto bueno a S1 con cuatro controles previos, recogidos en la nota S2 (notas de implementación, SHA-256 aaf3a43ba0d2d484e1f8d4b864dd78b0f689706cf5f823532897b4f083869a14):
  1. La SMA200 usa 200 observaciones diarias disponibles, no 200 días naturales; la exposición sigue la fórmula congelada.
  2. Las 18 fechas sin vela tampoco tienen minutos con operaciones en los ficheros de 1 minuto. 17 son anteriores a la cartera combinada y quedan como incidencias de calidad no explicadas. ETH 2018-01-12 es compatible con la parada de Kraken de enero de 2018, pendiente de fuente. Nada se rellena.
  3. Las aleatorias solo usan desplazamientos que conservan exactamente el número de cambios de posición; la exposición media coincide exactamente. Se guardan semilla, desplazamientos, direcciones simuladas y resultados.
  4. El buy-and-hold tiene las mismas fechas y costes, incluida la compra inicial.
- Al implementar el punto 3, una prueba detectó un error en el cálculo del punto de corte (sentido de la rotación); se corrigió antes de ejecutar. Pruebas: 10, todas superadas.
- Verificaciones: el YAML congelado mantiene su SHA-256 (a4e0e6b388c163c1bab49e69d54498323ef860f8318c84cd50cb6f6792af945c); los ficheros XBTUSD_1440.csv, ETHUSD_1440.csv, XBTUSD_1.csv y ETHUSD_1.csv coinciden con los hashes de data/audit/extract_manifest.json.
- SHA-256 del código que se ejecuta: research/sma200_v1/sma200.py 909c2474760bd8f53059fb357ffb6b9999948979f7dd2c5add3c09718431c113; tests/test_sma200.py 523c2112242034645ac05f0f83ee32377dd63b1b0562db695a9b851d53133537.
- Se ejecuta una sola evaluación; el resultado se registrará sin ajustes.

## 2026-10-10: Resultado de trend-daily-sma200 v1 (fase A): long_short RECHAZADA, long_flat INCONCLUSA

- Una sola ejecución con el código registrado. Resultados en research/data/processed/sma200_v1/evaluation.json (SHA-256 9922590607ab9b5a49ca412611d6abc77ca05b014664972da2482fd6061d534a); semillas, desplazamientos, direcciones simuladas y rendimientos de las aleatorias en random_<variante>_<coste>.npz.
- Cartera combinada BTC + ETH, del 2016-02-28 al 2026-06-30. Emparejamiento de las aleatorias verificado: diferencia máxima de 0 cambios de posición y exposición media idéntica en todas las simulaciones.
- long_short (principal), costes base: CAGR 10,8 %, Sharpe 1,14, drawdown máximo 16,5 %, exposición media 0,16. IC 95 % (bloques de 20 días) 4,0 % a 18,1 %. Percentil frente a aleatorias 74,7 (p90 11,9 %). Costes adversos: CAGR 8,6 %, drawdown 18,5 %, percentil 78,3. Veredicto: RECHAZADA (drawdown >= 15 %).
- long_flat (secundaria), costes base: CAGR 11,1 %, Sharpe 1,49, drawdown 10,5 %, exposición media 0,09. IC 95 % 5,6 % a 17,1 %. Percentil frente a aleatorias 48,9 (p50 11,1 %, p90 12,3 %). Costes adversos: CAGR 9,9 %, Sharpe 1,34, drawdown 11,4 %, percentil 53,6. Veredicto: INCONCLUSA (no supera el percentil 90 de las aleatorias).
- Buy-and-hold 50/50 en el mismo periodo: CAGR 66,5 %, Sharpe 1,04, drawdown 92,0 %.
- Lectura: con la misma exposición y el mismo número de cambios de posición, situar esos cambios al azar da el mismo resultado que la SMA200. Ni la variante principal ni la secundaria muestran que el momento de entrada y salida de la SMA200 aporte valor en este periodo. El Sharpe superior al buy-and-hold y el drawdown bajo vienen de la exposición pequeña y del control por volatilidad, no de la señal.
- Consecuencias según las reglas acordadas: la fase B (perpetuos) no se habilita. El resultado de long_flat es secundario e inconcluso; no se reformula ni se rescata.
- research/trials_inventory.csv: se actualiza el estado de trend-daily-sma200 de "prerregistrado; no ejecutado" a "evaluado: long_short rechazada, long_flat inconclusa". El número de ensayos no cambia.
- No se ha ajustado nada después de ver el resultado. live_trading sigue desactivado.

## 2026-10-10: funding-carry cross-sectional: auditoría de cobertura antes del prerregistro

- Decisión del usuario: estudiar la estrategia de carry de funding transversal entre perpetuos de Kraken Futures (corto en los de funding más alto, largo en los de funding más bajo), pero solo después de una auditoría de cobertura de datos. No hay prerregistro ni experiment_id todavía.
- Precisiones aceptadas: (1) el paper Crypto Carry (BIS WP 1087, Management Science) estudia la base futuro-spot; da fundamento económico, pero no prueba esta estrategia transversal; (2) igualar nominales largo y corto no hace la cartera neutral al mercado: hay que medir la beta residual frente a BTC y al mercado cripto; (3) con nominal bruto igual al capital, rotar toda la cartera cada semana con taker al 0,05 % cuesta en torno al 5,2 % anual, sin contar deslizamiento.
- Fuente 1 (repositorio aesx9/bot, rama main, backtest/datos): PF_XBTUSD, PF_ETHUSD y PF_SOLUSD, funding horario del 2025-10-06 08:00 al 2026-10-09 09:00 UTC (8.826, 8.826 y 8.827 filas), sin duplicados. SHA-256: XBT b5417b1e...26e1 (b5417b1ed0a92c079ce74d392074ba53cdf90f4bf505e05025dce7aa820260e1), ETH 259b28a4523fc3339960542fc5239659932680f18dc16b62a392726d0c3e9a3d, SOL 2c9c509d637dadd769d88a9afba01a8d210c11b941fea394f39d49250b98426c, manifest 7592e011f87f639958b292bb3b02fee2639f3a8aff51e9093caf62da978871de.
- Los huecos mayores de una hora caen en las mismas horas en los tres contratos (2025-11-01, 2025-11-02, 2025-11-19, 2025-12-04, 2026-02-04, 2026-02-13 y, en BTC y ETH, 2026-05-09). Apunta a una causa común de plataforma o de la API, no a un contrato concreto. Pendiente de contrastar con la exportación.
- Hipótesis de trabajo, sin verificar: el endpoint historical-funding-rates devuelve una ventana móvil de aproximadamente un año, porque la serie empieza justo un año antes de la descarga.
- Registro de contaminación: al auditar se vieron estadísticos descriptivos del funding relativo de esa ventana (media anualizada aproximada: BTC 3,1 %, ETH 3,3 %, SOL -0,3 %; fracción de horas negativas 31 %, 32 % y 49 %). No son resultados de la estrategia, pero quedan registrados porque pueden influir en el diseño del prerregistro.
- Fuente 2 (exportación oficial de Kraken, support.kraken.com/articles/export-historical-funding-rates): descarga bloqueada desde el entorno de Claude y desde la VM local por la política de red (403 del proxy). Pendiente de que el usuario la descargue a data/raw/kraken_funding_export/.
- Universo: debe definirse con una regla aplicable en cada fecha (contratos listados y con liquidez mínima en ese momento), no con los siete activos del experimento de arbitraje anterior, para no introducir selección a posteriori. Hay que comprobar si la exportación incluye contratos retirados.
- live_trading sigue desactivado.

## 2026-10-10: funding-carry: resultado de la auditoría de la exportación oficial

- El usuario descargó la exportación (ZIP SHA-256 65ba6712a6ab657389d2795d3ed77bedb4270841dfe711147ae9df16e366edab). Informe: data/audit/funding_export_audit.md (SHA-256 41c6257dc763090324b592f81b5ed1b733ae35bc5a253c09799c3fd86a2c29be); detalle por contrato: data/audit/funding_export_coverage.csv (SHA-256 13d922e56824d3b7f205c2c79e40ce5568187b8ade47711089cec9cab68964e6); script data/audit_funding_export.py (SHA-256 8c4909be7ae005945a59a68cc9ca7592e1dbe5cd056be27d340921465e797fcf).
- 480 contratos (470 PF_), esquema único, sin duplicados ni nulos. PF_ desde 2022-03-22 hasta 2026-02-01; cadencia de 4 h hasta 2022-09-29 12:00 UTC y horaria después. Incluye 160 PF_ retirados: el funding no tiene sesgo de supervivencia.
- Contraste con la API (repositorio aesx9/bot): coincidencia exacta en las 2.820 horas comunes de BTC, ETH y SOL, sin desplazamiento horario.
- Seis huecos de 1 a 2 horas comunes a toda la plataforma (2025-07 a 2025-12) y 16 huecos aislados de contratos concretos. No se rellenan.
- Durante esta auditoría no se calcularon estadísticos de las tasas.
- Bloqueos antes del prerregistro: (1) febrero de 2026 a hoy para todos los contratos vía API; (2) precios y volumen de todos los PF_, incluidos los retirados; sin ellos el resultado tendría sesgo de supervivencia en la parte de precio; (3) unidades y signo para PF_.
- live_trading sigue desactivado.

## 2026-10-10: funding-carry: descarga complementaria en el VPS

- Decisión del usuario: ejecutar las descargas pendientes en el VPS de OVH (futures.kraken.com está bloqueado desde el entorno de Claude y la VM).
- Script: production_plan/backfill_funding/backfill.py (SHA-256 1947b538de1c6cc3736256eb4ca89876f60a449b49491a9331597667ca0a4191), lista de contratos symbols_export_pf.txt (470 PF_ de la exportación, SHA-256 438f19d8131d0a5e33cf9df5ef217a3ed6567f85fa2d78fa00edca88bfd121a1). Añade los PF_ que liste hoy el catálogo de instrumentos.
- Descarga por contrato: funding de la API, velas diarias trade (precio y volumen) y mark desde 2022-03-01. Solo endpoints públicos, sin claves. Guarda respuestas, SHA-256 por página y un manifiesto append-only.
- Probado con respuestas simuladas (paginación, deduplicación, contrato sin datos, contrato nuevo). Primer paso real: modo check con un contrato vivo (PF_XBTUSD) y uno retirado (PF_0GUSD) para saber si la API conserva precios de contratos retirados.

## 2026-10-10: funding-carry: diagnóstico de contratos retirados en la API de Kraken Futures

- Script production_plan/backfill_funding/inspect_retired.py (SHA-256 f305e182ed2c7e97623d20a1c0134bac34fa3635d3048c748f760c545788b314), ejecutado en el VPS sobre PF_0GUSD, PF_LUNAUSD, PF_OCEANUSD, PF_MATICUSD, PF_FTMUSD y PF_EOSUSD. Ninguno está hoy en el catálogo.
- Precios: la API de charts conserva las velas reales de los contratos retirados hasta su retirada (con volumen). Después las rellena hasta hoy con velas planas (O=H=L=C) y volumen 0, en trade y en mark. Ejemplo: LUNA, 51 velas con volumen hasta 2022-05-12 y 1.612 velas planas después.
- Consecuencia: los precios de los retirados existen, así que el sesgo de supervivencia en la parte de precio se puede evitar. Pero las velas de relleno no son precios negociables: hay que descartarlas fuera de la ventana de vida del contrato, que se define por su funding (exportación más API), no por las velas.
- Las velas mark tienen siempre volumen 0; el volumen solo sirve en trade.
- También hay días sin volumen dentro de la vida del contrato (MATIC: último día con volumen 2024-08-30, funding hasta 2024-09-06; 0G: 27 días con volumen en unos dos meses). Hace falta una regla de liquidez para la elegibilidad y una regla de cierre de posiciones al retirarse un contrato.
- Funding en la API: para los retirados antes de la ventana devuelve solo la última fila; para 0G, retirado dentro de la ventana, 1.101 filas desde 2025-10-06. Refuerza que la API cubre una ventana móvil de aproximadamente un año: el histórico anterior depende de la exportación.

## 2026-10-10: funding-carry: auditoría de la descarga del VPS y unión de fuentes

- Descarga completa del VPS: raw/backfill_out.tgz (SHA-256 e672b3595edb59b14b4aa7dda5fc097eda040395c2db32f1fd46ae4e5bf212a8); 1.519 peticiones válidas. Informe: data/audit/funding_backfill_audit.md.
- 504 contratos PF_ (470 de la exportación, 34 nuevos). Exportación y API coinciden exactamente en 899.339 horas de 469 contratos. Serie unida en processed/funding_carry/ (hashes en audit/funding_backfill_summary.json).
- 300 contratos retirados (139 después de febrero de 2026), todos con funding y precios hasta su retirada. Las velas de relleno quedan marcadas fuera de la vida del contrato.
- 43 contratos no cripto identificados (40 por categoría del catálogo y 3 retirados por revisión manual: AUD, JPY, TUSD).
- Registro de contaminación: se han visto conteos de contratos por umbral de volumen en USD a fecha trimestral (tabla en el informe). No son resultados de la estrategia, pero muestran que antes de 2024 hay muy pocos contratos líquidos, y eso influirá en el periodo y el umbral del prerregistro. No se han calculado estadísticos de las tasas.
- Siguiente paso: redactar la propuesta de prerregistro para revisión del usuario. live_trading sigue desactivado.
- Script de la auditoría: data/audit_funding_backfill.py (SHA-256 0a7acb4aadf6736f40ce28c7dc4cfc4a4a32febc93bbbc52d1d09a31aa87830c); resumen audit/funding_backfill_summary.json (SHA-256 d5bdee63f2b8920118f5cdbce91ad75316a4f4839b8e410faa049e8a431dfaef).

## 2026-10-10: funding-carry-cross-sectional-v1: propuesta de prerregistro (proposal_pending_gates)

- Propuesta del usuario incorporada en hypotheses/funding_carry_cross_sectional_v1.proposal.yaml (borrador, SHA-256 84fc2d7b36a99f9559740009dcab39581c88143b15a60f33eb139dc93cc75ec7). No está congelada; no se ha calculado P&L.
- Concreciones añadidas a la propuesta, pendientes de visto bueno:
  1. Umbral de liquidez de 1 M USD de mediana de 30 velas completas anteriores a la decisión; el umbral se elige después de ver los conteos de liquidez (registrado como contaminación).
  2. Ranking con relative_rate y flujo de funding con absolute_rate x cantidad, sin aproximar el nominal con precios diarios.
  3. Decisión los lunes a las 00:00 UTC y ejecución a la apertura de la vela trade de ese día.
  4. Cierre por retirada al último mark de su vida con un 2 % adverso, y sensibilidad con un 5 %.
  5. Aleatorias con la tasa de retención de la estrategia.
  6. Rechazo si el funding acumulado no supera los costes.
  7. Holdout de 52 semanas desde 2025-10-06; coincide con la ventana en la que se vieron las medias de BTC, ETH y SOL, así que no es completamente ciego.
- Puerta 1 (unidades y cadencia): superada en datos. Script data/gate1_funding_units.py (SHA-256 2db771176dda67e66a5d2fff3d5640cc5e451b3960061678cb6a6c9e836e6e62); resumen data/audit/gate1_funding_units_summary.json (SHA-256 135a4a23b2375f69b43735c767bbfa1df9b1a13192c319ae0d2260640a637ac0). El signo queda como hecho documental.
- Puerta 2: comprobación previa superada (todos los contratos tienen mark dentro de su vida).
- Puerta 3: pendiente del usuario.
- Pendiente de confirmar el umbral del 6 %. live_trading sigue desactivado.

## 2026-10-11: funding-carry-cross-sectional-v1: propuesta v2 (sigue en proposal_pending_gates)

- Cambios pedidos por el usuario e incorporados en hypotheses/funding_carry_cross_sectional_v1.proposal.yaml (versión 2, SHA-256 6cbf23c988417563503ac65a28db500272cc5c165014789d87f3e6668bc3b043):
  1. Umbral económico: CAGR neta de al menos el 8,5 % en el walk-forward agregado y en el holdout, fijado ahora y no como previsión. Se informa además frente a 1 y 2 veces la letra del Tesoro a 13 semanas de cada periodo. Comprobación: el 5 de octubre de 2026 la subasta a 13 semanas dio un 4,149 % (investment rate), así que el doble ronda el 8,3 %.
  2. Ranking con tasas de timestamp en [T-169h, T-1h): periodos terminados y publicados antes de T con cualquier interpretación del timestamp.
  3. Retiradas: ajuste adverso según el lado (2 % principal, 5 % sensibilidad), más costes de salida, etiquetado como sustituto. Si el veredicto cambia entre escenarios, el resultado es inconcluso. Desaparece la regla del 10 %.
  4. Aleatorias principales con rotación igualada por recuento de contratos mantenidos en cada lado; las independientes semanales quedan como secundarias.
  5. Holdout etiquetado como retrospectivo no completamente ciego.
  6. La puerta 3 bloquea paper y producción, no el congelado ni el backtest.
- Corrección propia: la regla "tradable_at_T" usaba el volumen del día T para elegir, lo que era información posterior a la decisión. Ahora la elegibilidad solo usa datos anteriores a T; si no hay operaciones el día T, la orden no se ejecuta y el hueco queda en efectivo, sin sustituir el contrato.
- Inicio de la muestra registrado antes de calcular P&L: 2024-03-11. Script data/funding_carry_eligibility.py (SHA-256 ee8e00fe9793b607dfc47af2e44b8bb9bcc64394e3bf745fb45fa3e105cd90b6); resumen data/audit/funding_carry_eligibility_summary.json (SHA-256 512d18283608177ad1a30904a92a4ac0ae7f403d80002c99d64c10df9162acc3).
  - Walk-forward: 82 semanas, entre 4 y 9 contratos por lado.
  - Holdout: 52 semanas, 9 de ellas en efectivo por tener menos de 20 elegibles.
  - Las semanas en efectivo cuentan con rendimiento 0 en la CAGR.
- Pendiente: visto bueno del usuario para congelar. live_trading sigue desactivado.

## 2026-10-11: funding-carry-cross-sectional-v1 CONGELADO (preregistered)

- Visto bueno del usuario con dos condiciones, incorporadas en la propuesta versión 3 (hypotheses/funding_carry_cross_sectional_v1.proposal.yaml, SHA-256 aa882507d8152975584931e92a5d9c5ad87b789af79e7045d68720d9029725c5):
  A. Aleatorias principales:
     - Mismo universo y mismo n por lado.
     - Se reproducen los recuentos semanales de la estrategia: mantenidos por lado (k_L, k_S) y cambios de dirección (f_LS, f_SL).
     - Resto elegido uniformemente, sin funding ni rendimientos.
     - El turnover nocional no puede igualarse exactamente, porque el reajuste de las posiciones mantenidas depende de los precios. Se informa del cociente frente a la estrategia (mediana, p5, p95).
     - Las aleatorias independientes quedan como diagnóstico, con y sin costes.
  B. Equilibrio de lados tras las ejecuciones:
     - Objetivo por lado: m = min(L, S, 0,5 x patrimonio), con tolerancia del 1 % del patrimonio.
     - Reducción proporcional del lado mayor al mismo precio, cobrando los costes dos veces (supuesto conservador).
     - Si no es posible: fallo de ejecución. En la estrategia, la evaluación es no evaluable; en una aleatoria, se descarta y se cuenta.
     - Compatible con el límite de exposición: tras cada rebalanceo la exposición bruta no supera el 100 %.
- Otras precisiones de la versión 3:
  - Letras del Tesoro: una sola serie, Daily Treasury Bill Rates de mercado secundario a 13 semanas (coupon equivalent), sin mezclar con tipos de subasta.
  - El veredicto usa costes base y retiradas del 2 %.
  - Variantes de sensibilidad solo informativas.
- Prerregistro congelado: hypotheses/funding_carry_cross_sectional_v1.yaml, SHA-256 adf6b882e5dc7eca3bbf053f97acb8a0bd98295ef4e2f7a7226caed36a35904d. Validador hypotheses/validate_funding_carry_v1.py (SHA-256 b07b960768c5d0e76e94aeeb65e52cd578c795b48cfbc096ab8592e60ec0cde5): OK.
- Inventario: ensayo 52 añadido a research/trials_inventory.csv; fila nueva en research/experiments.csv.
- Puerta 3 (cuenta) sigue pendiente: bloquea paper y producción.
- Siguiente: motor y pruebas sin generar resultados de rentabilidad; después, una única ejecución del walk-forward. Holdout reservado. live_trading desactivado.

## 2026-10-11: funding-carry v1: motor y pruebas (sin resultados de rentabilidad)

- Código en research/funding_carry_v1/: config.py (SHA-256 2560d60c...fd23), data.py (af775939...aa32c), engine.py (9ea3eea0...b638), evaluate.py (4d5decbd...32c2), tests/test_engine.py (1d933cf0...7daa). 16 pruebas superadas, todas con datos sintéticos:
  - Hash del YAML.
  - Ventana del score (excluye T-1h y posteriores e incluye T-169h).
  - Elegibilidad sin datos del día T.
  - Quintiles y empates.
  - Signo y horario del funding.
  - Redondeo de lotes y límite bruto.
  - Ejemplo calculado a mano.
  - Entrada fallida que reduce el otro lado con doble coste.
  - Fallo de equilibrio que detiene la ejecución.
  - Retirada con ajuste adverso por lado y sin funding posterior.
  - Cierre retrasado al día siguiente con volumen.
  - Precios futuros que no alteran el pasado.
  - Aleatorias que reproducen los recuentos de la estrategia.
  - Estadísticos y reglas de veredicto.
- Notas de implementación N1 escritas antes de ejecutar: hypotheses/funding_carry_cross_sectional_v1_implementation_notes.md (SHA-256 4891f8584dc043dd88493ce56c3446f8c8c59f10d3c8a866b080b2dec4fb3440). Incluye dos casos que el YAML no contempla: ADV30 = 0 (deslizamiento del 10 %) y apertura redondeada a 0 por lotes.
- Corrección durante las pruebas, antes de usar datos reales: la regla de equilibrio fallaba si las posiciones no ejecutables superaban m por una diferencia mínima. Ahora solo hay fallo si superan m más la tolerancia del 1 %, que es lo que dice el YAML.
- Holdout: evaluate.py no lo implementa; solo se escribirá si el walk-forward no queda rechazado.
- Pendiente antes de ejecutar: descargar en el VPS la serie Daily Treasury Bill Rates (production_plan/backfill_funding/fetch_tbills.py, SHA-256 e03b88620e980f15a6d06849442568335656538a066d567722a55cc04ff26640) y registrar su hash, como exige el YAML.

## 2026-10-11: funding-carry v1: serie de letras del Tesoro registrada antes de la evaluación

- Descargada en el VPS con production_plan/backfill_funding/fetch_tbills.py y copiada a data/raw/treasury_bill_rates/. Daily Treasury Bill Rates, columna "13 WEEKS COUPON EQUIVALENT" (mercado secundario).
- SHA-256 (coinciden con los impresos en el VPS):
  - 2024: 47cc8985d9474bbdaea37266ac5658f6b1b276d3310c54c7efd70358e80bc37b
  - 2025: aa024dfde7700a971b287d7cc8284fd8c34f4dd6c66cb009cf39f501be33b562
  - 2026: ddbc352b4f934c540d9ba1e683108fe04208fb4a7161cf9325aa060cd41889bf
- Se lanza la única ejecución del walk-forward con el código registrado.

## 2026-10-11: Resultado de funding-carry-cross-sectional-v1 (walk-forward): RECHAZADA

- Una sola ejecución con el código registrado (tests 16/16; hashes de código y YAML verificados antes de lanzar). La elegibilidad recalculada coincidió semana a semana con el registro previo.
- Resultados:
  - data/processed/funding_carry_v1/evaluation_wf.json (SHA-256 ab2d58ac72c0afa07f5f9b699ba082cace361628d9676f30c8ebd45cb8dc52db)
  - equity_wf.csv (40e2adbe...09fe2)
  - randoms_wf.npz (7e1a8f01...9ab9)
  - run_wf.log (0ac8e587...efb48)
- Periodo: decisiones del 2024-03-11 al 2025-09-29, P&L hasta 2025-10-06. 82 semanas, 1 en efectivo (2024-09-23).
- Estrategia (costes base, retiradas 2 %):
  - Rentabilidad: CAGR 24,4 %, Sharpe 0,92, drawdown máximo 23,4 %; patrimonio final 775 USD.
  - Desglose: precio +224,7 USD; funding +90,3; comisiones -29,6; deslizamiento -59,2; retiradas -0,8.
  - Funding menos costes: +1,5 USD.
  - Turnover 56 veces el patrimonio al año; costes del 10,3 % del capital al año; multiplicador de equilibrio 3,5.
- Sensibilidades:
  - Costes adversos: CAGR 11,4 %, drawdown 27,7 %, funding menos costes -106,6 USD.
  - Retiradas al 5 %: CAGR 24,2 %, mismo veredicto.
- IC 95 % de la CAGR (bloques de 10, 20 y 40 días): límite inferior entre -15,0 % y -14,3 %; el intervalo incluye el 0.
- Aleatorias principales:
  - Percentil 94,9 (mediana -9,4 %, p90 16,9 %).
  - Cociente de turnover frente a la estrategia 0,99 a 1,01: rotación bien igualada.
- Aleatorias independientes: percentil 97,1 con costes y 90,4 sin costes.
- Deflated Sharpe 0,09 (N = 52). Efecto mínimo detectable del 45 % anual: la potencia es muy baja.
- Beta Newey-West contra BTC -0,11 (t -4,2) y contra la equiponderada del universo -0,08 (t -4,4). Alfa diaria no significativa (t 1,6).
- Letras del Tesoro a 13 semanas (informativo): media del 4,67 % en el periodo; la estrategia queda 15,1 puntos por encima del doble.
- Veredicto: RECHAZADA por drawdown de 15 % o más, en los dos escenarios de retirada. Tampoco cumple el IC 95 % ni el Deflated Sharpe. Sí cumple el percentil 90 frente a las aleatorias, el 8,5 %, el multiplicador de costes de 2 y que el funding supere los costes (por 1,5 USD).
- Lectura: el diferencial de funding apenas paga los costes de rotación (+1,5 USD en el escenario base, negativo con costes adversos). La rentabilidad viene casi entera del precio. Es decir, el mecanismo que la hipótesis quería probar (que el carry se pague a sí mismo) no queda respaldado. El componente de precio no se interpreta como hallazgo, porque no era la hipótesis y su intervalo incluye el 0.
- Descartes de aleatorias: 376 de 1.000 principales y 406 de 1.000 independientes, todas por un único evento. El lunes 2024-09-23 era la semana en efectivo y PF_FETUSD no tuvo operaciones. Las aleatorias que lo tenían no pudieron cerrarlo, se quedaron con exposición unilateral y se descartaron por la regla del YAML. La estrategia no lo tenía. Los percentiles se calculan sobre las supervivientes (624 y 594). No cambia el veredicto, que no depende de las aleatorias.
- Diagnóstico posterior sin cambiar nada: scripts temporales fuera del repositorio para localizar la causa de los descartes.
- Consecuencias:
  - Holdout no consumido; no se crea HOLDOUT_UNLOCK.
  - Fase de paper no habilitada.
  - No se ajusta ninguna regla.
  - Cualquier idea derivada del componente de precio (por ejemplo, ponerse corto en contratos con funding alto por riesgo de caída) sería una hipótesis nueva sugerida por estos datos, con su propio experiment_id. No podría validarse en este mismo periodo.
- live_trading sigue desactivado.

## 2026-10-11: CIERRE FORMAL de funding-carry-cross-sectional-v1

- Decisión del usuario: cerrar el experimento como RECHAZADA, sin cambiar reglas ni lanzar variantes retrospectivas.
- Lectura acordada: la rentabilidad observada no es ficticia, y el funding podría ser útil en otros contextos. Lo que dice el resultado es que este experimento no demuestra una ventaja de carry lo bastante robusta para seguir con la estrategia.
- Holdout del 2025-10-06 al 2026-10-05: sellado; no existe HOLDOUT_UNLOCK y no se ejecutará para rescatar ni para elegir variantes. El de v4 (del 2025-07-01 al 2026-07-01) tampoco se ha consumido.
- No se activan paper ni producción para esta estrategia.
- Se conservan el código, la configuración, los resultados y sus hashes: decisions/funding_carry_v1_closure_hashes.txt.
- Registro consolidado de las líneas examinadas, con modos de fallo, familias ya probadas, estado de los periodos reservados y lecciones de proceso: decisions/research_summary.md (SHA-256 caac6b6af4d49ccec2133ecfd94319853dfc1e726ef8e3af041fbf63c771a7ea).
- live_trading sigue desactivado.

## 2026-10-11: Copias públicas redactadas de tres prerregistros congelados

Decisión del usuario antes de publicar la capa de investigación en el repositorio canónico: no publicar los originales que nombran el nivel de comisiones de la cuenta ni el tramo de volumen, pero tampoco sobrescribirlos ni alterar su identidad histórica.

- Se conservan intactos los tres originales internos, fuera del repositorio. Sus SHA-256 se verificaron el 2026-10-11 y siguen siendo los registrados al congelarlos: carry congelado adf6b882...904d, propuesta v3 aa882507...25a5, v4 3440417e...0196.
- Se derivan copias públicas con un único fragmento de texto descriptivo redactado por fichero: `costs.fee_source` en los dos del carry y `costs.fee_schedule.tier` en la v4. SHA-256 de las copias públicas: 7d63433b...f5c1, 5d92a2ec...15ad y 53d946d9...12af.
- Las redacciones no alteran parámetros, datos, umbrales, universos, reglas, criterios de aceptación ni resultados. Las tarifas empleadas (0,0005 taker) se conservan íntegras. El protocolo operativo congelado no cambia y no se reabre ningún experimento.
- Número de líneas y finales de línea conservados; el resto de cada fichero es idéntico al original byte a byte.
- Detalle, tabla de hashes, fragmentos y consecuencias: decisions/redaction_manifest.md.
- Las citas de hash de las entradas históricas de este registro y de experiments.csv se refieren al original interno y no se reescriben. En experiments.csv se anota de forma explícita que el hash citado es el del original interno y que la copia pública está en el manifiesto.
- El código de los motores que comprueba el hash del prerregistro (funding_carry_v1/config.py) espera el original interno y no se publica. Quien reproduzca los ensayos debe partir del original.
- La captura de la cuenta sigue sin versionarse. La entrada del 2026-10-10 sobre la tarifa verificada se redactó antes para dejar solo las tarifas usadas como supuesto de investigación.
- live_trading sigue desactivado. El estado de aprobación de tendencia-micros-cme-v1 no cambia.
