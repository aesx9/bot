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
- Tras cerrar M3 y M4: 33 mutaciones adicionales sobre el libro (paginación, cursor, idempotencia,
  conciliación, moneda y signo); los tests las detectan todas.
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

## Estado actual

- **Resueltos:** A1-A5, M1-M14 y B1, B2, B3, B4, B6, B7, B8 (cada uno con su commit y su test
  de regresión). M3 y M4, que quedaron fuera del primer lote, se cerraron después a petición
  del usuario; las acciones de GitHub Actions de B1 se fijaron por SHA.
- **Siguen abiertos:** solo B5 y B9, documentados como pendientes (B9 se verificará en la
  prueba supervisada con dinero real).
- **A verificar en esa prueba supervisada (B9):** los campos reales del `account-log` de una
  cuenta multi-colateral (`collateral` frente a `asset`, signo de `fee`, `realized_pnl`), la
  paginación real de `/fills` (`lastFillTime`) y del log (`since` inclusivo o no), y que
  `fiscal_conciliacion_<año>.csv` cuadre con la cuenta. Las comprobaciones de M3 y M4 están
  escritas para el peor caso en cada duda, y avisan en vez de suponer.
- Las 10 mutaciones que sobrevivían en el commit base (M14) mueren ahora; el kill switch live
  tiene test de integración (M2).
- Live sigue sin probarse contra Kraken real: lo cubierto aquí es la lógica del bot contra
  una API privada simulada a partir de la documentación.

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
| M3 | Media | Libro fiscal sin deduplicación ni conciliación | D | resuelto (`0329124`) |
| M4 | Media | Moneda y signo de funding y comisiones sin verificar | (lectura) | resuelto (`60f3637`) |
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
| B1 | Baja | `config.toml` no ignorado; pre-commit voluntario; sin CI | (lectura) | resuelto (`dbc9c96`, `b3dbfc8`) |
| B2 | Baja | `Authorization: Bearer x` deja el token | (lectura) | resuelto (`8094e83`) |
| B3 | Baja | Año fiscal y día BCE en UTC en vez de Madrid | (lectura) | resuelto (`2f3d6c2`) |
| B4 | Baja | Export no atómico; funding asignable a dos posiciones | (lectura) | resuelto (`03b9a29`) |
| B5 | Baja | `PaperAccount.orders` crece sin límite | (lectura) | pendiente (documentado) |
| B6 | Baja | Telegram: dedupe también de críticas; `InvalidURL` sin capturar | (lectura) | resuelto (`b769bfa`) |
| B7 | Baja | `pip --upgrade pip` sin hash; sin backups | (lectura) | resuelto (`9f67fb0`) |
| B8 | Baja | Fila duplicada en `trades.csv` tras caída | (lectura) | resuelto (`90b9140`) |
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
**Gravedad:** media · **Estado:** resuelto en `0329124`

Se ignoran `fill_id` y `booking_uid` (PoC D: una fila duplicada impide cerrar la
posición); `drain_ledger()` vacía la memoria antes de escribir; `/fills` solo se
consulta cada 300 s y devuelve ≤100; el cursor `ts+1` con `count=50` puede saltar
eventos con el mismo milisegundo. Corrección: dedupe al escribir y al exportar,
escritura antes de avanzar cursores, sondeo en cada ciclo con paginación y
conciliación del neto de fills con las posiciones.
- **Aplicado:** libro en dos fases (`collect_funding` prepara, el motor escribe los CSV y
  `commit_ledger` avanza cursor e ids vistos); `/fills` paginado con `lastFillTime` (más
  antiguo + 1 ms) y account-log paginado desde (último ms − 1), ambos con dedupe por
  `fill_id`/`booking_uid`; CSV idempotentes por id; foto de posiciones (`positions.csv`) y
  `fiscal_conciliacion_<año>.csv` con aviso si no cuadra. Si una página entera comparte
  milisegundo se avisa en vez de repetirla. Las entradas ilegibles siguen saltándose con
  alerta.
