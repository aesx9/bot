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
| A1 | Alta | Una excepción no prevista mata el bucle y el bot sigue "vivo" sin operar | B, C | resuelto (`fbbc0f8`) |
| A2 | Alta | Posiciones huérfanas si el ciclo se aborta a mitad de ejecución | J, J2 | resuelto (`9770f80`) |
| A3 | Alta | Estado compartido entre paper y live | E | resuelto (`34176e2`) |
| A4 | Alta | Los fills del primer ciclo live no entran en el libro fiscal | A | resuelto (`d59f8ef`) |
| A5 | Alta | Cierre de emergencia frágil | I, K | resuelto (`e9a7925`) |
| M1 | Media | Tamaños y precios en notación científica en `sendorder` | formato | resuelto (`b5fc7d0`) |
| M2 | Media | Stops de catástrofe: tarde, reemplazo no atómico, sobreviven al cierre | G, G2, L | resuelto (`29da910`) |
| M3 | Media | Libro fiscal sin deduplicación ni conciliación | D | pendiente |
| M4 | Media | Moneda y signo de funding y comisiones sin verificar | (lectura) | pendiente |
| M5 | Media | Puerta `--check` mal invalidada y sin revalidar permisos | M | resuelto (`8c87bfd`) |
| M6 | Media | "SSH solo con clave" puede no aplicarse | (OpenSSH) | resuelto (`70deb3f`) |
| M7 | Media | `copybot-cli --status` falla con el servicio activo | (tests) | resuelto (`454058d`) |
| M8 | Media | Sin vigilancia externa, watchdog ni manejo de SIGTERM | (lectura) | resuelto (`2c2087d`) |
| M9 | Media | El kill switch puede tardar hasta 1 h | (lectura) | resuelto (`a248c21`) |
| M10 | Media | Posiciones manuales en mercados del líder se adoptan | E | resuelto (`e8e0274`) |
| M11 | Media | Sin coherencia de precios HL↔Kraken; `max_position_size` sin usar | (lectura) | resuelto (`f138907`) |
| M12 | Media | Los topes se aplican al objetivo, no a la exposición real | (lectura) | resuelto (`66e8f39`) |
| M13 | Media | Tras un HALT las posiciones quedan con stops laxos | (lectura) | resuelto (`6efcace`) |
| M14 | Media | Lagunas de tests (mutaciones supervivientes) | mutación | resuelto (`0c8d487`) |
| B1 | Baja | `config.toml` no ignorado; pre-commit voluntario; sin CI | (lectura) | resuelto (`dbc9c96`) |
| B2 | Baja | `Authorization: Bearer x` deja el token | (lectura) | resuelto (`8094e83`) |
| B3 | Baja | Año fiscal y día BCE en UTC en vez de Madrid | (lectura) | resuelto (`2f3d6c2`) |
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
**Gravedad:** alta · **Estado:** resuelto en `fbbc0f8`

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
- **Regresión:** tests/integration/test_resilience.py; test_hyperliquid_ws.py::test_unexpected_callback_failure_reconnects_instead_of_killing_the_stream; test_live_exchange.py::test_malformed_account_log_entries_are_skipped_with_an_alert y ::test_malformed_payloads_raise_exchange_error

### A2 — Posiciones huérfanas
**Gravedad:** alta · **Estado:** resuelto en `9770f80`

- **Dónde:** `engine.py` (actualización de `managed_symbols`),
  `executor.py` (`execute`).
- **Escenario (PoC J y J2):** `managed_symbols` solo se actualiza al terminar
  todas las acciones. Si el ciclo se aborta tras abrir una posición (breaker,
  límite duro, `OrderUncertain`, `OSError`, caída), la posición existe sin
  constar como gestionada ni tener stop: el kill switch "cierra" sin cerrar nada
  y tras `--reset-halt` con el líder plano tampoco se cierra.
- **Corrección:** registrar el símbolo y guardar antes de enviar; sincronizar
  stops también cuando el ciclo se aborta.
- **Regresión:** tests/integration/test_resilience.py::test_position_opened_before_a_breaker_trip_stays_managed y ::test_orphan_is_closed_once_the_leader_is_flat_after_reset; test_live_exchange.py::test_stop_is_placed_even_when_the_cycle_aborts_mid_execution

