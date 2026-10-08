# Auditoría de seguridad y riesgo

> Auditoría de la rama `claude/verify-api-access-formats-pxwzed` (commit base
> `31ab5bb`). Cada hallazgo lleva su **Estado**: `pendiente`, `resuelto` (con el
> commit de la corrección) o `pendiente (documentado)` cuando se decide no
> corregirlo todavía. Los commits de corrección empiezan por el ID del hallazgo
> (`git log --grep '^A1:'`).

## Método

- `make check` completo sobre el commit base: ruff, mypy strict, 403 tests,
  pip-audit (60 dependencias, 0 vulnerabilidades) y detect-secrets.
- Lectura completa de `src/`, `deploy/`, `scripts/` y los tests.
- PoCs (letras A-M en la columna *PoC*) escritos sobre los dobles de prueba del
  repo. Cada corrección añade un test de regresión que invierte su PoC.
- 73 mutaciones sobre una copia: los tests detectaron 63 y sobrevivieron 10
  (ver M14).
- Escaneo del historial de git de todas las ramas: sin secretos.
- Limitación: nada se ha probado contra Kraken ni Hyperliquid reales.

## Decisiones de diseño (respuestas del usuario)

- **A3:** `data_dir` separado por modo (`<data_dir>/paper`, `<data_dir>/live`),
  modo guardado en el estado y negativa a arrancar si no coincide.
- **M10:** es fatal arrancar en live por primera vez con cualquier posición
  abierta en Kraken Futures.
- **M13:** distancia del stop de catástrofe = `max_drawdown_pct` / apalancamiento
  efectivo (con el perfil de arranque a 1x, 15 %). Los HALT siguen sin cierre
  automático, pero con alerta crítica.
- **M8:** healthcheck externo opcional (URL en `.env`), `OnFailure=` y manejo de
  SIGTERM.
- **B5 y B9:** se documentan como pendientes (B9 se verificará en la prueba
  supervisada con dinero real).

## Resumen

| ID | Gravedad | Hallazgo | PoC | Estado |
|---|---|---|---|---|
| A1 | Alta | Una excepción no prevista mata el bucle y el bot sigue "vivo" sin operar | B, C | pendiente |
| A2 | Alta | Posiciones huérfanas si el ciclo se aborta a mitad de ejecución | J, J2 | pendiente |
| A3 | Alta | Estado compartido entre paper y live | E | pendiente |
| A4 | Alta | Los fills del primer ciclo live no entran en el libro fiscal | A | pendiente |
| A5 | Alta | Cierre de emergencia frágil | I, K | pendiente |
| M1 | Media | Tamaños y precios en notación científica en `sendorder` | formato | pendiente |
| M2 | Media | Stops de catástrofe: tarde, reemplazo no atómico, sobreviven al cierre | G, G2, L | pendiente |
| M3 | Media | Libro fiscal sin deduplicación ni conciliación | D | pendiente |
| M4 | Media | Moneda y signo de funding y comisiones sin verificar | (lectura) | pendiente |
| M5 | Media | Puerta `--check` mal invalidada y sin revalidar permisos | M | pendiente |
| M6 | Media | "SSH solo con clave" puede no aplicarse | (OpenSSH) | pendiente |
| M7 | Media | `copybot-cli --status` falla con el servicio activo | (tests) | pendiente |
| M8 | Media | Sin vigilancia externa, watchdog ni manejo de SIGTERM | (lectura) | pendiente |
| M9 | Media | El kill switch puede tardar hasta 1 h | (lectura) | pendiente |
| M10 | Media | Posiciones manuales en mercados del líder se adoptan | E | pendiente |
| M11 | Media | Sin coherencia de precios HL↔Kraken; `max_position_size` sin usar | (lectura) | pendiente |
| M12 | Media | Los topes se aplican al objetivo, no a la exposición real | (lectura) | pendiente |
| M13 | Media | Tras un HALT las posiciones quedan con stops laxos | (lectura) | pendiente |
| M14 | Media | Lagunas de tests (mutaciones supervivientes) | mutación | pendiente |
| B1 | Baja | `config.toml` no ignorado; pre-commit voluntario; sin CI | (lectura) | pendiente |
| B2 | Baja | `Authorization: Bearer x` deja el token | (lectura) | pendiente |
| B3 | Baja | Año fiscal y día BCE en UTC en vez de Madrid | (lectura) | pendiente |
| B4 | Baja | Export no atómico; funding asignable a dos posiciones | (lectura) | pendiente |
| B5 | Baja | `PaperAccount.orders` crece sin límite | (lectura) | pendiente (documentado) |
| B6 | Baja | Telegram: dedupe también de críticas; `InvalidURL` sin capturar | (lectura) | pendiente |
| B7 | Baja | `pip --upgrade pip` sin hash; sin backups | (lectura) | pendiente |
| B8 | Baja | Fila duplicada en `trades.csv` tras caída | (lectura) | pendiente |
| B9 | Baja | Posible desfase de `/openpositions` tras un fill | (no verificable) | pendiente (documentado) |

