# tendencia-micros-cme-v1: decisiones pendientes antes del prerregistro

Estado: borrador de fase documental, 2026-10-11. No es el prerregistro. No se ha descargado, abierto ni calculado ningún dato de precios. `live_trading: false`.

Familia: tendencia (ya probada con SMA200 en BTC/ETH y con SMA + Stoch RSI en 4 h). Se registraría como ensayo 53 de `research/trials_inventory.csv`.

Cada punto tiene una propuesta (P). Ninguna es definitiva hasta que Llorenç la apruebe. Las marcadas **[DECIDE LLORENÇ]** son las que el traspaso prohíbe resolver por mi cuenta.

## 0. Bloqueo de proceso: no hay repositorio git

- `C:\Users\lsala\Downloads\research` no es un repositorio git (ni su carpeta superior). Tampoco están aquí `src/copybot` ni `backtest/`.
- El README ya registra un 403 de GitHub al crear la rama `research/week1-preregister`.
- Consecuencia: ahora mismo no puedo crear rama ni abrir PR.
- **[DECIDE LLORENÇ]** Opciones: (a) indicar la ruta del checkout git real o el remoto, y trabajar allí; (b) `git init` local con una rama, sin PR; (c) entregar el YAML con su hash sin PR y registrarlo en el decision log. P: (a).

## 1. Exclusión por costes y prior de Sharpe **[DECIDE LLORENÇ]**

Interpretación del cociente: `coste_anual_taker / riesgo_anual_contrato` es la erosión anual esperada de rentabilidad expresada en unidades de volatilidad de una posición de un contrato. No es una Sharpe observada ni una estimación de la Sharpe futura. Solo es comparable con una Sharpe supuesta antes de costes.

Alternativas:

| Opción | Prior | Umbral (prior / 3) | Procedencia y significado | Efecto con las cifras orientativas del traspaso |
|---|---:|---:|---|---|
| A | 0,40 | 0,133 | Regla práctica de gestores de tendencia (el "límite de velocidad" de 0,13 unidades de Sharpe que populariza R. Carver, *Systematic Trading*, 2015): Sharpe típica antes de costes de una regla de tendencia en un mercado individual en backtests largos. No sale de Hurst, Ooi y Pedersen (2017), que informan de la cartera diversificada; la atribución exacta está por verificar. | Pasan todos salvo el 10Y con 6,50 USD (0,20). |
| B | 0,17 | 0,057 | Implícita: Sharpe de cartera 0,25 dividida por raíz de unas 2 apuestas independientes. Coherente con la expectativa prudente del propio traspaso. | No pasa ninguno (el menor es M6A con 0,07). El experimento terminaría sin backtest: rechazado para este tamaño de cuenta. |
| C | ninguno | ninguno | Sin exclusión previa por costes. Los costes taker se cobran íntegros en la simulación y los criterios netos deciden. El cociente se informa por mercado como diagnóstico. | El universo queda fijado solo por la validación contractual y de datos. |

- La elección determina el resultado antes de ver datos (con B el experimento acaba sin evaluarse). Por eso no la elijo.
- Mi recomendación, si sirve: C. Las opciones A y B dependen de un prior que no se puede defender con datos de este experimento, y C no usa la rentabilidad del periodo evaluado.
- Ambigüedad adicional si se elige A o B: `ejecuciones_senal_por_anio_supuestas: 6`. Hay que fijar si son lados o idas y vueltas, si incluyen los rollovers (FX 4 al año, 10Y 12, 1OZ 6, cada uno con 2 ejecuciones) y qué precio se usa para el nominal (el cálculo necesita un precio, así que solo puede hacerse tras congelar, con una fecha fijada ahora). P: lados, rollovers incluidos, precio de cierre del último día hábil de septiembre de 2026.

## 2. Instrumento 10Y

Lo que dicen fuentes públicas de CME (no verificado contra la cuenta de Kraken EEE):

- El código `10Y` de CME corresponde a **Micro 10-Year Yield futures**, no a un futuro de bono convencional.
- Cotiza **en rendimiento** (4,213 significa 4,213 %). Unidad: 1.000 USD x puntos de índice, es decir, 10 USD por punto básico. Tick 0,001 = 1 USD.
- Liquidación en efectivo contra el índice BrokerTec del rendimiento a 10 años (un documento de 2021 dice el CUSIP on-the-run más reciente; prevalece la documentación actual).
- Dos vencimientos mensuales consecutivos; termina el último día hábil del mes. Lanzado en agosto de 2021.
- No he encontrado confirmación de que Kraken lo ofrezca en el EEE. Su página de futuros TradFi menciona índices, oro, plata, petróleo, gas y divisas, pero no tipos de interés.