- **Regresión:** tests/integration/test_live_ledger.py (12: paginación de /fills y del account-log, mismo milisegundo, escritura antes del cursor, idempotencia de los CSV) y tests/unit/test_fiscal.py::test_duplicated_fill_rows_are_counted_once, ::test_duplicated_fee_and_funding_rows_are_counted_once, ::test_reconciliation_*

### M4 — Moneda y signo sin verificar
**Gravedad:** media · **Estado:** resuelto en `60f3637`

El funding se calcula como `new_balance − old_balance` y se etiqueta USD sin
comprobar `asset`; el signo de `fee` no se verifica; `report.py` ignora
comisiones en EUR. Corrección: exigir `asset=usd` (o convertir) y alertar; verificar
el signo de `fee` como el del funding; no ignorar EUR.
- **Aplicado:** la moneda de cada entrada es su colateral (o activo). El funding que no es
  USD no se escribe en `funding.csv` y avisa con importe, moneda y `booking_uid`; las
  comisiones se guardan con su moneda real (`DESCONOCIDA` si falta) y avisan si no son USD ni
  EUR (el export convierte EUR). Alerta para una comisión negativa o que hace subir el saldo.
  `fiscal.py` ya no da por USD una comisión sin moneda; `report.py` suma USD, muestra EUR
  aparte sin convertir y avisa del resto.
- **Regresión:** tests/integration/test_live_ledger.py::test_funding_in_another_currency_is_not_recorded_as_usd, ::test_fee_currency_is_recorded_and_never_assumed_usd, ::test_negative_or_inverted_fee_sign_alerts_and_the_value_is_kept; tests/unit/test_fiscal.py::test_fees_without_currency_or_in_other_currencies_are_not_counted_as_usd; tests/unit/test_report.py::test_live_report_does_not_ignore_eur_fees_nor_assume_usd

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
**Gravedad:** baja · **Estado:** resuelto en `dbc9c96` (acciones fijadas por SHA en `b3dbfc8`)

`config.toml` no está ignorado, el pre-commit es voluntario y no hay CI. Las acciones de
GitHub del CI (`actions/checkout`, `actions/setup-python`) se referenciaban por etiqueta
movible; ahora van fijadas por SHA de commit con la versión en un comentario.
- **Regresión:** tests/unit/test_repo_hygiene.py (4 tests) y ::test_ci_actions_are_pinned_by_commit_sha_not_by_movable_tag

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
**Gravedad:** baja · **Estado:** resuelto en `03b9a29`

El export sobrescribe y puede dejar ficheros parciales si falla el BCE; un
funding puede asignarse a dos posiciones (sin marca `used`).
- **Regresión:** tests/unit/test_fiscal.py::test_failed_rate_lookup_leaves_no_partial_files_and_keeps_the_previous_export, ::test_export_files_are_private, ::test_funding_at_a_direction_change_is_counted_once_and_by_the_open_position

### B5 — `PaperAccount.orders` crece sin límite
**Gravedad:** baja · **Estado:** pendiente (documentado)

La cuenta paper guarda todas las órdenes y las reserializa en cada guardado.
Sin impacto en seguridad; se aborda si el tamaño de `state.json` molesta.

### B6 — Telegram: dedupe de críticas e `InvalidURL`
**Gravedad:** baja · **Estado:** resuelto en `b769bfa`

El dedupe por texto también silencia alertas CRÍTICAS repetidas y
`httpx.InvalidURL` no es `HTTPError`, así que escapa de `alert()`.
- **Regresión:** tests/unit/test_alerts.py::test_critical_alerts_are_not_silenced_for_ten_minutes y ::test_any_failure_sending_is_swallowed_including_invalid_urls

### B7 — Instalación en el VPS
**Gravedad:** baja · **Estado:** resuelto en `9f67fb0`

`pip install --upgrade pip` sin hash y sin backups de `state.json` ni CSV fiscales.
- **Regresión:** tests/unit/test_deploy.py::test_setup_does_not_upgrade_pip_without_a_hash, ::test_setup_installs_the_backup_job, ::test_backup_contains_state_and_csv_but_never_secrets_logs_or_lock, ::test_backup_keeps_only_the_latest_and_leaves_no_temp_files, ::test_backup_with_nothing_to_copy_is_not_an_error

