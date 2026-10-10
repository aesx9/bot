# Backtest 4h: tendencia (SMA) + RSI estocástico en perpetuos de Kraken Futures

Informe generado por `python -m backtest ejecutar`; no editar a mano. Operaciones en `backtest/resultados/`.

## 1. Veredicto

Criterios fijados antes de ver resultados, evaluados con el escenario de funding **pesimista**. El tramo reservado se ejecuta una sola vez con los parámetros base; la robustez ±20 % se mide solo en desarrollo.

| Criterio | Resultado | Estado |
|---|---|---|
| Neto positivo en el reservado | rentabilidad neta -11,30 % (-62,16 USD) | ❌ no cumple |
| Operaciones en el reservado | 248 operaciones (mínimo 50) | ✅ cumple |
| Drawdown máximo en el reservado | 26,41 % (debe ser < 15,00 %) | ❌ no cumple |
| Positivo con los parámetros ±20 % (desarrollo) | 0/20 variantes con neto positivo; peor -62,89 % | ❌ no cumple |
| Percentil frente al azar (reservado) | percentil 53,8 de 1000 simulaciones (mínimo 90); mediana del azar -12,49 % | ❌ no cumple |

**Veredicto: NO supera los criterios** (fallan: Neto positivo en el reservado; Drawdown máximo en el reservado; Positivo con los parámetros ±20 % (desarrollo); Percentil frente al azar (reservado)). Con estas reglas fijas no se ha demostrado ventaja real.

## 2. Datos

Velas de 4h tipo `trade` y funding horario de la API pública de Kraken Futures (`api/charts/v1` y `historical-funding-rates`), guardados en `backtest/datos/` para reproducir sin red. La vela en curso en el momento de la descarga se descartó.

| Activo | Velas 4h | Rango | Años de velas | Horas de funding | Rango de funding | Años de funding |
|---|---|---|---|---|---|---|
| PF_XBTUSD | 9966 | 2022-03-23 → 2026-10-09 | 4,55 | 8826 | 2025-10-06 → 2026-10-09 | 1,01 |
| PF_ETHUSD | 9966 | 2022-03-23 → 2026-10-09 | 4,55 | 8826 | 2025-10-06 → 2026-10-09 | 1,01 |
| PF_SOLUSD | 9966 | 2022-03-23 → 2026-10-09 | 4,55 | 8827 | 2025-10-06 → 2026-10-09 | 1,01 |

- Corte 70/30 por tiempo: desarrollo = primeras 6976 velas (2022-03-23 → 2025-05-29); reservado = 2990 velas restantes (2025-05-29 → 2026-10-09).
- **Funding: Kraken solo publica ≈1,0 año; el endpoint no admite rangos ni paginación.** Donde no hay dato se imputa (ver sección 5) y los dos escenarios se informan por separado.

Tasas relativas horarias del año real de funding (en millonésimas por hora; positivo = pagan los largos). El escenario pesimista aplica `|P75|` siempre en contra de la posición; el central, la mediana con signo.

| Activo | Mediana | P75 (con signo) | Abs. del P75 (usado) | P75 de abs. (no usado) |
|---|---|---|---|---|
| PF_XBTUSD | +3,470 | +8,534 | 8,534 | 9,615 |
| PF_ETHUSD | +3,683 | +9,018 | 9,018 | 10,253 |
| PF_SOLUSD | +0,487 | +9,483 | 9,483 | 16,845 |

El escenario pesimista usa la lectura literal `|P75|` (valor absoluto del percentil 75 de la tasa con signo). La columna «P75 de abs.» es la lectura alternativa (percentil 75 de las tasas en valor absoluto), más dura; se muestra solo como referencia.

## 3. Reglas y supuestos

- **Régimen**: cierre > SMA50 → solo largos; cierre < SMA50 → solo cortos.
- **Entrada**: RSI estocástico (14, 14, 3, 3) con RSI de Wilder. Largo si %K cruza por encima de %D y %K de la vela anterior estaba por debajo de 20; corto si %K cruza por debajo de %D y %K de la vela anterior estaba por encima de 80. (Interpretación de «habiendo estado por debajo/encima»: la vela inmediatamente anterior al cruce.)
- **Ejecución**: señal al cierre de la vela, entrada a la apertura de la siguiente; nunca se usan datos de la vela en curso.
- **Salidas**: stop a 2,0×ATR(14) y take profit a 3,0×ATR desde el precio de entrada (ATR de la vela de la señal). Stop y TP en la misma vela → salta el stop. Hueco más allá del stop → se ejecuta a la apertura. Sin salida por tiempo; al final del tramo se cierra al cierre.
- **Tamaño**: se arriesga el 1 % del capital realizado hasta el stop; nocional total abierto ≤ 2× el capital (compartido entre los tres activos, que comparten una cartera única); una posición por activo. Capital inicial 550 USD en cada tramo.
- **Costes**: comisión taker 0,05 % y slippage 5 pb en contra, en cada ejecución (entrada y salida, también en TP). Funding horario mientras la posición está abierta, con la granularidad de la vela de 4h.