Consecuencias si el instrumento es ese:

- Signo: un largo gana cuando **sube** el rendimiento (lo contrario de un futuro de bono). No se codifica hasta confirmarlo.
- `cambio en pb x 10 USD x posición` es el P&L exacto **sobre el precio del propio contrato**. Aplicado a FRED DGS10 es solo una aproximación: DGS10 es rendimiento spot a vencimiento constante; el futuro cotiza un rendimiento a plazo que converge al spot. La aproximación omite ese arrastre (con curva positiva, sobrestima el P&L de los largos en rendimiento), usa otra referencia (CMT frente a BrokerTec) y otra hora de cierre.
- Historia del contrato: unos 5 años. Los 15 años solo se cubren con un proxy.
- Base de la comisión: "0,05 % del nominal" no tiene sentido evidente en un contrato cotizado en rendimiento (1.000 x 4,2 = 4.200 USD). Solo Kraken puede aclararlo.

**[DECIDE LLORENÇ]**:
- (a) Excluir el 10Y de la implementación principal hasta que Kraken confirme que lo ofrece, su especificación y su base de comisión; informar "sensibilidad a la exclusión del 10Y" no aplicaría.
- (b) Incluirlo con DGS10 como proxy etiquetado `APROXIMACIÓN NO VERIFICADA`, señal calculada sobre el cambio del rendimiento y posición larga en el contrato cuando el rendimiento sube.
- P: (a) mientras no haya confirmación; (b) solo si Kraken confirma el instrumento antes de congelar.
- Si se incluye: qué escenario de comisión es el caso principal. P: 6,50 USD por lado como principal (conservador) y 2,60 como sensibilidad. El traspaso no lo fija.
- Diferencia declarada frente a Hurst, Ooi y Pedersen: ellos usan rendimientos en exceso de futuros de bono, que incluyen el arrastre; el signo del cambio de rendimiento puede diferir del signo de esa rentabilidad cuando el movimiento es pequeño.

## 3. Datos para señales, P&L y costes **[DECIDE LLORENÇ]**

Hallazgo previo: el 1OZ (1 onza de oro, COMEX) cotiza desde 2025 y se liquida en efectivo contra el GC; los micros de divisas tienen historia más larga, pero tampoco cubren 2008 por sí mismos. Todos los mercados necesitan proxy.

Requisito implícito de historia: el episodio 2008-09-01 exige datos válidos desde aproximadamente 2007-06 (252 días hábiles de señal más el arranque de la EWMA). Es decir, unos 19 años, no 15.

| Uso | Opción 1 (preferida) | Opción 2 | Opción 3 |
|---|---|---|---|
| Señal y P&L, divisas | Contratos individuales 6E/6B/6A con liquidación diaria y rollover propio, fuente con licencia (CME DataMine u otro proveedor; coste y cobertura previa a 2010 por verificar) | Series continuas tipo `6E=F`: método de rollover desconocido, saltos sin ajustar y licencia dudosa. P: no aceptable para P&L | Tipos spot oficiales (BCE; Reserva Federal H.10 en FRED: DEXUSEU, DEXUSUK, DEXUSAL). Omiten el diferencial de tipos que incorpora el futuro (sesgo declarado; relevante en AUD) |
| Señal y P&L, oro | Contratos GC individuales con licencia | `GC=F` continuo, mismas objeciones | Sin fuente oficial gratuita clara (FRED retiró las series LBMA) |
| Señal y P&L, 10Y | Ver punto 2 | | |
| Costes | Comisión y spread solo como supuesto `PROVISIONAL`; no hay datos de spread intradía | | |
| Conversión a euros | Tipo de referencia diario del BCE | | |

- P: opción 1 si Llorenç acepta su coste; si no, opción 3 con el sesgo de arrastre declarado y resultados etiquetados como proxy. Nunca la opción 2 para P&L.
- En todos los casos el P&L es una reconstrucción a partir de liquidaciones diarias, no un historial de ejecuciones.

## 4. Pesos, IDM y granularidad

Aritmética con las cifras orientativas del traspaso, sin datos: con 28.000 USD, objetivo 6 %, IDM 1,5 y peso 0,2, el numerador es 504 USD.

| Mercado | Riesgo supuesto | Posición ideal con señal ±1 | Con señal ±1/3 |
|---|---:|---:|---:|
| M6E | 980 | 0,51 | 0,17 |
| M6B | 660 | 0,76 | 0,25 |
| M6A | 700 | 0,72 | 0,24 |
| 10Y | 1.000 | 0,50 | 0,17 |
| 1OZ | 760 | 0,66 | 0,22 |