### B8 — Fila duplicada en `trades.csv`
**Gravedad:** baja · **Estado:** resuelto en `90b9140`

Una caída entre escribir la fila y guardar el estado la duplica al reconciliar.
- **Regresión:** tests/unit/test_records.py::test_trade_rows_are_idempotent_by_client_order_id; test_executor.py::test_crash_between_csv_row_and_state_save_does_not_duplicate_the_trade

### B9 — Desfase de `/openpositions` tras un fill
**Gravedad:** baja · **Estado:** pendiente (documentado)

Si `/openpositions` se retrasa respecto a un fill, el ciclo siguiente podría
repetir la orden. No es verificable sin la API real: se comprobará en la prueba
supervisada con dinero real.

---

# Segunda auditoría

> Auditoría de las correcciones anteriores sobre `7b5ba8e`. Mismo formato: cada hallazgo
> lleva su **Estado** y los commits de corrección empiezan por su ID (`git log --grep '^N1:'`).

## Método

- `make check` completo en `7b5ba8e`: ruff, mypy strict, 545 tests, pip-audit (0
  vulnerabilidades) y detect-secrets, todo en verde. SHA de las acciones de GitHub contrastados
  con `git ls-remote`.
- **Reversión:** para cada commit de corrección de la primera auditoría se restauró el código
  (no los tests) del commit padre y se ejecutaron los tests nuevos. Todos fallan salvo los de
  "no romper" (comportamiento que ya era correcto) y los de A1/M8, que no importan con el código
  anterior; esos dos se verificaron por mutación.
- **Mutación:** 34 mutaciones dirigidas sobre el código actual (A1-A5, M2-M5, M8-M14, B3, B6,
  B8). Mueren 31; sobreviven dos de A1 (las dos capas de captura se tapan entre sí) y una de B3
  (el test no distingue el día del tipo del BCE): ver T1 y T2.
- PoCs P1-P5 sobre la API privada simulada.

## Resumen

| ID | Gravedad | Hallazgo | PoC | Estado |
|---|---|---|---|---|
| N1 | Media | El tope de nocional por hora detiene el bot al copiar un cierre | P4 | resuelto (`c8b33be`) |
| N2 | Media | Con el bot detenido el libro fiscal no recoge nada (cierres de emergencia, stops) | P3 | resuelto (`d40b652`) |
| N3 | Media | Cualquier rechazo del stop nuevo retira el antiguo (regresión de M2) | P2 | resuelto (`ca9285c`) |
| N4 | Media | El funding que no es USD solo queda en una alerta; el export no lo ve (M4) | P1 | resuelto (`b430609`) |
| N5 | Baja/Media | Tras saltar un stop de catástrofe el bot reabre en el ciclo siguiente | P5 | resuelto (`65eb429`) |
| T1 | Baja | Hueco de test de A1: las dos capas de captura se tapan entre sí | mutación | resuelto (`6d30626`) |
| T2 | Baja | Hueco de test de B3: ningún test distingue el día del tipo del BCE | mutación | resuelto (`98e9a0c`) |
| N6 | Baja | `fills_seen` conserva los ids más antiguos al recortar | (lectura) | resuelto (`b1df33b`) |
| N7 | Baja | `liquidation_fee` no se deduce como comisión | (lectura) | pendiente |
| N8 | Baja | La guarda de exposición no cuenta los activos con precio incoherente | (lectura) | pendiente |
| N9 | Baja | El README dice que `positions.csv` se escribe "al cambiar" | (lectura) | resuelto (`d40b652`) |
| N10 | Baja | Los cierres de emergencia cuentan en el tope de nocional | (lectura) | resuelto (`c8b33be`) |

## Hallazgos

### N1 — El tope de nocional detiene el bot al copiar un cierre
**Gravedad:** media · **Estado:** resuelto en `c8b33be`