Gravedad crítica: ninguna. El perfil de arranque (1x, 100 USD por activo) acota
la pérdida; las altas debilitan justo las redes de seguridad.

---

## Altas

### A1 — Una excepción no prevista mata el bucle en silencio
**Gravedad:** alta · **Estado:** pendiente

- **Dónde:** `engine.py` (`CYCLE_ERRORS`, `reconcile_loop`, `run_forever`),
  `hyperliquid_ws.py` (`run`), `live.py` (parseo del `account-log`).
- **Escenario (PoC B y C):** una entrada del `account-log` sin `date`
  (`KeyError`), una fecha ilegible (`ValueError`), un `openPositions` sin
  `symbol` o un `OSError` en `_save()` dentro de `_halt()` no están en
  `CYCLE_ERRORS`. La excepción sale de `cycle()`, mata la tarea REST (sin
  `try`) y nadie la recoge: `run_forever` solo espera `stopped`. Con `KeyError`,
  una consulta al líder en 7 s (con `LeaderDataError`, dos), sin errores
  contados, sin parada ni alerta; el heartbeat sigue vivo.
- **Corrección:** capturar `Exception` en el ciclo y contarla como error;
  supervisar las tres tareas y salir con código ≠ 0 si una muere; validar el
  `account-log` dentro de `ExchangeError`.

### A2 — Posiciones huérfanas
**Gravedad:** alta · **Estado:** pendiente

- **Dónde:** `engine.py` (actualización de `managed_symbols`),
  `executor.py` (`execute`).
- **Escenario (PoC J y J2):** `managed_symbols` solo se actualiza al terminar
  todas las acciones. Si el ciclo se aborta tras abrir una posición (breaker,
  límite duro, `OrderUncertain`, `OSError`, caída), la posición existe sin
  constar como gestionada ni tener stop: el kill switch "cierra" sin cerrar nada
  y tras `--reset-halt` con el líder plano tampoco se cierra.
- **Corrección:** registrar el símbolo y guardar antes de enviar; sincronizar
  stops también cuando el ciclo se aborta.

### A3 — Estado compartido entre paper y live
**Gravedad:** alta · **Estado:** pendiente

- **Dónde:** `state.py` (`BotState` sin modo), `main.py`, `checks.py`.
- **Escenario (PoC E):** el README reutiliza el mismo `state.json` al pasar de
  paper a live. Se heredan `managed_symbols`, `preexisting*`, el pico de capital
  simulado y el registro del breaker: con una posición manual de 0,01 BTC y el
  líder sin BTC, el primer ciclo live la cierra.
- **Corrección (decisión de diseño):** directorio de datos separado por modo,
  modo guardado en el estado y negativa a arrancar si no coincide.

### A4 — Los fills del primer ciclo live no entran en el libro fiscal
**Gravedad:** alta · **Estado:** pendiente

- **Dónde:** `live.py` (`_poll_fills`, `collect_funding`), `engine.py` (orden de
  `execute` y `_after_trading`).
- **Escenario (PoC A):** la primera llamada a `collect_funding` ocurre después de
  que el primer ciclo opere; marca como vistos y descarta los fills de esas
  órdenes y sus comisiones. El export no declara la posición cerrada y avisa de
  un corto abierto inexistente.