## 4. Resultados por tramo y activo

Los tres activos operan en una cartera única; las filas por activo son su contribución (PnL de sus operaciones sobre el capital inicial). Drawdown y Sharpe (diario, anualizado ×√365, sin tasa libre de riesgo) salen del capital marcado a mercado al cierre de cada vela de 4h.

### Funding pesimista — escenario que se evalúa

#### Desarrollo

2022-03-23 → 2025-05-29. Señales con entrada posible: 1067; ignoradas por posición abierta: 566; descartadas por margen: 0.

| Activo | Rent. neta | Neto USD | Ops | Acierto | Profit factor | DD máx | Sharpe |
|---|---|---|---|---|---|---|---|
| PF_XBTUSD | -18,12 % | -99,68 | 172 | 37,2 % | 0,80 | 27,18 % | -0,78 |
| PF_ETHUSD | -15,43 % | -84,87 | 164 | 38,4 % | 0,83 | 24,63 % | -0,60 |
| PF_SOLUSD | -10,45 % | -57,46 | 165 | 38,8 % | 0,88 | 14,65 % | -0,41 |
| TOTAL | -44,00 % | -242,01 | 501 | 38,1 % | 0,84 | 55,19 % | -0,82 |

Desglose de costes (negativo = coste; el bruto es el movimiento de precio sin costes):

| Activo | Bruto USD | Comisiones | Slippage | Funding real | Funding imputado | Neto USD |
|---|---|---|---|---|---|---|
| PF_XBTUSD | -31,16 | -26,31 | -26,31 | 0,00 | -15,90 | -99,68 |
| PF_ETHUSD | -27,83 | -21,35 | -21,35 | 0,00 | -14,33 | -84,87 |
| PF_SOLUSD | -23,04 | -12,91 | -12,91 | 0,00 | -8,61 | -57,46 |
| TOTAL | -82,03 | -60,57 | -60,57 | 0,00 | -38,84 | -242,01 |

#### Reservado

2025-05-29 → 2026-10-09. Señales con entrada posible: 438; ignoradas por posición abierta: 190; descartadas por margen: 0.

| Activo | Rent. neta | Neto USD | Ops | Acierto | Profit factor | DD máx | Sharpe |
|---|---|---|---|---|---|---|---|
| PF_XBTUSD | -9,23 % | -50,75 | 85 | 38,8 % | 0,83 | 14,09 % | -0,70 |
| PF_ETHUSD | -7,73 % | -42,53 | 85 | 38,8 % | 0,86 | 14,92 % | -0,55 |
| PF_SOLUSD | 5,66 % | +31,12 | 78 | 44,9 % | 1,13 | 10,32 % | 0,52 |
| TOTAL | -11,30 % | -62,16 | 248 | 40,7 % | 0,93 | 26,41 % | -0,33 |

Desglose de costes (negativo = coste; el bruto es el movimiento de precio sin costes):

| Activo | Bruto USD | Comisiones | Slippage | Funding real | Funding imputado | Neto USD |
|---|---|---|---|---|---|---|
| PF_XBTUSD | -6,60 | -20,25 | -20,25 | +0,22 | -3,87 | -50,75 |
| PF_ETHUSD | -15,51 | -12,64 | -12,64 | +0,14 | -1,88 | -42,53 |
| PF_SOLUSD | +54,57 | -10,75 | -10,75 | -0,38 | -1,58 | +31,12 |
| TOTAL | +32,46 | -43,63 | -43,63 | -0,02 | -7,34 | -62,16 |

### Funding central

#### Desarrollo

2022-03-23 → 2025-05-29. Señales con entrada posible: 1067; ignoradas por posición abierta: 566; descartadas por margen: 0.

| Activo | Rent. neta | Neto USD | Ops | Acierto | Profit factor | DD máx | Sharpe |
|---|---|---|---|---|---|---|---|
| PF_XBTUSD | -16,39 % | -90,16 | 172 | 37,2 % | 0,83 | 26,02 % | -0,68 |
| PF_ETHUSD | -13,34 % | -73,39 | 164 | 38,4 % | 0,85 | 23,59 % | -0,49 |
| PF_SOLUSD | -9,17 % | -50,43 | 165 | 38,8 % | 0,90 | 14,60 % | -0,34 |
| TOTAL | -38,91 % | -213,98 | 501 | 38,1 % | 0,86 | 51,36 % | -0,68 |

Desglose de costes (negativo = coste; el bruto es el movimiento de precio sin costes):