- Con la banda de apertura en 0,6, M6E y 10Y no abrirían nunca, y los demás solo cuando las tres ventanas coinciden. La cartera tendría como máximo 3 contratos, y es posible que la volatilidad mediana quede por debajo del 4 % que exige el criterio.
- No propongo cambiar la regla para evitarlo: es justo lo que el experimento quiere medir. Pero debe quedar decidido ahora, no después.
- **[DECIDE LLORENÇ]** Si se excluye un mercado (por el 10Y o por costes), ¿los pesos se renormalizan (0,25 con 4 mercados, numerador 630, y M6E pasaría a abrir con señal completa) o se mantiene 0,2? ¿El IDM sigue en 1,5? P: mantener 0,2 e IDM 1,5 fijos, para que una exclusión no cambie el tamaño de los demás.

## 5. Límite de dos divisas en el mismo sentido frente al USD

Las tres son XXX/USD: largo en las tres equivale a estar corto en USD tres veces.
P: si las tres quieren el mismo sentido, se mantienen primero las posiciones ya abiertas; para plazas nuevas, mayor |posición ideal|; empate, orden fijo M6E, M6B, M6A. La divisa bloqueada queda en 0 y se registra.

## 6. Riesgo de cartera y covarianzas

La fórmula de posición no usa la covarianza; `limite_exposicion` y `metodo_calculo_riesgo_cartera` están vacíos.
P: covarianza solo como diagnóstico (volatilidad ex ante, contribución al riesgo, diferencia frente al objetivo), sin tope adicional, porque el máximo de 1 contrato por mercado ya acota la exposición. Estimador: EWMA con centro de masa 60 sobre P&L diario en USD por contrato, con datos hasta t, pares con observaciones comunes, sin relleno. Alternativa: un tope de volatilidad ex ante del 10 % que elimine la posición de mayor contribución.

## 7. Volatilidad por contrato

- Se estima directamente sobre el P&L diario en USD de un contrato (cambio de precio x multiplicador; 10Y: cambio en pb x 10), así el nivel de precio entra solo.
- EWMA de cuadrados sin restar la media, centro de masa 60 (lambda = 60/61), anualizada con raíz de 261.
- Arranque: 60 observaciones válidas antes de la primera estimación; sin estimación, posición 0.
- Conversión a EUR con el tipo del BCE del mismo día.

## 8. Señal

- Rentabilidad de cada ventana: suma de los rendimientos diarios de la serie de P&L elegida en el punto 3 en las últimas 21, 63 y 252 observaciones válidas hasta el cierre de t (10Y: suma de cambios en pb).
- Signo 0 si la suma es exactamente 0; entonces aparecen valores fuera de la lista (±2/3, 0). P: aceptarlo y ampliar la lista.
- Días sin dato: no se rellenan; la ventana cuenta observaciones válidas, y si falta el dato de t no hay señal nueva ese día.
- Calendario: días hábiles de liquidación de CME en EE. UU.

## 9. Ejecución y costes del caso principal

- Señal al cierre de t, ejecución al precio de liquidación de t+1 (variante: t+2). La variante mide sensibilidad al retraso y no reconstruye órdenes post-only.
- Coste por contrato y lado: 0,05 % del nominal más 1 tick. Pasar de -1 a +1 son 2 contratos. Rollover: 2 ejecuciones con el mismo coste. Todo `PROVISIONAL`.
- No incluye tasas de CME, NFA ni cámara salvo que Kraken confirme que su comisión las incluye (pendiente externo).

## 10. Calendario de rollover (según especificaciones públicas; por confirmar con Kraken)

| Contrato | Ciclo | Último día de negociación | Propuesta |
|---|---|---|---|
| M6E, M6B, M6A | Trimestral H/M/U/Z, 2 trimestres listados | 2 días hábiles antes del tercer miércoles | 5 días hábiles antes; el M6E es de entrega física, así que nunca se mantiene hasta el vencimiento |
| 10Y | Mensual, 2 meses listados | Último día hábil del mes | 3 días hábiles antes (12 rollovers al año) |
| 1OZ | Meses pares, 24 meses listados | Tercer último día hábil del mes anterior al del contrato | 5 días hábiles antes (P; el traspaso no lo fija) |

Si el P&L sale de series spot (opción 3), el coste del rollover se cobra en su fecha pero el diferencial entre vencimientos no se modela.

## 11. Bootstrap