### A3 — Estado compartido entre paper y live
**Gravedad:** alta · **Estado:** resuelto en `34176e2`

- **Dónde:** `state.py` (`BotState` sin modo), `main.py`, `checks.py`.
- **Escenario (PoC E):** el README reutiliza el mismo `state.json` al pasar de
  paper a live. Se heredan `managed_symbols`, `preexisting*`, el pico de capital
  simulado y el registro del breaker: con una posición manual de 0,01 BTC y el
  líder sin BTC, el primer ciclo live la cierra.
- **Corrección (decisión de diseño):** directorio de datos separado por modo,
  modo guardado en el estado y negativa a arrancar si no coincide.
- **Regresión:** tests/integration/test_main.py::test_live_does_not_inherit_paper_state, ::test_state_of_another_mode_refuses_to_start, ::test_each_mode_has_its_own_directory, ::test_legacy_single_directory_state_is_not_silently_ignored; test_state_risk.py::test_state_is_bound_to_the_mode_that_created_it; test_config.py::test_run_dir_is_separate_per_mode

### A4 — Los fills del primer ciclo live no entran en el libro fiscal
**Gravedad:** alta · **Estado:** resuelto en `d59f8ef`

- **Dónde:** `live.py` (`_poll_fills`, `collect_funding`), `engine.py` (orden de
  `execute` y `_after_trading`).
- **Escenario (PoC A):** la primera llamada a `collect_funding` ocurre después de
  que el primer ciclo opere; marca como vistos y descarta los fills de esas
  órdenes y sus comisiones. El export no declara la posición cerrada y avisa de
  un corto abierto inexistente.
- **Corrección:** fijar la línea base del libro antes del primer envío.
- **Regresión:** tests/integration/test_live_exchange.py::test_first_cycle_fills_reach_the_fiscal_ledger y ::test_prepare_ledger_is_idempotent_and_runs_once

### A5 — Cierre de emergencia frágil
**Gravedad:** alta · **Estado:** resuelto en `e9a7925`

- **Dónde:** `engine.py` (`_kill_switch`, `_close_all_managed`), `planner.py`,
  `executor.py`.
- **Escenario (PoC I y K):** un solo mercado sin `MarketSpec` hace fallar el plan
  de todos; `kill_switch_closed=True` se fija aunque quede algo abierto y solo
  hay ~10 s de intentos; con el disco lleno no sale ninguna orden de cierre
  porque se persiste antes de enviar.
- **Corrección:** cerrar símbolo a símbolo con errores aislados; reintentar
  mientras queden posiciones; en emergencia, enviar aunque falle el guardado.
- **Regresión:** tests/integration/test_emergency.py (6 tests)

---

## Medias

### M1 — Notación científica en `sendorder`
**Gravedad:** media · **Estado:** resuelto en `b5fc7d0`

`Decimal(1).scaleb(3)` es `1E+3`: con `PF_PEPEUSD` (precisión −3) el bot enviaría
`size=5E%2B3`; igual `limitPrice` por debajo de 1e-6. Los property tests usan
`Decimal(10)**-p` y no lo ven. Corrección: serializar con formato positional.
- **Regresión:** tests/integration/test_live_exchange.py::test_negative_precision_market_sends_positional_decimals y ::test_stop_prices_and_sizes_are_positional; tests/unit/test_records.py::test_decimals_are_written_without_scientific_notation

### M2 — Stops de catástrofe
**Gravedad:** media · **Estado:** resuelto en `29da910`

Se sincronizan después de ejecutar y de `_after_trading` (PoC G2); el reemplazo
cancela antes de colocar (PoC G); el stop `cs-` sigue en el exchange tras el
cierre de emergencia (PoC L). Corrección: sincronizar justo tras ejecutar,
colocar antes de cancelar y cancelar al cerrar.
- **Regresión:** tests/integration/test_live_exchange.py::test_failed_replacement_keeps_the_old_stop, ::test_new_stop_is_placed_before_the_old_one_is_cancelled, ::test_replacement_falls_back_when_the_exchange_allows_one_stop_per_symbol, ::test_error_after_trading_does_not_skip_the_stop_sync, ::test_kill_switch_cancels_the_catastrophe_stops

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
**Gravedad:** media · **Estado:** resuelto en `8c87bfd`

