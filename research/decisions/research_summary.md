# Registro consolidado de investigación (2026-10-11)

Estado global: ninguna estrategia habilitada para paper ni para dinero real. `live_trading: false` en todos los experimentos.
Inventario de ensayos: 52 (research/trials_inventory.csv).

## Qué dicen y qué no dicen los resultados

Los rechazos no significan que las rentabilidades observadas sean ficticias, ni que el funding o la tendencia no puedan ser útiles. Significan que ningún experimento ha demostrado una ventaja lo bastante robusta, después de costes y riesgo, para justificar seguir con esa estrategia.

## Experimentos

| Experimento | Familia | Veredicto | Motivo principal |
|---|---|---|---|
| copy-trading | Copiar líderes de Hyperliquid | Rechazado | Candidatos no replicables (margen unificado); el restante, con drawdown cercano al 70 % |
| trend-4h-core | SMA + Stoch RSI en 4 h | Rechazado | Holdout -11,3 %; DD 26,4 %; percentil aleatorias 53,8; costes 87 USD frente a 32 brutos |
| trend-4h-volatile | SMA + Stoch RSI en 4 h | Rechazado | Holdout -4,2 %; percentil aleatorias 38,2 |
| funding-A | Arbitraje de funding entre plataformas | Rechazado en desarrollo | Funding +14,8 USD frente a costes -49,1 y transferencias -6 |
| funding-B | Cash-and-carry | Rechazado en desarrollo | Funding +5,6 USD frente a costes -100,7 |
| ml-daily-direction-multiasset-v4 | ML diario largo/corto (LightGBM) | Rechazado en walk-forward | DD 16,7 %; alfa p = 0,55; DSR 0,20; peor que la equiponderada |
| trend-daily-sma200 | SMA200 diaria | long_short rechazada; long_flat inconclusa | DD 16,5 %; percentiles frente a aleatorias 74,7 y 48,9 |
| funding-carry-cross-sectional-v1 | Carry de funding transversal | Rechazado en walk-forward | DD 23,4 %; funding apenas cubre costes (+1,5 USD; -106,6 con costes adversos); IC 95 % incluye 0; DSR 0,09 |

## Los tres modos de fallo

1. **Ventaja estadística insuficiente.** Ninguna señal supera de forma concluyente a referencias sencillas o aleatorias con la misma exposición:
   - v4: sin alfa frente a la equiponderada;
   - SMA200: el momento de entrada no aporta frente a cambios aleatorios;
   - carry: el IC incluye 0 y el Deflated Sharpe es 0,09;
   - tendencia en 4 h: percentiles 38 a 54.

   Con muestras de uno o dos años, el efecto mínimo detectable es enorme (unos 45 % anuales en el carry). Solo una ventaja muy grande sería demostrable.
2. **Costes que erosionan la supuesta ventaja.** Comisiones, deslizamiento y rotación se comen la ventaja bruta:
   - tendencia en 4 h: 32 USD brutos frente a 87 de costes;
   - funding-A y funding-B: el funding no cubre los costes;
   - carry transversal: rota 56 veces el patrimonio al año, con costes del 10 % del capital anual.

   El funding ha fallado así en tres formas distintas.
3. **Riesgo incompatible con los límites.** Drawdowns por encima del 15 % en v4 (16,7 %), SMA200 largo/corto (16,5 %), carry (23,4 %) y tendencia en 4 h (26,4 %). Lo que sí funciona es reducir la exposición o la volatilidad, y eso no es una ventaja de predicción.

## Familias ya probadas (no repetir con otro nombre)

Una hipótesis nueva cuenta como la misma familia si usa la misma fuente de señal y solo cambia parámetros, ventanas, umbrales, universo, frecuencia o la forma de seleccionar (por ejemplo, quintiles frente a terciles).
- Dirección diaria con ML sobre precio y volumen.
- Momento de entrada por medias móviles u osciladores (4 h y diario).
- Funding: arbitraje entre plataformas, cash-and-carry y ranking transversal semanal.
- Copiar operaciones de terceros.

Ideas sugeridas por estos resultados, como ponerse corto en contratos con funding alto por riesgo de caída a partir del componente de precio del carry, quedan contaminadas por los datos ya vistos. Solo podrían validarse hacia delante.

## Estado de los periodos reservados

- v4: holdout del 2025-07-01 al 2026-07-01, no consumido (no existe HOLDOUT_UNLOCK).
- funding-carry v1: holdout del 2025-10-06 al 2026-10-05, no consumido (no existe HOLDOUT_UNLOCK). Era retrospectivo y no del todo ciego.
- Ambos se solapan entre octubre de 2025 y junio de 2026, y ese periodo ya se ha visto en parte. Los únicos datos realmente nuevos son los que recoge el VPS desde el 2026-10-10.
- Ningún holdout se usará para elegir o rescatar variantes.

## Lecciones de proceso

- Calcular costes y rotación antes que la señal: la mayoría de las ideas mueren ahí.
- Comparar siempre con referencias aleatorias que igualen exposición y rotación, no solo con efectivo o buy-and-hold.
- Calcular la potencia (efecto mínimo detectable) antes de ejecutar, para saber si el experimento puede responder la pregunta.
- Incluir los activos retirados: Kraken purga los pares spot retirados del archivo OHLCVT; en Futures los precios existen, pero la API rellena con velas falsas después de la retirada.
- Registrar todo lo visto antes de diseñar (contaminación) y contar cada ensayo.

## Activos reutilizables

- Datos auditados:
  - spot OHLCVT de 9 activos;
  - funding horario y velas diarias de 504 perpetuos de Kraken Futures (vivos y retirados), unidos y verificados;
  - letras del Tesoro 2024-2026.
- VPS con recolector de datos públicos activo desde 2026-10-10.
- Código con pruebas: pipeline_v4, sma200_v1, funding_carry_v1.
- Plantillas de prerregistro, validadores y decisions/decision_log.md.