P:
- Se remuestrea la serie diaria de rentabilidad neta de la cartera en EUR (caso principal). Al tomar los mismos días para todos los mercados, se conserva la correlación entre activos y la dependencia de señal y posiciones dentro de cada bloque.
- Bloques circulares de 63 días hábiles con inicio uniforme, empalmados hasta 2.610 días (10 x 261); 10.000 trayectorias; semilla fija registrada. Cada trayectoria se compone desde capital 1 y se calcula su drawdown máximo.
- Probabilidad estimada = fracción de trayectorias con drawdown > 15 %. Se informa con intervalo de Wilson al 95 % (error Monte Carlo).
- **[DECIDE LLORENÇ]** El criterio se cumple si (a) la estimación puntual no supera 0,20, o (b) el extremo superior del intervalo no supera 0,20. P: (b), más conservador.
- Alternativa descartada como principal: remuestrear rendimientos de mercado y volver a simular el motor. Respetaría los contratos enteros, pero los bloques de 63 días rompen la persistencia de 252 días de la que vive la señal.
- Limitación: los efectos de granularidad quedan fijados en la trayectoria histórica del capital; el bootstrap no contiene crisis ausentes del historial.

## 12. Cobertura y episodios de estrés

P:
- Cada serie de P&L debe tener datos válidos desde 2007-06-01 hasta 2026-09-30, con como máximo 5 días hábiles seguidos sin dato y como máximo un 2 % de días ausentes por año natural.
- La fecha inicial efectiva es la primera en la que todos los mercados incluidos tienen señal y volatilidad válidas.
- Si una serie no cubre un episodio, el protocolo no es ejecutable tal como está escrito: se detiene y se pide decisión. No se sustituyen fechas ni fuentes.

## 13. Definición de métricas

P:
- Serie diaria de patrimonio en EUR.
- Volatilidad móvil de 3 meses: desviación de 63 rendimientos diarios x raíz de 261. Mediana y p95 sobre todo el periodo evaluado.
- Drawdown máximo sobre el patrimonio diario frente a su máximo previo.
- Periodos en efectivo: rendimiento 0, sin intereses (conservador; se informa).
- **[DECIDE LLORENÇ]** "Rentabilidad neta media > 0": P: CAGR del patrimonio en EUR > 0 (más exigente que la media aritmética diaria).

## 14. Holdouts y periodo

- Los holdouts sellados de v4 y carry v1 son de datos cripto de esos experimentos. Este experimento no lee ningún fichero de `data/processed/v4` ni de `funding_carry_v1`.
- **[DECIDE LLORENÇ]** Confirmar que usar datos de divisas, tipos y oro de 2025-2026 no se considera tocar esos holdouts. Si se considera que sí, el periodo terminaría el 2025-06-30.
- El traspaso no prevé un holdout propio: la historia se evalúa una sola vez y la validación independiente solo puede venir de paper. P: así, y declararlo.

## 15. Conversión a euros

P: tipo de referencia del BCE del día t (publicado a las 16:00 CET, antes del cierre de CME). El P&L en USD se convierte a EUR cada día (sin saldo en USD con exposición cambiaria). El capital para dimensionar en t es el patrimonio en EUR de t convertido con ese tipo.

## 16. Diagnósticos

- SG Trend Index: es un índice con licencia. P: solo si Llorenç aporta datos autorizados; si no, se omite y se declara.
- Uso de margen: con los márgenes publicados por CME en una fecha registrada, como supuesto, mientras Kraken no confirme los suyos.

## 17. Confirmaciones menores

- Mercados excluidos (MES, MNQ, MYM, M2K, MGC, NKD, MCL): se excluyen por presupuesto de riesgo con cifras supuestas, no medidas. P: exclusión fija, no se reevalúa con datos.
- Identificador: el traspaso usa `tendencia-micros-cme-v1`; el repositorio usa identificadores en inglés (`trend-daily-sma200`). P: mantener `tendencia-micros-cme-v1`.

## Registro de conocimiento previo y contaminación

- No se ha visto ningún precio, rentabilidad ni volatilidad de estos mercados en este proyecto.
- Las volatilidades y costes del traspaso son supuestos aportados antes de auditar.
- Se conocen los resultados publicados de Hurst, Ooi y Pedersen (2017) y de la literatura TSMOM, que motivan la hipótesis.
- Especificaciones contractuales consultadas en fuentes públicas el 2026-10-11 (páginas de CME, presentaciones ante la CFTC, comunicados de brokers). No sustituyen la confirmación del instrumento que ofrece la cuenta del EEE.

## Resoluciones de Llorenç (2026-10-11)