`executor.py`: `check_notional` se aplica también a las órdenes reduceOnly. Con 1.500 USD
abiertos en la última hora, el cierre del líder supera 2.000 USD/h, salta el circuit breaker y
el bot se detiene SIN cerrar: posición abierta con el líder plano y solo el stop de catástrofe.
Corrección: las reduceOnly no cuentan ni se frenan en el tope de nocional.

- **Regresión:** tests/unit/test_executor.py::test_reduce_only_orders_never_trip_nor_count_in_the_notional_limit
### N2 — Libro fiscal parado mientras el bot está detenido
**Gravedad:** media · **Estado:** resuelto en `d40b652`

`engine.py`: el libro (funding, fills, comisiones, cursor y foto de posiciones) solo se
actualiza al final de un ciclo de trading completo. Los fills de un cierre por STOP o drawdown,
de un stop de catástrofe o de una liquidación no llegan a `kraken_fills.csv` mientras dure la
parada (nunca, si el bot se abandona). El export cree que la posición sigue abierta y la
conciliación dice "cuadra" porque tampoco hay foto posterior. Corrección: actualizar el libro
también en ciclos detenidos y tras cada cierre de emergencia, y `--sync-ledger` de solo lectura.

- **Regresión:** tests/integration/test_halted_ledger.py (cierre de emergencia en el libro y en el export, ciclos detenidos, --sync-ledger solo lectura y su negativa sin línea base)
### N3 — Un rechazo cualquiera del stop nuevo retira el antiguo
**Gravedad:** media · **Estado:** resuelto en `ca9285c`

`live.py`, `_sync_catastrophe_stops`: el respaldo "cancelar y recolocar" (pensado para un
exchange que admite un solo stop por símbolo) se activa con CUALQUIER estado distinto de
`placed`. Con `marketSuspended` o un precio inválido se cancela el stop válido, el reintento
también falla y la posición queda sin protección. Corrección: respaldo solo con el código
concreto; cualquier otro rechazo mantiene el antiguo y alerta crítica.

- **Regresión:** tests/integration/test_live_exchange.py::test_other_rejections_of_the_new_stop_keep_the_old_one (3 estados)
### N4 — Funding en otra moneda: solo una alerta
**Gravedad:** media · **Estado:** resuelto en `b430609`

`live.py`, `_funding_event`: un funding que no es USD no se escribe en ningún CSV, su
`booking_uid` se marca como visto (no se relee ni se vuelve a avisar) y solo queda la alerta
en Telegram y en `copybot.log` (rota a 60 días, sin copia de seguridad). `export_fiscal` no lo
detecta. Además `_currency` prefiere `collateral` a `asset` en silencio. Corrección: CSV propio
con moneda; el export convierte EUR y falla si es otra; aviso si `collateral` y `asset`
discrepan.

- **Regresión:** tests/integration/test_live_ledger.py::test_funding_in_another_currency_is_kept_with_its_currency, ::test_collateral_and_asset_disagreeing_is_unknown_and_alerts; tests/unit/test_fiscal.py::test_eur_funding_is_converted_with_the_ecb_rate_of_its_day, ::test_funding_in_an_unconvertible_currency_blocks_the_export
### N5 — Reapertura tras un stop de catástrofe
**Gravedad:** baja/media · **Estado:** resuelto en `65eb429`

Tras saltar el stop, el ciclo siguiente reabre la posición del líder sin aviso específico. Con
M13 el stop está al 7,5 % (2x) o 5 % (3x), alcanzable con volatilidad normal: cada repetición
pierde esa distancia hasta que corta el drawdown. Corrección: un fill de origen
`stop_catastrofe` o `liquidación` detiene el bot con alerta crítica.

- **Regresión:** tests/integration/test_halted_ledger.py::test_a_catastrophe_stop_or_liquidation_halts_instead_of_reopening (stop y liquidación) y ::test_bot_and_manual_fills_do_not_halt
### T1 — Las dos capas de captura de A1
**Gravedad:** baja · **Estado:** resuelto en `6d30626`

