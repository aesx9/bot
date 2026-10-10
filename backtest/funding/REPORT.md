# Backtest de arbitraje de funding neutral al precio

Informe generado por `python -m backtest.funding ejecutar`; no editar a mano. Posiciones en `backtest/funding/resultados/`.

- Commit congelado: `8f510419fd9d308ddb1f50e0f3488befe7491dec`. Datos descargados: 2026-10-10T10:48:20Z.
- Tramo reservado: **no ejecutado** (`--solo-desarrollo`).

| Estrategia | Veredicto |
|---|---|
| A — Entre plataformas: Kraken Futures y Hyperliquid | sin emitir |
| B — Cash and carry en Kraken: spot largo + perpetuo corto | sin emitir |

## A — Entre plataformas: Kraken Futures y Hyperliquid

### Veredicto

| Criterio | Resultado | Estado |
|---|---|---|
| Rentabilidad anualizada en el reservado ≥ 6 % | sin ejecutar | n/e |
| Rentabilidad anualizada en desarrollo > 3 % | -16,62 % | ❌ no cumple |
| Drawdown máximo < 5 % (en cada tramo) | desarrollo 6,61 % | ❌ no cumple |
| Ninguna liquidación simulada | desarrollo 0 | n/e |
| Con los umbrales ±20 % sigue > 3 % en desarrollo | 0/6 variantes; peor entrada -20 %: -24,68 % | ❌ no cumple |
| Ningún activo aporta más del 50 % del beneficio (reservado) | sin ejecutar | n/e |
| Al menos 10 ciclos completos en el reservado | sin ejecutar | n/e |

**Veredicto: sin emitir.** El tramo reservado no se ha ejecutado (`--solo-desarrollo`).

### Datos

Ventana: 2026-03-17 → 2026-10-10 (207,0 días, 4968 h). Activos: BTC, ETH, SOL, XRP, ZEC, HYPE, DOGE.

Velas de 1h `trade` de Kraken Futures y `candleSnapshot` de Hyperliquid, funding real horario de ambas plataformas. Solo datos reales: ningún precio de Hyperliquid se sustituye por el de Kraken. La ventana empieza el 2026-03-17 porque `candleSnapshot` solo sirve las 5000 velas de 1h más recientes y en la descarga ya no tenía las primeras del 2026-03-16.

**Regla de datos** (fijada antes de ver resultados): un activo entra solo si tiene una vela real en cada hora de la ventana en todas sus series de precio (ninguna ausencia). Las horas sin funding en cualquier plataforma, en la ventana o en las 24 h de calentamiento, cuentan como funding cero (sin rellenar) si no superan el 0,5 % de las horas de la ventana; por encima, el activo se excluye. Nunca se acorta la ventana.

Ningún activo del universo excluido por datos.

| Activo | Velas de la fuente sin operaciones | Horas sin funding (= 0) |
|---|---|---|
| BTC | Kraken: 0, Hyperliquid: 0 | Kraken: 1, Hyperliquid: 0 |
| ETH | Kraken: 0, Hyperliquid: 0 | Kraken: 1, Hyperliquid: 0 |
| SOL | Kraken: 0, Hyperliquid: 0 | Kraken: 0, Hyperliquid: 0 |
| XRP | Kraken: 0, Hyperliquid: 0 | Kraken: 0, Hyperliquid: 0 |
| ZEC | Kraken: 0, Hyperliquid: 0 | Kraken: 0, Hyperliquid: 0 |
| HYPE | Kraken: 0, Hyperliquid: 0 | Kraken: 0, Hyperliquid: 0 |
| DOGE | Kraken: 0, Hyperliquid: 0 | Kraken: 0, Hyperliquid: 0 |

Horas sin funding que cuentan como cero:

- BTC (Kraken): 2026-05-09T06:00:00Z
- ETH (Kraken): 2026-05-09T06:00:00Z

### Reglas y supuestos