0: opción A (checkout canónico; sin git en `research/`). 1: C. 2: 10Y excluido de la cartera principal y de los criterios. 3: P&L con contratos individuales; oro previo al 1OZ con GC individual escalado por onza; sin series continuas de fuente desconocida; si los datos no tienen coste aceptable, parar. 4: cuatro mercados, peso fijo 0,25, IDM 1,5. 5: límite de dos divisas con desempate M6E, M6B, M6A; documentar cierres, reversos y entradas. 6: covarianza solo diagnóstica. 7: EWMA (centro de masa 60) con media cero documentada. 8: señal sobre cadena sin saltos y P&L separado; signo 0 permitido (7 valores); cambio de signo: cerrar y abrir el contrario solo si alcanza 0,6. 9: costes provisionales sin suponer cero los cargos no verificados; sin aprobado definitivo con costes sin verificar. 10: rollover según el contrato de Kraken. 11: bloques de 63 (principal), 126 y 252 (sensibilidad); Wilson superior <= 0,20; inconcluso si cambia la conclusión. 12: calendario esperado de sesiones. 13: CAGR neta en EUR > 0. 14: datos de divisas y oro permitidos; 2025-2026 es "evaluación histórica no inspeccionada". 15: último tipo del BCE ya publicado; P&L EUR = P&L USD / (USD por EUR). 16: sin SG Trend Index; margen de CME como aproximación. 17: exclusiones fijas.

## Estado del repositorio (comprobado el 2026-10-11)

- Repositorio canónico: `github.com/aesx9/bot`. Checkout local: `C:\Users\lsala\bot-universo` (rama local `claude/backtest-universo-volatil`, sin rama remota; árbol limpio). `git fetch` funciona.
- `research/` no existe en `origin/main` ni en ninguna rama remota: toda la capa de investigación (registro de decisiones, inventario, validadores, prerregistros) solo existe en esta carpeta local.
- La CLI `gh` no está instalada. No se han verificado permisos de escritura (push) ni de creación de PR.

## Decisiones que siguen abiertas antes de congelar

- R1. Acceso al repositorio y contenido del PR.
- R2. Fuente y presupuesto de datos históricos.
- R3. Contratos de Kraken sin confirmar: congelar ahora con una puerta previa o esperar.
- R4. Divisas antes de 2009: contratos estándar escalados.
- R5. Oro: dónde empalmar el GC escalado con el 1OZ.
- R6. Fuente del calendario esperado de sesiones.
- R7. Definición de "cambio material" entre longitudes de bloque.

## Resoluciones R1 a R7 de Llorenç (2026-10-11)

- R1: rama nueva desde `origin/main` de `aesx9/bot` con la capa de documentación y gobernanza de `research/`, tras auditar el listado. Sin la captura de la cuenta, credenciales, precios, datos procesados ni artefactos de holdouts. PR solo tras resolver autenticación y permiso de escritura.
- R2: Norgate (contratos estándar individuales 6E, 6B, 6A y GC). Límite inicial de 150 USD por 6 meses; 12 meses (hasta 270 USD) solo con autorización expresa. No se contrata ni descarga nada todavía.
- R3: congelar con puerta previa obligatoria de Kraken; si un instrumento falta o no coincide, no ejecutable y prerregistro nuevo.
- R4: liquidaciones de 6E, 6B y 6A en todo el historial con el multiplicador micro; todo el tramo anterior a los micros es proxy sintético; costes del micro.
- R5: GC escalado por onza como proxy homogéneo en todo el periodo, sin empalme con el 1OZ; el 1OZ solo como diagnóstico.
- R6: calendarios y especificaciones oficiales de CME con versión y fecha; librería con versión fijada solo como ayuda; las fechas de Kraken prevalecen tras confirmarlas.
- R7: bloques circulares de 63 (principal), 126 y 252; inconcluso si cambia la decisión; Wilson descrito como incertidumbre Monte Carlo.
- Holdouts: comprobado en YAML y código que la protección es específica de los datos y artefactos de cada experimento; no hay regla global por fecha.

Resultado: hypotheses/tendencia_micros_cme_v1.yaml y hypotheses/validate_tendencia_micros_cme_v1.py.

## Pendientes externos de Llorenç (sin cambios respecto al traspaso)

Comisión total por lado y su base (también la del 10Y); disponibilidad de post-only; lista oficial de contratos TradFi de la cuenta del EEE (¿existe el 10Y? ¿el 1OZ?); especificaciones y trato de la entrega física del M6E/M6B/M6A en Kraken; fuente y licencia de datos históricos (punto 3).