- **Corrección:** fijar la línea base del libro antes del primer envío.

### A5 — Cierre de emergencia frágil
**Gravedad:** alta · **Estado:** pendiente

- **Dónde:** `engine.py` (`_kill_switch`, `_close_all_managed`), `planner.py`,
  `executor.py`.
- **Escenario (PoC I y K):** un solo mercado sin `MarketSpec` hace fallar el plan
  de todos; `kill_switch_closed=True` se fija aunque quede algo abierto y solo
  hay ~10 s de intentos; con el disco lleno no sale ninguna orden de cierre
  porque se persiste antes de enviar.
- **Corrección:** cerrar símbolo a símbolo con errores aislados; reintentar
  mientras queden posiciones; en emergencia, enviar aunque falle el guardado.

---

## Medias

### M1 — Notación científica en `sendorder`
**Gravedad:** media · **Estado:** pendiente

`Decimal(1).scaleb(3)` es `1E+3`: con `PF_PEPEUSD` (precisión −3) el bot enviaría
`size=5E%2B3`; igual `limitPrice` por debajo de 1e-6. Los property tests usan
`Decimal(10)**-p` y no lo ven. Corrección: serializar con formato positional.

### M2 — Stops de catástrofe
**Gravedad:** media · **Estado:** pendiente

Se sincronizan después de ejecutar y de `_after_trading` (PoC G2); el reemplazo
cancela antes de colocar (PoC G); el stop `cs-` sigue en el exchange tras el
cierre de emergencia (PoC L). Corrección: sincronizar justo tras ejecutar,
colocar antes de cancelar y cancelar al cerrar.

### M3 — Libro fiscal sin deduplicación ni conciliación
**Gravedad:** media · **Estado:** pendiente

Se ignoran `fill_id` y `booking_uid` (PoC D: una fila duplicada impide cerrar la
posición); `drain_ledger()` vacía la memoria antes de escribir; `/fills` solo se
consulta cada 300 s y devuelve ≤100; el cursor `ts+1` con `count=50` puede saltar
eventos con el mismo milisegundo. Corrección: dedupe al escribir y al exportar,
escritura antes de avanzar cursores, sondeo en cada ciclo con paginación y
conciliación del neto de fills con las posiciones.

### M4 — Moneda y signo sin verificar
**Gravedad:** media · **Estado:** pendiente

El funding se calcula como `new_balance − old_balance` y se etiqueta USD sin
comprobar `asset`; el signo de `fee` no se verifica; `report.py` ignora
comisiones en EUR. Corrección: exigir `asset=usd` (o convertir) y alertar; verificar
el signo de `fee` como el del funding; no ignorar EUR.

### M5 — Puerta `--check`
**Gravedad:** media · **Estado:** pendiente

Los `return` tempranos de `run_check` no limpian `live_check` (PoC M) y los
permisos nunca se revalidan al arrancar. Corrección: invalidar al empezar y
revalidar la clave en cada arranque live.

### M6 — SSH solo con clave
**Gravedad:** media · **Estado:** pendiente

sshd usa el primer valor de cada directiva y `50-cloud-init.conf` gana a
`99-copybot.conf`; el script solo hace `sshd -t`. Corrección: `00-copybot.conf` y
verificar con `sshd -T`.

### M7 — `copybot-cli --status`
**Gravedad:** media · **Estado:** pendiente

El wrapper lo deja pasar con el servicio activo pero Python toma el lock
exclusivo y falla. Corrección: `--status` de solo lectura.

### M8 — Vigilancia externa, watchdog y SIGTERM
**Gravedad:** media · **Estado:** pendiente

El bot es su propio único canal de alerta, `StartLimitBurst=5` lo abandona y no
hay manejo de SIGTERM. Corrección (decisión de diseño): healthcheck externo
opcional con la URL en `.env`, `OnFailure=` y parada ordenada por señal.

### M9 — Latencia del kill switch
**Gravedad:** media · **Estado:** pendiente