| Activo | Bruto USD | Comisiones | Slippage | Funding real | Funding imputado | Neto USD |
|---|---|---|---|---|---|---|
| PF_XBTUSD | -35,11 | -27,47 | -27,47 | 0,00 | -0,11 | -90,16 |
| PF_ETHUSD | -29,35 | -22,25 | -22,25 | 0,00 | +0,45 | -73,39 |
| PF_SOLUSD | -23,57 | -13,46 | -13,46 | 0,00 | +0,06 | -50,43 |
| TOTAL | -88,02 | -63,18 | -63,18 | 0,00 | +0,40 | -213,98 |

#### Reservado

2025-05-29 → 2026-10-09. Señales con entrada posible: 438; ignoradas por posición abierta: 190; descartadas por margen: 0.

| Activo | Rent. neta | Neto USD | Ops | Acierto | Profit factor | DD máx | Sharpe |
|---|---|---|---|---|---|---|---|
| PF_XBTUSD | -8,75 % | -48,15 | 85 | 38,8 % | 0,84 | 14,14 % | -0,65 |
| PF_ETHUSD | -7,47 % | -41,10 | 85 | 38,8 % | 0,87 | 14,83 % | -0,52 |
| PF_SOLUSD | 5,92 % | +32,57 | 78 | 44,9 % | 1,13 | 10,39 % | 0,54 |
| TOTAL | -10,31 % | -56,68 | 248 | 40,7 % | 0,93 | 25,90 % | -0,29 |

Desglose de costes (negativo = coste; el bruto es el movimiento de precio sin costes):

| Activo | Bruto USD | Comisiones | Slippage | Funding real | Funding imputado | Neto USD |
|---|---|---|---|---|---|---|
| PF_XBTUSD | -6,91 | -20,44 | -20,44 | +0,22 | -0,59 | -48,15 |
| PF_ETHUSD | -15,69 | -12,76 | -12,76 | +0,14 | -0,03 | -41,10 |
| PF_SOLUSD | +54,67 | -10,85 | -10,85 | -0,38 | -0,03 | +32,57 |
| TOTAL | +32,08 | -44,04 | -44,04 | -0,02 | -0,66 | -56,68 |

## 5. Funding: real frente a imputado

- *Cobertura del tramo*: % de las horas del tramo con funding real publicado.
- *Horas en posición con dato real*: % de las horas con posición abierta cubiertas por funding real.
- *% del coste real*: parte del importe de funding (suma de valores absolutos por operación, sin compensar signos) que viene de funding real, en cada escenario.

| Tramo | Activo | Cobertura del tramo | Horas en posición con dato real | % coste real (pesimista) | % coste real (central) |
|---|---|---|---|---|---|
| Desarrollo | PF_XBTUSD | 0,0 % | 0,0 % | 0,0 % | 0,0 % |
| Desarrollo | PF_ETHUSD | 0,0 % | 0,0 % | 0,0 % | 0,0 % |
| Desarrollo | PF_SOLUSD | 0,0 % | 0,0 % | 0,0 % | 0,0 % |
| Desarrollo | TOTAL | 0,0 % | 0,0 % | 0,0 % | 0,0 % |
| Reservado | PF_XBTUSD | 73,8 % | 72,8 % | 50,5 % | 71,6 % |
| Reservado | PF_ETHUSD | 73,8 % | 77,8 % | 60,8 % | 79,3 % |
| Reservado | PF_SOLUSD | 73,8 % | 76,3 % | 62,2 % | 97,0 % |
| Reservado | TOTAL | 73,8 % | 75,7 % | 56,3 % | 79,7 % |

## 6. Buy & hold (contexto)

Largo 1× con todo el capital en un solo activo durante el mismo tramo, con las mismas comisiones, slippage y funding que la estrategia.

| Tramo | Activo | Rent. bruta | Neta (funding central) | Neta (funding pesimista) | DD máx (cierres) |
|---|---|---|---|---|---|
| Desarrollo | PF_XBTUSD | 155,71 % | 144,32 % | 128,33 % | 67,28 % |
| Desarrollo | PF_ETHUSD | -9,19 % | -17,20 % | -28,54 % | 74,12 % |
| Desarrollo | PF_SOLUSD | 88,33 % | 86,66 % | 61,94 % | 93,70 % |
| Reservado | PF_XBTUSD | -23,41 % | -27,28 % | -28,93 % | 53,55 % |
| Reservado | PF_ETHUSD | -6,72 % | -11,60 % | -13,84 % | 68,07 % |
| Reservado | PF_SOLUSD | -35,77 % | -35,99 % | -38,98 % | 75,21 % |

## 7. Robustez ±20 % (solo desarrollo)