- **Reglas**: Diferencial = funding de Hyperliquid − funding de Kraken. Entra si la media de 24 h anualizada supera 20 % en valor absoluto (corto donde se paga más, largo donde se paga menos); sale si baja de 5 % o cambia de signo.
- **Decisión** cada hora, a la apertura, con funding ya liquidado (las horas anteriores completas); ejecución a la apertura de la vela de 1h de cada pierna. Sin media completa de 24 h no hay decisión.
- **Posiciones**: máximo 3 simultáneas, mismo nocional, misma cantidad de base en las dos piernas. Si hay más candidatos que huecos, entran los de mayor diferencial.
- **Capital**: Capital 550,00 USD: una transferencia inicial (3,00 USD) y el resto a partes iguales entre plataformas; apalancamiento por pierna ≤ 2x. Nocional por pierna y posición: 178,76 USD. Cada cuenta reserva un 2 % para comisiones y funding (el nocional se divide entre 1,02). Una entrada que superase el apalancamiento máximo de su cuenta se descarta.
- **Costes**: Kraken Futures taker 0,05 %, Hyperliquid taker 0,045 % (tier 0), slippage 5 pb por pierna en entrada y salida. **Transferencias: 3,00 USD por movimiento (supuesto conservador documentado: la retirada de Hyperliquid cuesta 1 USDC; la de Kraken no está verificada)**, al empezar cada tramo y en cada reequilibrio. Reequilibrio: si al cierre de una hora una plataforma tiene menos del 50 % de la media de las dos, se transfiere la mitad de la diferencia; **el importe tarda 2 h en llegar y mientras tanto no cuenta como margen en ninguna plataforma** (no se lanza otro reequilibrio hasta que llega).
- **Funding**: tasa relativa horaria × cantidad × apertura de la hora; una hora sin dato cuenta como funding cero, también en la media de 24 h (ver Datos), y se anota.
- **Liquidación**: cada hora, con los mínimos (largos) y máximos (cortos) de la vela a la vez, se compara el capital de cada cuenta con margen con su margen de mantenimiento (Kraken: primer tramo minorista del instrumento; Hyperliquid: 1 / (2 × apalancamiento máximo)). Si cae por debajo, todas las piernas de esa cuenta se cierran al extremo y se pierde el margen de mantenimiento; la cobertura se cierra al cierre de la vela.
- **Tramos**: 70 % desarrollo / 30 % reservado por tiempo; cada uno empieza con el capital inicial y cierra al final lo abierto (no cuenta como ciclo completo).
- **Rentabilidad anualizada** = neto / capital × 8760 / horas del tramo (simple). Referencia: 3 % anual sin riesgo sobre el mismo capital.

### Resultados

| Tramo | Periodo | Neto USD | Rent. neta | Anualizada | Ref. 3 % (USD) | DD máx | Posiciones | Ciclos completos | Duración media | Diferencial capturado |
|---|---|---|---|---|---|---|---|---|---|---|
| Desarrollo | 2026-03-17 → 2026-08-08 (144,9 días) | -36,27 | -6,60 % | -16,62 % | +6,55 | 6,61 % | 70 | 70 | 52,8 h (2,2 días) | 19,63 % |

### Desglose

| Tramo | Funding cobrado | Funding pagado | Comisiones | Slippage | Resultado por base | Transferencias | Coste transferencias | Liquidaciones | Pérdida por liquidación | Neto USD |
|---|---|---|---|---|---|---|---|---|---|---|
| Desarrollo | +25,52 | -10,71 | -23,93 | -25,19 | +4,03 | 2 | -6,00 | 0 | 0,00 | -36,27 |

Signo: positivo suma al resultado. Neto = funding cobrado − pagado − comisiones − slippage + base − transferencias (la pérdida por liquidación ya está dentro de las columnas anteriores; incluye el margen de mantenimiento perdido).

| Tramo | Entradas descartadas (3 posiciones) | Entradas descartadas (margen) | Horas-activo sin media de 24 h | Horas de posición sin dato de funding |
|---|---|---|---|---|
| Desarrollo | 0 | 133 | 0 | 0 |

### Liquidaciones

Ninguna liquidación simulada en ningún tramo.

### Robustez ±20 % de los umbrales

Solo en desarrollo; cada umbral por separado y ambos a la vez.

| Variante | Entrada | Salida | Anualizada | DD máx | Ciclos | Liquidaciones |
|---|---|---|---|---|---|---|
| entrada -20 % | 16,00 % | 5,00 % | -24,68 % | 9,80 % | 99 | 0 |
| salida -20 % | 20,00 % | 4,00 % | -16,35 % | 6,51 % | 69 | 0 |
| ambos -20 % | 16,00 % | 4,00 % | -24,42 % | 9,69 % | 97 | 0 |
| entrada +20 % | 24,00 % | 5,00 % | -12,35 % | 4,90 % | 54 | 0 |
| salida +20 % | 20,00 % | 6,00 % | -17,33 % | 6,90 % | 71 | 0 |
| ambos +20 % | 24,00 % | 6,00 % | -13,01 % | 5,16 % | 56 | 0 |