STOP solo se mira al inicio de cada ciclo y `reconcile_interval_seconds` admite
hasta 3600 s. Corrección: vigilante de STOP cada segundo.

### M10 — Posiciones manuales adoptadas
**Gravedad:** media · **Estado:** pendiente

Una posición manual en un mercado que opera el líder se trata como propia.
Corrección (decisión de diseño): fatal arrancar en live por primera vez con
cualquier posición abierta en Kraken Futures.

### M11 — Coherencia de precios HL↔Kraken y `max_position_size`
**Gravedad:** media · **Estado:** pendiente

No se compara el mid de Hyperliquid con el mark de Kraken y `max_position_size`
se lee pero no se usa. Corrección: descartar el activo si el precio diverge y
respetar el máximo del mercado.

### M12 — Topes sobre exposición real
**Gravedad:** media · **Estado:** pendiente

El ejecutor solo valida el tope por activo (600 USD) en los aumentos; no el total
ni el perfil de arranque. Corrección: guarda de exposición proyectada antes de
cada orden no reduceOnly.

### M13 — HALT sin cerrar y stops laxos
**Gravedad:** media · **Estado:** pendiente

Solo drawdown y STOP cierran; el stop al 20 % del precio equivale a ~40 % del
capital a 2x. Corrección (decisión de diseño): distancia del stop =
`max_drawdown_pct` / apalancamiento efectivo; HALT sin cierre pero con alerta
crítica que lista las posiciones abiertas.

### M14 — Lagunas de tests
**Gravedad:** media · **Estado:** pendiente

Mutaciones supervivientes: `slippage_cap_pct` → límite de la orden ejecutada;
orden rechazada como ciclo con error; mercado `suspended` en el motor; año
fiscal por cierre; tipo del día del pago en el fichero de posiciones; `redact()`
en Telegram; ejecución parcial marcada FILLED; `OSError` al enviar; loggers de
httpx; deduplicación de alertas. No hay test de integración del kill switch live.

---

## Bajas

### B1 — `.gitignore`, pre-commit y CI
**Gravedad:** baja · **Estado:** pendiente

`config.toml` no está ignorado, el pre-commit es voluntario y no hay CI.

### B2 — Redacción de `Authorization: Bearer`
**Gravedad:** baja · **Estado:** pendiente

Solo se enmascara "Bearer" y el token queda en claro (latente: Kraken y Telegram
no usan ese esquema).

### B3 — Año fiscal y día BCE en UTC
**Gravedad:** baja · **Estado:** pendiente

Una posición cerrada el 31/12 a las 23:30 UTC (ya 1 de enero en Madrid) cuenta
para el año equivocado y toma el tipo del día equivocado.

### B4 — Export fiscal no atómico
**Gravedad:** baja · **Estado:** pendiente

El export sobrescribe y puede dejar ficheros parciales si falla el BCE; un
funding puede asignarse a dos posiciones (sin marca `used`).

### B5 — `PaperAccount.orders` crece sin límite
**Gravedad:** baja · **Estado:** pendiente (documentado)

La cuenta paper guarda todas las órdenes y las reserializa en cada guardado.
Sin impacto en seguridad; se aborda si el tamaño de `state.json` molesta.

### B6 — Telegram: dedupe de críticas e `InvalidURL`
**Gravedad:** baja · **Estado:** pendiente

El dedupe por texto también silencia alertas CRÍTICAS repetidas y
`httpx.InvalidURL` no es `HTTPError`, así que escapa de `alert()`.

### B7 — Instalación en el VPS
**Gravedad:** baja · **Estado:** pendiente

`pip install --upgrade pip` sin hash y sin backups de `state.json` ni CSV fiscales.

### B8 — Fila duplicada en `trades.csv`
**Gravedad:** baja · **Estado:** pendiente

Una caída entre escribir la fila y guardar el estado la duplica al reconciliar.

### B9 — Desfase de `/openpositions` tras un fill
**Gravedad:** baja · **Estado:** pendiente (documentado)

Si `/openpositions` se retrasa respecto a un fill, el ciclo siguiente podría
repetir la orden. No es verificable sin la API real: se comprobará en la prueba
supervisada con dinero real.