Cada parámetro se varía por separado (el resto, en su valor base); los enteros se redondean. Rentabilidad neta de la cartera en el tramo de desarrollo. **No se ejecuta sobre el reservado**, que solo se evalúa una vez con los parámetros base.

| Parámetro | Variación | Valor | Rent. neta (pesimista) | Ops | Rent. neta (central) | Ops |
|---|---|---|---|---|---|---|
| *base* | — | — | -44,00 % | 501 | -38,91 % | 501 |
| sma_len | −20 % | 40 | -48,47 % | 474 | -44,15 % | 474 |
| sma_len | +20 % | 60 | -49,32 % | 513 | -44,62 % | 513 |
| rsi_len | −20 % | 11 | -39,30 % | 510 | -33,61 % | 510 |
| rsi_len | +20 % | 17 | -34,56 % | 491 | -28,72 % | 491 |
| stoch_len | −20 % | 11 | -45,58 % | 539 | -40,28 % | 539 |
| stoch_len | +20 % | 17 | -49,08 % | 480 | -44,83 % | 480 |
| k_smooth | −20 % | 2 | -42,76 % | 554 | -37,15 % | 554 |
| k_smooth | +20 % | 4 | -40,31 % | 484 | -35,07 % | 484 |
| d_smooth | −20 % | 2 | -47,99 % | 525 | -43,06 % | 525 |
| d_smooth | +20 % | 4 | -43,31 % | 484 | -38,28 % | 484 |
| oversold | −20 % | 16 | -40,50 % | 493 | -35,16 % | 493 |
| oversold | +20 % | 24 | -39,92 % | 506 | -34,35 % | 506 |
| overbought | −20 % | 64 | -38,64 % | 523 | -32,69 % | 523 |
| overbought | +20 % | 96 | -37,76 % | 429 | -33,52 % | 429 |
| atr_len | −20 % | 11 | -46,94 % | 506 | -42,19 % | 506 |
| atr_len | +20 % | 17 | -42,85 % | 511 | -37,68 % | 511 |
| stop_atr | −20 % | 1,60 | -62,89 % | 587 | -58,91 % | 587 |
| stop_atr | +20 % | 2,40 | -29,01 % | 449 | -23,29 % | 449 |
| tp_atr | −20 % | 2,40 | -49,03 % | 554 | -44,81 % | 554 |
| tp_atr | +20 % | 3,60 | -40,19 % | 460 | -34,26 % | 460 |

## 8. Comparación con el azar

1000 simulaciones (semilla 20261009) con entradas aleatorias: cada vela elegible dispara, por activo, con la misma frecuencia de señales largas y cortas que la estrategia en ese tramo, sin filtro de régimen ni de oscilador. Mismas salidas (stop y TP en ATR, pesimista), mismo tamaño, apalancamiento, comisiones, slippage y funding. El percentil es el % de simulaciones por debajo de la estrategia (rentabilidad neta).

| Tramo | Funding | Estrategia | Percentil | Mediana azar | P5 azar | P95 azar | Azar > 0 | Ops estrategia / azar (media) |
|---|---|---|---|---|---|---|---|---|
| Desarrollo | pesimista | -44,00 % | **33,0** | -36,71 % | -59,69 % | 1,71 % | 5,5 % | 501 / 536,3 |
| Desarrollo | central | -38,91 % | **32,7** | -30,77 % | -55,90 % | 10,80 % | 9,9 % | 501 / 536,3 |
| Reservado | pesimista | -11,30 % | **53,8** | -12,49 % | -35,17 % | 18,08 % | 24,9 % | 248 / 244,2 |
| Reservado | central | -10,31 % | **53,8** | -11,49 % | -34,44 % | 19,48 % | 26,9 % | 248 / 244,2 |

## 9. Limitaciones

- **Funding imputado**: gran parte del periodo no tiene funding publicado; los resultados del tramo de desarrollo y de parte del reservado dependen de la imputación (sección 5). Por eso el veredicto usa el escenario pesimista.
- Las estadísticas del funding imputado salen del último año, que incluye parte del tramo reservado (afecta solo a costes, no a señales ni parámetros).
- El drawdown se mide con el capital a precios de cierre de las velas de 4h, no intravela; el real puede ser algo mayor. El orden de los extremos dentro de una vela es desconocido: de ahí el criterio pesimista del stop.
- Funding con granularidad de vela: una salida dentro de la vela paga el funding de toda la vela.
- El capital que dimensiona cada operación es el realizado (sin PnL latente de otras posiciones). Con señales simultáneas, el margen lo consume el activo que va antes en el orden BTC, ETH, SOL.
- Ejecución idealizada: sin profundidad de libro, sin rechazos de órdenes, sin liquidación (a ≤ 2× está lejos) y con cantidades continuas.
- Tres activos muy correlacionados: las operaciones no son independientes, así que el número efectivo de observaciones es menor que el de operaciones.