Quitar el `try` de `Engine.cycle()` o reducir el `except` de `_cycle` a `CYCLE_ERRORS`
sobrevive a la suite. Con lo segundo, una excepción imprevista acaba en `_last_resort`, que no
guarda el estado ni avisa.

- **Regresión:** tests/integration/test_resilience.py::test_unexpected_exception_in_trading_is_alerted_and_persisted y ::test_exception_outside_the_trading_block_does_not_escape_cycle (cada uno mata una de las dos mutaciones)
### T2 — Día del tipo del BCE (B3)
**Gravedad:** baja · **Estado:** resuelto en `98e9a0c`

El test de B3 usa el 1 de enero, sin tipo publicado: UTC y Madrid caen ambos en el 31/12 y la
mutación `local_date` → fecha UTC sobrevive.

- **Regresión:** tests/unit/test_fiscal.py::test_each_flow_takes_the_ecb_rate_of_its_madrid_day_when_utc_says_otherwise (mata la mutación local_date -> UTC)
### N6 — Recorte de `fills_seen`
**Gravedad:** baja · **Estado:** resuelto en `b1df33b`

Las páginas de `/fills` van de las más recientes a las más antiguas y se añaden en ese orden:
al recortar a 500 se conservan los ids más antiguos y en cada sondeo se vuelven a paginar los
recientes (el CSV no duplica, pero consume peticiones).

- **Regresión:** tests/integration/test_live_ledger.py::test_fills_seen_keeps_the_most_recent_ids_when_trimmed
### N7 — `liquidation_fee`
**Gravedad:** baja · **Estado:** pendiente

Las entradas de liquidación del log traen la comisión en `liquidation_fee`, que se ignoraba.

### N8 — Guarda de exposición y precios incoherentes
**Gravedad:** baja · **Estado:** pendiente

`engine.py`: las posiciones de los activos con precio incoherente (M11) se quitan de `current`
antes de pasarlo al ejecutor, y la guarda de exposición total no las cuenta.

### N9 — README de `positions.csv`
**Gravedad:** baja · **Estado:** resuelto en `d40b652`

La foto se escribe con el registro de capital (cada 15 min) y, sin cambios, como mucho cada hora.

- **Regresión:** README (tabla de ficheros); la foto se toma en cada actualización del libro: tests/integration/test_halted_ledger.py::test_emergency_close_fills_reach_the_ledger_and_the_export
### N10 — Cierres de emergencia y tope de nocional
**Gravedad:** baja · **Estado:** resuelto en `c8b33be`

Las órdenes de emergencia se registraban con su nocional: tras `--reset-halt` el breaker podía
saltar en la primera orden normal.

- **Regresión:** tests/unit/test_executor.py::test_emergency_closes_do_not_count_in_the_notional_limit
## Checklist de la prueba supervisada live (capital mínimo)

- [ ] Log de cuenta en una cuenta con colateral EUR: qué traen `asset` y `collateral`, moneda del
      funding y de las comisiones, signo de `fee`, presencia de `liquidation_fee`.
- [ ] Paginación: `since` inclusivo o no; con `sort=asc` y `count`, que se devuelven las entradas
      más ANTIGUAS (no las más recientes ordenadas); semántica de `lastFillTime` en `/fills`.
- [ ] B9: `/openpositions` refleja el fill nada más recibir la respuesta; dos ciclos seguidos
      (debounce) no duplican la orden.
- [ ] Stops de catástrofe: código de rechazo real con dos stops en el mismo símbolo; `openorders`
      devuelve el `cliOrdId` `cs-`; el stop aparece en la web de Kraken.
- [ ] Kill switch real: cierre, cancelación de los stops `cs-` y fills del cierre en el libro.
- [ ] `--sync-ledger` con el bot detenido trae los fills y el funding pendientes.
- [ ] Al terminar: export fiscal con `fiscal_conciliacion_<año>.csv` cuadrando; comisiones y funding
      contrastados con el historial de Kraken; signo del funding verificado.
- [ ] Reinicio por systemd sin pedir confirmación; salida 3 al detenerse; aviso de `OnFailure`;
      healthcheck externo.