Los `return` tempranos de `run_check` no limpian `live_check` (PoC M) y los
permisos nunca se revalidan al arrancar. Corrección: invalidar al empezar y
revalidar la clave en cada arranque live.
- **Regresión:** tests/integration/test_check.py::test_failed_recheck_invalidates_the_previous_pass y ::test_key_is_revalidated_before_every_live_start; test_main.py::test_live_start_is_refused_if_the_key_gained_transfer_permission

### M6 — SSH solo con clave
**Gravedad:** media · **Estado:** resuelto en `70deb3f`

sshd usa el primer valor de cada directiva y `50-cloud-init.conf` gana a
`99-copybot.conf`; el script solo hace `sshd -t`. Corrección: `00-copybot.conf` y
verificar con `sshd -T`.
- **Regresión:** tests/unit/test_deploy.py::test_dropin_sorts_before_cloud_init_so_it_wins, ::test_verification_catches_a_dropin_that_loses_to_cloud_init, ::test_setup_verifies_effective_config_before_reloading_sshd

### M7 — `copybot-cli --status`
**Gravedad:** media · **Estado:** resuelto en `454058d`

El wrapper lo deja pasar con el servicio activo pero Python toma el lock
exclusivo y falla. Corrección: `--status` de solo lectura.
- **Regresión:** tests/integration/test_main.py::test_status_works_while_the_service_runs_and_says_so, ::test_status_creates_no_files, ::test_second_instance_is_refused

### M8 — Vigilancia externa, watchdog y SIGTERM
**Gravedad:** media · **Estado:** resuelto en `2c2087d`

El bot es su propio único canal de alerta, `StartLimitBurst=5` lo abandona y no
hay manejo de SIGTERM. Corrección (decisión de diseño): healthcheck externo
opcional con la URL en `.env`, `OnFailure=` y parada ordenada por señal.
- **Regresión:** tests/unit/test_healthcheck.py; test_resilience.py::test_healthy_bot_pings_ok, ::test_failing_halted_or_hung_bot_pings_fail, ::test_heartbeat_task_reports_to_the_healthcheck, ::test_graceful_stop_waits_for_the_cycle_in_progress, ::test_sigterm_and_sigint_request_a_graceful_stop; test_deploy.py::test_service_notifies_from_outside_the_bot_when_it_fails, ::test_service_stops_gracefully_on_sigterm

### M9 — Latencia del kill switch
**Gravedad:** media · **Estado:** resuelto en `a248c21`

STOP solo se mira al inicio de cada ciclo y `reconcile_interval_seconds` admite
hasta 3600 s. Corrección: vigilante de STOP cada segundo.
- **Regresión:** tests/integration/test_emergency.py::test_stop_file_is_honoured_within_seconds_not_at_the_next_cycle

### M10 — Posiciones manuales adoptadas
**Gravedad:** media · **Estado:** resuelto en `e8e0274`

Una posición manual en un mercado que opera el líder se trata como propia.
Corrección (decisión de diseño): fatal arrancar en live por primera vez con
cualquier posición abierta en Kraken Futures.
- **Regresión:** tests/integration/test_main.py::test_first_live_start_is_refused_with_any_open_position, ::test_restarts_with_the_bots_own_positions_are_not_blocked; test_check.py::test_open_positions_are_fatal_before_the_first_live_start, ::test_open_positions_after_the_bot_started_are_only_a_warning, ::test_no_open_positions_is_clean

### M11 — Coherencia de precios HL↔Kraken y `max_position_size`
**Gravedad:** media · **Estado:** resuelto en `f138907`

No se compara el mid de Hyperliquid con el mark de Kraken y `max_position_size`
se lee pero no se usa. Corrección: descartar el activo si el precio diverge y
respetar el máximo del mercado.
- **Regresión:** tests/integration/test_engine.py::test_asset_with_incoherent_price_is_not_traded_and_warns_once, ::test_incoherent_price_leaves_an_existing_position_untouched, ::test_size_factor_makes_a_scaled_asset_coherent; test_planner.py::test_target_is_capped_at_the_market_max_position_size

