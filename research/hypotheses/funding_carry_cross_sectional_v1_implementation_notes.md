# funding-carry-cross-sectional-v1: notas de implementación (N1)

Escritas antes de ejecutar ninguna simulación con datos reales. Concretan detalles que el YAML congelado
(SHA-256 adf6b882e5dc7eca3bbf053f97acb8a0bd98295ef4e2f7a7226caed36a35904d) no fija. No cambian ninguna regla.

1. Rejilla diaria. La vela d va de d 00:00 a d+1 00:00 UTC. La valoración en la frontera d usa el cierre mark de la vela d-1.
2. Funding diario. Para la posición mantenida durante el día d se suma absolute_rate de las tasas con ts en [d, d+1). Una posición abierta en la apertura del lunes T cuenta desde ts = T, como dice el YAML. Las horas sin registro no pagan ni cobran.
3. Ejecución en T. Las posiciones anteriores se revalúan del mark de T a la apertura trade de T; esa diferencia es P&L de precio. La orden se ejecuta solo si la vela trade de T tiene volumen y el contrato está en su vida.
4. ADV30 para el deslizamiento: media del volumen en USD de las 30 velas completas anteriores al día de la operación, con ceros donde no hay volumen. Si es 0, se aplican 1.000 pb (10 %), un caso que el YAML no contempla.
5. Lotes. Incremento = 10^(-contractValueTradePrecision) del catálogo de 2026-10-10. Sin especificación: 10^floor(log10(1/precio)), que es como mucho 1 USD de nominal. Si el redondeo deja una apertura en 0, no se abre; se cuenta (rounded_to_zero) y se aplica el equilibrio de lados.
6. Empates. Los cortos se eligen entre los contratos no seleccionados como largos (solo importa con empates en la frontera del quintil).
7. Equilibrio de lados. m = min(L, S, 0,5 x patrimonio previo al rebalanceo). El lado que supera m + 1 % se reduce proporcionalmente entre sus posiciones ejecutables ese día (factor mínimo 0). Hay fallo de ejecución solo si las posiciones no ejecutables de ese lado superan por sí solas m + 1 %.
8. Salidas a mitad de semana (cierres retrasados por falta de operaciones y retiradas) no provocan reajuste del otro lado hasta el lunes siguiente.
9. Retirada. El último día de vida es el día del último funding. La posición cobra o paga el funding de ese día, se valora al cierre mark de ese día y sale a mark x (1 - a) si es larga o mark x (1 + a) si es corta, con comisión y deslizamiento.
10. Fin del walk-forward: las posiciones abiertas el 2025-10-06 00:00 se valoran a mark, sin cierre forzoso ni costes adicionales.
11. Estadísticos:
    - IC 95 % por bootstrap de bloques móviles sobre la CAGR geométrica, como en sma200.
    - Percentil frente a las aleatorias sobre la CAGR.
    - Deflated Sharpe con N = filas de trials_inventory.csv (52) y varianza = máx(varianza del Sharpe diario de las aleatorias principales, varianza entre las tres variantes de la estrategia, 1/(T-1)).
    - Multiplicador de equilibrio por bisección del multiplicador de comisión y deslizamiento hasta patrimonio final = 550.
    - Efecto mínimo detectable = (z0,95 + z0,80) x mediana de la desviación diaria de las aleatorias / raíz(n días) x 365.
12. Betas: regresión diaria con errores Newey-West (10 retardos) contra el rendimiento mark de PF_XBTUSD y contra la equiponderada larga del universo elegible del último lunes, sin costes.
13. Aleatorias que sufren un fallo de ejecución se descartan y se cuentan. Las aleatorias principales y las independientes usan las semillas 20261010 y 20261011.
14. La elegibilidad se recalcula en el código y se comprueba que coincide semana a semana con data/audit/funding_carry_eligibility_weekly.csv; si no coincide, la ejecución se detiene.
15. Letras del Tesoro: media de la columna de 13 semanas coupon equivalent de Daily Treasury Bill Rates en los días hábiles del periodo. Es informativo.