### Por activo

Solo informativo.

| Activo | Tramo | Posiciones | Ciclos | Funding neto | Base | Costes | Neto USD | Duración media |
|---|---|---|---|---|---|---|---|---|
| DOGE | Desarrollo | 3 | 3 | +0,05 | -0,47 | -2,07 | -2,49 | 21,0 h (0,9 días) |
| ETH | Desarrollo | 1 | 1 | +0,07 | +0,12 | -0,71 | -0,52 | 41,0 h (1,7 días) |
| HYPE | Desarrollo | 31 | 31 | +3,32 | +2,50 | -21,55 | -15,73 | 40,5 h (1,7 días) |
| SOL | Desarrollo | 6 | 6 | +0,50 | -0,23 | -4,21 | -3,94 | 45,0 h (1,9 días) |
| ZEC | Desarrollo | 29 | 29 | +10,88 | +2,11 | -20,58 | -7,59 | 71,3 h (3,0 días) |

## B — Cash and carry en Kraken: spot largo + perpetuo corto

### Veredicto

| Criterio | Resultado | Estado |
|---|---|---|
| Rentabilidad anualizada en el reservado ≥ 6 % | sin ejecutar | n/e |
| Rentabilidad anualizada en desarrollo > 3 % | -23,09 % | ❌ no cumple |
| Drawdown máximo < 5 % (en cada tramo) | desarrollo 17,75 % | ❌ no cumple |
| Ninguna liquidación simulada | desarrollo 0 | n/e |
| Con los umbrales ±20 % sigue > 3 % en desarrollo | 0/6 variantes; peor salida +20 %: -22,84 % | ❌ no cumple |
| Ningún activo aporta más del 50 % del beneficio (reservado) | sin ejecutar | n/e |
| Al menos 10 ciclos completos en el reservado | sin ejecutar | n/e |

**Veredicto: sin emitir.** El tramo reservado no se ha ejecutado (`--solo-desarrollo`). Evaluado con la pierna spot como **maker**.

### Datos

Ventana: 2025-10-10 → 2026-10-10 (365,0 días, 8760 h). Activos: BTC, ETH, SOL, XRP, ZEC, HYPE, DOGE.

Perpetuo: velas de 1h `trade` de Kraken Futures y funding real horario. **Spot: índice spot de la API de gráficos de Kraken Futures (`/api/charts/v1/spot/PF_*/1h`) como aproximación del precio spot de Kraken**; el índice agrega varias plataformas y no es el libro de órdenes spot de Kraken.

**Regla de datos** (fijada antes de ver resultados): un activo entra solo si tiene una vela real en cada hora de la ventana en todas sus series de precio (ninguna ausencia). Las horas sin funding en cualquier plataforma, en la ventana o en las 24 h de calentamiento, cuentan como funding cero (sin rellenar) si no superan el 0,5 % de las horas de la ventana; por encima, el activo se excluye. Nunca se acorta la ventana.

Ningún activo del universo excluido por datos.

| Activo | Velas de la fuente sin operaciones | Horas sin funding (= 0) |
|---|---|---|
| BTC | perpetuo: 6, índice spot: 2 | Kraken: 8 |
| ETH | perpetuo: 6, índice spot: 2 | Kraken: 8 |
| SOL | perpetuo: 6, índice spot: 2 | Kraken: 7 |
| XRP | perpetuo: 6, índice spot: 2 | Kraken: 7 |
| ZEC | perpetuo: 7, índice spot: 2 | Kraken: 7 |
| HYPE | perpetuo: 20, índice spot: 2 | Kraken: 7 |
| DOGE | perpetuo: 7, índice spot: 2 | Kraken: 7 |

Horas sin funding que cuentan como cero:

- BTC (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z, 2026-05-09T06:00:00Z
- ETH (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z, 2026-05-09T06:00:00Z
- SOL (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z
- XRP (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z
- ZEC (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z
- HYPE (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z
- DOGE (Kraken): 2025-11-01T16:00:00Z, 2025-11-01T17:00:00Z, 2025-11-02T04:00:00Z, 2025-11-19T05:00:00Z, 2025-12-04T09:00:00Z, 2026-02-04T12:00:00Z, 2026-02-13T18:00:00Z

### Reglas y supuestos

- **Reglas**: Entra si la media de 24 h del funding de Kraken, anualizada, supera 10 % (los largos pagan): largo spot + corto perpetuo. Sale si baja de 2 % o se vuelve negativa.
- **Decisión** cada hora, a la apertura, con funding ya liquidado (las horas anteriores completas); ejecución a la apertura de la vela de 1h de cada pierna. Sin media completa de 24 h no hay decisión.
- **Posiciones**: máximo 3 simultáneas, mismo nocional, misma cantidad de base en las dos piernas. Si hay más candidatos que huecos, entran los de mayor diferencial.
- **Capital**: Capital 550,00 USD: mitad para comprar spot y mitad como margen del corto (1x). Nocional por pierna y posición: 89,87 USD. Cada cuenta reserva un 2 % para comisiones y funding (el nocional se divide entre 1,02). Una entrada que superase el apalancamiento máximo de su cuenta se descarta.
- **Costes**: Kraken spot maker 0,40 % (evaluado) y taker 0,80 % (referencia pesimista); perpetuo taker 0,05 %; slippage 5 pb por pierna en entrada y salida (también en la pierna maker). Sin transferencias entre plataformas (0 movimientos).
- **Funding**: tasa relativa horaria × cantidad × apertura de la hora; una hora sin dato cuenta como funding cero, también en la media de 24 h (ver Datos), y se anota.
- **Liquidación**: cada hora, con los mínimos (largos) y máximos (cortos) de la vela a la vez, se compara el capital de cada cuenta con margen con su margen de mantenimiento (Kraken: primer tramo minorista del instrumento; Hyperliquid: 1 / (2 × apalancamiento máximo)). Si cae por debajo, todas las piernas de esa cuenta se cierran al extremo y se pierde el margen de mantenimiento; la cobertura se cierra al cierre de la vela.
- **Tramos**: 70 % desarrollo / 30 % reservado por tiempo; cada uno empieza con el capital inicial y cierra al final lo abierto (no cuenta como ciclo completo).
- **Rentabilidad anualizada** = neto / capital × 8760 / horas del tramo (simple). Referencia: 3 % anual sin riesgo sobre el mismo capital.

### Resultados

| Tramo | Periodo | Neto USD | Rent. neta | Anualizada | Ref. 3 % (USD) | DD máx | Posiciones | Ciclos completos | Duración media | Diferencial capturado |
|---|---|---|---|---|---|---|---|---|---|---|
| Desarrollo | 2025-10-10 → 2026-06-22 (255,5 días) | -88,88 | -16,16 % | -23,09 % | +11,55 | 17,75 % | 102 | 101 | 64,3 h (2,7 días) | 8,34 % |

### Desglose

| Tramo | Funding cobrado | Funding pagado | Comisiones | Slippage | Resultado por base | Transferencias | Coste transferencias | Liquidaciones | Pérdida por liquidación | Neto USD |
|---|---|---|---|---|---|---|---|---|---|---|
| Desarrollo | +7,90 | -2,29 | -82,39 | -18,31 | +6,21 | 0 | 0,00 | 0 | 0,00 | -88,88 |

Signo: positivo suma al resultado. Neto = funding cobrado − pagado − comisiones − slippage + base − transferencias (la pérdida por liquidación ya está dentro de las columnas anteriores; incluye el margen de mantenimiento perdido).

| Tramo | Entradas descartadas (3 posiciones) | Entradas descartadas (margen) | Horas-activo sin media de 24 h | Horas de posición sin dato de funding |
|---|---|---|---|---|
| Desarrollo | 23 | 801 | 0 | 10 |

### Liquidaciones

Ninguna liquidación simulada en ningún tramo.

### Referencia pesimista: pierna spot como taker (no se evalúa)

| Tramo | Periodo | Neto USD | Rent. neta | Anualizada | Ref. 3 % (USD) | DD máx | Posiciones | Ciclos completos | Duración media | Diferencial capturado |
|---|---|---|---|---|---|---|---|---|---|---|
| Desarrollo (spot taker) | 2025-10-10 → 2026-06-22 (255,5 días) | -137,97 | -25,09 % | -35,84 % | +11,55 | 26,36 % | 87 | 86 | 68,5 h (2,9 días) | 8,28 % |

| Tramo | Funding cobrado | Funding pagado | Comisiones | Slippage | Resultado por base | Transferencias | Coste transferencias | Liquidaciones | Pérdida por liquidación | Neto USD |
|---|---|---|---|---|---|---|---|---|---|---|
| Desarrollo (spot taker) | +7,10 | -2,04 | -132,85 | -15,63 | +5,45 | 0 | 0,00 | 0 | 0,00 | -137,97 |

Signo: positivo suma al resultado. Neto = funding cobrado − pagado − comisiones − slippage + base − transferencias (la pérdida por liquidación ya está dentro de las columnas anteriores; incluye el margen de mantenimiento perdido).

### Días para cubrir los costes de un ciclo

| Spot | Coste del ciclo (sobre nocional) | Días al umbral de entrada (10 %) | Días al capturado medio (Desarrollo) |
|---|---|---|---|
| maker | 1,100 % | 40,1 | 48,1 |
| taker | 1,900 % | 69,4 | 83,1 |

Coste del ciclo = 2 × (comisión spot + comisión perpetuo) + 4 × slippage.

Duración real de los periodos de funding alto (media de 24 h por encima del 10 % hasta que baja del 2 %), en toda la ventana y sin límite de posiciones:

| Activo | Periodos | Media | Mediana | Máximo |
|---|---|---|---|---|
| BTC | 17 | 119,4 h (5,0 días) | 54,0 h (2,2 días) | 785,0 h (32,7 días) |
| ETH | 26 | 100,9 h (4,2 días) | 68,5 h (2,9 días) | 416,0 h (17,3 días) |
| SOL | 40 | 36,2 h (1,5 días) | 23,0 h (1,0 días) | 162,0 h (6,8 días) |
| XRP | 24 | 55,4 h (2,3 días) | 45,5 h (1,9 días) | 156,0 h (6,5 días) |
| ZEC | 62 | 48,5 h (2,0 días) | 36,0 h (1,5 días) | 212,0 h (8,8 días) |
| HYPE | 77 | 56,2 h (2,3 días) | 37,0 h (1,5 días) | 266,0 h (11,1 días) |
| DOGE | 31 | 65,9 h (2,7 días) | 46,0 h (1,9 días) | 197,0 h (8,2 días) |

### Robustez ±20 % de los umbrales

Solo en desarrollo; cada umbral por separado y ambos a la vez.

| Variante | Entrada | Salida | Anualizada | DD máx | Ciclos | Liquidaciones |
|---|---|---|---|---|---|---|
| entrada -20 % | 8,00 % | 2,00 % | -16,05 % | 12,92 % | 72 | 0 |
| salida -20 % | 10,00 % | 1,60 % | -21,99 % | 17,00 % | 96 | 0 |
| ambos -20 % | 8,00 % | 1,60 % | -15,68 % | 12,67 % | 71 | 0 |
| entrada +20 % | 12,00 % | 2,00 % | -17,42 % | 13,32 % | 78 | 0 |
| salida +20 % | 10,00 % | 2,40 % | -22,84 % | 17,58 % | 98 | 0 |
| ambos +20 % | 12,00 % | 2,40 % | -17,99 % | 13,71 % | 79 | 0 |

### Por activo

Solo informativo.

| Activo | Tramo | Posiciones | Ciclos | Funding neto | Base | Costes | Neto USD | Duración media |
|---|---|---|---|---|---|---|---|---|
| BTC | Desarrollo | 3 | 3 | +0,87 | +0,49 | -2,89 | -1,53 | 351,7 h (14,7 días) |
| DOGE | Desarrollo | 9 | 8 | +0,04 | +0,66 | -8,68 | -7,98 | 46,2 h (1,9 días) |
| ETH | Desarrollo | 5 | 5 | +0,41 | +0,07 | -4,85 | -4,38 | 128,8 h (5,4 días) |
| HYPE | Desarrollo | 42 | 42 | +3,04 | +0,84 | -41,48 | -37,60 | 60,8 h (2,5 días) |
| SOL | Desarrollo | 11 | 11 | +0,16 | +0,90 | -10,85 | -9,78 | 42,8 h (1,8 días) |
| XRP | Desarrollo | 3 | 3 | +0,12 | -0,92 | -2,87 | -3,66 | 55,3 h (2,3 días) |
| ZEC | Desarrollo | 29 | 29 | +0,98 | +4,16 | -29,09 | -23,94 | 43,3 h (1,8 días) |

## Limitaciones

- Universo elegido con el volumen de los 90 días previos a la descarga, que se solapan con el final de las ventanas (sesgo de selección asumido por la regla fija).
- Ejecución a la apertura de la vela con slippage fijo; sin profundidad de libro.
- Transferencias con coste fijo supuesto; el reequilibrio tarda 2 h en llegar (supuesto, no medido).
- Liquidación en el peor caso simultáneo de todas las piernas de una cuenta.
- B usa el índice spot de Kraken Futures como aproximación del spot de Kraken.