### M12 — Topes sobre exposición real
**Gravedad:** media · **Estado:** resuelto en `66e8f39`

El ejecutor solo valida el tope por activo (600 USD) en los aumentos; no el total
ni el perfil de arranque. Corrección: guarda de exposición proyectada antes de
cada orden no reduceOnly.
- **Regresión:** tests/unit/test_executor.py::test_increase_above_the_per_asset_cap_is_skipped_not_sent, ::test_open_is_skipped_if_real_positions_already_use_the_total, ::test_reductions_and_closes_are_never_blocked_by_the_guard; test_emergency.py::test_open_is_skipped_while_a_previous_close_has_not_filled

### M13 — HALT sin cerrar y stops laxos
**Gravedad:** media · **Estado:** resuelto en `6efcace`

Solo drawdown y STOP cierran; el stop al 20 % del precio equivale a ~40 % del
capital a 2x. Corrección (decisión de diseño): distancia del stop =
`max_drawdown_pct` / apalancamiento efectivo; HALT sin cierre pero con alerta
crítica que lista las posiciones abiertas.
- **Regresión:** tests/unit/test_state_risk.py::test_catastrophe_stop_distance_follows_drawdown_over_leverage y ::test_stop_loss_at_the_stop_equals_the_drawdown_limit; test_live_exchange.py::test_catastrophe_stop_distance_comes_from_drawdown_and_leverage; test_resilience.py::test_halt_without_auto_close_alerts_what_stays_open y ::test_halt_with_everything_closed_has_no_open_positions_note; test_main.py::test_live_summary_shows_the_computed_catastrophe_stop

### M14 — Lagunas de tests
**Gravedad:** media · **Estado:** resuelto en `0c8d487`

Mutaciones supervivientes: `slippage_cap_pct` → límite de la orden ejecutada;
orden rechazada como ciclo con error; mercado `suspended` en el motor; año
fiscal por cierre; tipo del día del pago en el fichero de posiciones; `redact()`
en Telegram; ejecución parcial marcada FILLED; `OSError` al enviar; loggers de
httpx; deduplicación de alertas. No hay test de integración del kill switch live.
- **Regresión:** tests/unit/test_alerts.py; test_executor.py::test_limit_price_uses_the_configured_cap_and_never_more_than_the_hard_one, ::test_oserror_after_the_order_reached_the_exchange_is_reconciled; test_engine.py::test_rejected_orders_count_as_a_cycle_error_and_five_halt, ::test_suspended_market_blocks_the_cycle_without_orders; test_live_exchange.py::test_partial_ioc_execution_is_reported_as_partial; test_fiscal.py::test_a_position_belongs_to_the_year_it_was_closed_not_opened, ::test_each_funding_in_a_position_uses_the_rate_of_its_payment_day

---

## Bajas

### B1 — `.gitignore`, pre-commit y CI
**Gravedad:** baja · **Estado:** resuelto en `dbc9c96`

`config.toml` no está ignorado, el pre-commit es voluntario y no hay CI.
- **Regresión:** tests/unit/test_repo_hygiene.py (4 tests)

### B2 — Redacción de `Authorization: Bearer`
**Gravedad:** baja · **Estado:** resuelto en `8094e83`

Solo se enmascara "Bearer" y el token queda en claro (latente: Kraken y Telegram
no usan ese esquema).
- **Regresión:** tests/unit/test_credentials_logging.py::test_authorization_schemes_hide_the_whole_token

### B3 — Año fiscal y día BCE en UTC
**Gravedad:** baja · **Estado:** resuelto en `2f3d6c2`

Una posición cerrada el 31/12 a las 23:30 UTC (ya 1 de enero en Madrid) cuenta
para el año equivocado y toma el tipo del día equivocado.
- **Regresión:** tests/unit/test_fiscal.py::test_madrid_time_matches_the_tz_database_for_every_hour_of_several_years y ::test_position_closed_at_year_end_utc_belongs_to_the_next_year_in_spain

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
