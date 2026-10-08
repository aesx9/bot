# Proyecto: copy bot Hyperliquid -> Kraken Futures

> Especificación original del proyecto (texto del encargo), seguida del
> registro de decisiones tomadas durante las fases. Si algo de aquí choca con
> el código, manda este documento: hay que corregir el código o actualizar
> este fichero con una decisión explícita.

## Rol y prioridades
Eres un ingeniero de software senior especializado en sistemas de trading
automatizado. Vas a construir desde cero un copy bot en Python.
Orden de prioridades, sin excepción: 1) seguridad del capital y de las
credenciales, 2) corrección, 3) trazabilidad, 4) rentabilidad.

## Contexto del usuario
- Residente fiscal en España. Sin experiencia previa en apalancamiento.
- Capital inicial simbólico (unos 500 EUR). Primero solo modo paper.
- Se ejecutará en un VPS Ubuntu 24.04, no en su PC.
- Explícame las decisiones técnicas en español y de forma breve.

## Forma de trabajar (obligatorio)
1. Antes de escribir código, presenta un plan con la estructura de
   módulos y espera mi aprobación.
2. Trabaja por fases (ver abajo). Al final de cada fase: tests en verde,
   resumen de lo hecho y STOP hasta que yo confirme.
3. No inventes endpoints, campos ni límites de las APIs. Verifica contra
   la documentación oficial de Hyperliquid (endpoint /info y WebSocket)
   y de Kraken Futures. Si algo no está claro, pregúntame.
4. Si una decisión afecta al riesgo (tamaños, topes, tipos de orden),
   pregúntame antes de asumir.
5. Commits pequeños y descriptivos.

## Prohibiciones absolutas
- Nunca ejecutes el bot en modo live ni envíes órdenes reales.
- Nunca crees, pidas ni manejes claves reales. Solo .env.example.
- Ninguna función de retiro o transferencia de fondos en el código.
- Nunca escribas secretos en logs, CSV, excepciones ni commits.

## Funcionalidad
El bot replica a escala las posiciones de una wallet de Hyperliquid
en perpetuos PF_*USD de Kraken Futures. No copia órdenes sueltas: en
cada ciclo compara posiciones objetivo vs actuales y envía la diferencia.

- Disparador: WebSocket userFills del líder con debounce configurable,
  más ciclo de reconciliación de respaldo por REST. Reconexión con
  backoff exponencial y ciclo forzado al reconectar.
- Lectura del líder: clearinghouseState (capital, posiciones con signo)
  y allMids.
- Filtros: allow/deny, posiciones preexistentes del líder ignoradas
  hasta que las cierre, mapeo de símbolos con overrides y size_factor.
  Activo sin mercado en Kraken: aviso único y se ignora.
- Tamaño: modo equity (proporción de capitales x multiplier) o fixed.
  Topes por activo en USD y en % del capital, apalancamiento total
  máximo con reducción proporcional.
- Planificador: abrir, aumentar, reducir, cerrar, cambio de dirección
  (cierre reduceOnly + apertura separada). Umbral mínimo en USD y
  umbral de reajuste en %. Los cierres totales nunca se filtran.
- Ejecución: redondeo a la precisión del mercado, descarte bajo el
  mínimo, órdenes a mercado, reduceOnly en toda reducción, lectura
  del precio real de ejecución.
- Solo gestiona activos del líder o registrados en su estado; nunca
  toca posiciones manuales en otros activos.

## Modos
- paper (por defecto): cuenta simulada con precios reales de Kraken,
  sin claves. Debe simular de forma realista: comisión taker de Kraken
  Futures, slippage estimado a partir del libro de órdenes y funding
  aplicado según el intervalo real del mercado. Parametrizable.
- live: requiere a la vez mode = "live" en config, flag --live por línea
  de comandos y que el comando --check haya pasado. Al arrancar en live,
  muestra resumen de topes y exige confirmación escrita.
- --check: verifica conectividad, permisos de la clave (debe tener lectura
  y trading, y ABORTAR si detecta permiso de retiro), mercados y config,
  sin enviar órdenes.
- --once, --reset-halt, --status.

## Seguridad y robustez (requisitos, no sugerencias)
Credenciales
- Claves solo en .env (permisos 600), cargado con python-dotenv.
- .gitignore incluye .env, state, logs y CSV desde el primer commit.
- Hook pre-commit con detección de secretos (gitleaks o detect-secrets).
- Clase de configuración que oculte secretos en repr y logs.

Límites duros
- Validación de config con pydantic: tipos, rangos y coherencia.
- Topes absolutos en código que la config no puede superar
  (por ejemplo apalancamiento máximo 5x, nocional máximo por activo
  y total). Si la config los supera, el bot no arranca.
- Circuit breaker: máximo de órdenes por minuto y de nocional enviado
  por hora; si se superan, modo detenido.

Protecciones de riesgo
- Stop por drawdown desde el máximo de capital, persistente entre
  reinicios. Opción de cerrar todo lo gestionado.
- Stop de catástrofe opcional en el exchange: orden stop reduceOnly
  lejana por posición, para proteger si el VPS o el bot caen.
- Kill switch: fichero STOP en el directorio.
- Parada tras N ciclos consecutivos con error.

Integridad de datos
- Decimal para todo importe y tamaño, nunca float.
- Sanity checks del líder: datos obsoletos, capital cero o negativo,
  saltos de posición absurdos entre ciclos. Ante duda, no operar.
- Idempotencia: clientOrderId único por orden; tras error o timeout,
  reconciliar con el exchange antes de reintentar. Nunca duplicar.
- state.json con escritura atómica (fichero temporal + rename).
- Lockfile para impedir dos instancias simultáneas.
- Timestamps en UTC. Respeto de rate limits con backoff.

Dependencias
- Versiones fijadas (requirements.lock con hashes) y pip-audit en el
  flujo de tests.

## Registro, informes y fiscalidad
- Logs rotativos sin secretos. Heartbeat periódico.
- Alertas opcionales por Telegram (arranque, parada, drawdown, errores).
- trades.csv por cada ejecución: timestamp UTC, mercado, lado, tamaño,
  precio líder, precio propio, slippage en pb, comisión, retraso,
  modo (paper/live).
- funding.csv separado: importe por evento, distinguiendo pagado y
  cobrado (tienen tratamiento fiscal distinto en España).
- equity.csv periódico: capital propio y del líder.
- report.py: rentabilidad, drawdown máximo, slippage medio, comisiones,
  funding neto y retraso, global y por activo.
- export_fiscal.py: resultado por posición cerrada en USD, con columnas
  de funding pagado y cobrado separadas y columna para el tipo de cambio
  EUR/USD del día de liquidación. Excluir siempre las operaciones paper.
- rank_leaders.py: ranking de wallets candidatas por Sharpe con
  30 días o más, descartando scalpers y activos sin mercado en Kraken.

## Tests
- Unitarios del planificador y del sizing (incluye property-based
  testing con hypothesis).
- Integración con APIs simuladas: desconexiones, timeouts, respuestas
  malformadas, órdenes rechazadas, reconexión del WebSocket.
- Tests específicos de: reduceOnly en toda reducción, topes absolutos,
  drawdown persistente, no duplicación de órdenes, kill switch.

## Despliegue
- Script o guía paso a paso para el VPS: usuario sin privilegios
  dedicado, SSH solo con clave, ufw, fail2ban, actualizaciones
  automáticas de seguridad.
- Servicio systemd con reinicio automático y endurecimiento
  (NoNewPrivileges, ProtectSystem=strict, PrivateTmp,
  ReadWritePaths solo al directorio de datos).
- logrotate configurado.

## Fases
1. Plan y estructura del repositorio. STOP.
2. Config, validación, secretos, logging, planificador y sizing
   con tests. STOP.
3. Fuente Hyperliquid (REST + WebSocket) con tests. STOP.
4. Ejecución paper realista, estado, protecciones y tests. STOP.
5. Ejecución live (sin probarla con dinero real) y --check. STOP.
6. Informes, export fiscal y rank_leaders. STOP.
7. Despliegue en VPS y README completo en español. STOP.

## Criterios de aceptación
- Todos los tests en verde y pip-audit sin vulnerabilidades conocidas.
- El bot arranca en paper sin .env.
- Imposible pasar a live sin config + flag + check + confirmación.
- README con instalación, configuración, puesta en marcha por fases
  (paper, live con capital mínimo), y procedimiento de emergencia.

---

# Registro de decisiones

## Fase 1
- Topes absolutos en código (`src/copybot/limits.py`), más estrictos que el
  ejemplo de la spec: apalancamiento 3x, 600 USD por activo, 1.500 USD en
  total, slippage máximo del límite IOC 0,5 %, 10 órdenes/min,
  2.000 USD/h de nocional enviado, 5 ciclos seguidos con error y
  drawdown del 15 %.
- Órdenes: límite IOC con tope de slippage del 0,5 % en lugar de mercado
  puro. Las ejecuciones parciales se reconcilian en el ciclo siguiente.
- Permiso de retiro: `--check` aborta si no puede verificar que la clave no
  lo tiene. Si el endpoint no permite verificarlo, se exige restricción de
  IP activa en la clave más una confirmación escrita.
- Stop de catástrofe: activado por defecto en live, al 20 % del precio de
  entrada, comprobando que queda antes del precio de liquidación.
- Drawdown del 15 %: se cierra todo lo gestionado y el bot se detiene.
- `rank_leaders`: usa el leaderboard no oficial de Hyperliquid, marcado
  como tal, con opción de pasar una lista manual de wallets.
- Colateral en Kraken: EUR.

## Fase 2 (respuestas a las decisiones pendientes)
1. `fixed` = ratio fijo sobre el tamaño del líder.
2. `min_order_usd = 10` y `rebalance_threshold_pct = 5`. Además se respeta el
   tamaño mínimo de cada mercado de Kraken según `instruments`.
3. Sanity del líder:
   - El control de "salto de 20 veces" se aplica solo a posiciones ya
     abiertas, nunca a aperturas desde 0.
   - Si salta cualquier control (incluido el del capital del líder, que
     puede variar por depósitos o retiros), se salta el ciclo y se alerta.
   - Si persiste N ciclos seguidos, el bot se detiene sin operar y alerta.
   - N propuesto = 3 (`sanity.halt_after_consecutive_failures`, tope
     absoluto 5). La referencia es el último dato aceptado, así que un salto
     que persiste (p. ej. un depósito) acaba en parada y exige `--reset-halt`.
4. Los valores del circuit breaker también son topes absolutos en código.
5. Capital propio:
   - Live: el capital es el valor del portfolio que da Kraken (ya incluye
     haircut y conversión); el bot no lo calcula.
   - Paper: EUR/USD de Kraken en cada ciclo, simulando el haircut del
     colateral EUR si la documentación lo especifica.

## Fase 3 (verificación de APIs, 2026-10-08)
Lo que sigue se ha contrastado con la documentación oficial y con la API
pública real:
- **Kraken `instruments`:** los PF_ no tienen campo de tamaño mínimo. El
  mínimo y el paso de tamaño salen de `contractValueTradePrecision`
  (paso = 10^-precisión, que puede ser negativa: PF_PEPEUSD = -3, es decir,
  múltiplos de 1.000).
- **Kraken demo:** `demo-futures.kraken.com` aparece en la documentación,
  pero desde el entorno de desarrollo todas sus rutas responden 301 a una
  página comercial de kraken.com.
- **Hyperliquid REST (`/info`):** `clearinghouseState` devuelve
  `assetPositions[].position.{coin, szi}`, `marginSummary.accountValue` y
  `time` (ms), con números como cadenas (`liquidationPx` puede ser `null`).
  `allMids` incluye mercados spot (`@1`) y otros (`#14720`), que se
  descartan. Pesos por IP: 1200/min; `clearinghouseState` y `allMids` pesan
  2 y el resto 20. El cliente usa como mucho la mitad.
- **Hyperliquid WebSocket:** la documentación dice que los mensajes en
  streaming de `userFills` llevan `isSnapshot: false`; en la API real ese
  campo no viene. Ausente se trata como `false`. El servidor cierra si no
  hay mensajes en 60 s: ping `{"method": "ping"}` -> `{"channel": "pong"}`.
- **Hyperliquid, modos de cuenta:** con "unified account" o "portfolio
  margin", la documentación indica que el capital no está en
  `clearinghouseState` sino en el estado spot. Se consulta
  `userAbstraction` y esos modos no se operan.

## Decisiones antes de la fase 4
1. Se rechazan los líderes en unified account, portfolio margin o
   dexAbstraction (`rank_leaders` también los descartará).
2. Sin entorno demo de Kraken: la fase 5 no lo soporta.
3. Sanity: N = 3 ciclos seguidos, con tope absoluto de 5.
4. Perfil de arranque live: más estricto que los topes normales (1x de
   apalancamiento total y 100 USD por activo, constantes en `limits.py`).
   Se activa por defecto la primera vez que se arranca en live y solo se
   quita de forma explícita (`--release-startup-profile` con confirmación
   escrita).

## Fase 4 (verificación para el modo paper, 2026-10-08)
- **Tickers** (`GET /derivatives/api/v3/tickers`): `markPrice`, `bid`, `ask`,
  `suspended`, `fundingRate` (absoluto) y `relativeFundingRate`. En los PF_,
  `fundingRate` ≈ `relativeFundingRate` x precio: USD por unidad y periodo.
- **Funding histórico** (`GET /derivatives/api/v3/historical-funding-rates`):
  marcas cada 3.600 s en la API real. El intervalo no se supone: se deduce
  de las marcas de tiempo. Signo: con tasa positiva pagan los largos.
- **Libro** (`GET /derivatives/api/v3/orderbook?symbol=`): `orderBook.bids`
  y `orderBook.asks` como `[precio, tamaño]`. La API real devuelve los bids
  en orden ascendente: el bot ordena siempre ambos lados.
- **EUR/USD:** perpetuo `PF_EURUSD` de Kraken; se usa su `indexPrice`.
- **Haircut del colateral EUR:** la documentación de la API no lo
  especifica. La ayuda de Kraken para el EEE (no accesible desde el entorno
  de desarrollo, leída vía buscador) indica 0 % de haircut y 0 % de
  conversión para EUR. Queda parametrizado (`paper.eur_haircut_pct = 0`).
- **Comisión taker:** el endpoint de comisiones está obsoleto desde
  2026-06-22 y la tabla oficial no es accesible desde aquí. Fuentes de
  terceros dan 0,05 % para el nivel inicial: valor por defecto
  parametrizado, a confirmar en la cuenta real.

## Decisiones antes de la fase 5
- Demo, comisión taker y haircut EUR: sin dato del usuario todavía. Se
  mantiene "sin soporte demo" y los valores por defecto del modo paper
  (0,05 % y 0 %), editables en `config.toml`.
- Circuit breaker (opción B):
  - El límite de órdenes por minuto nunca se supera. Lo que no cabe se aplaza
    al ciclo siguiente, en este orden de prioridad: cierres (totales y de
    cambio de dirección), reducciones, y aperturas o aumentos. Dentro de cada
    grupo, de mayor a menor nocional.
  - Superar el nocional por hora sigue deteniendo el bot.
  - Fuera de la sincronización inicial, si hay órdenes aplazadas en más de
    N = 3 ciclos seguidos (`risk.max_consecutive_paced_cycles`, tope absoluto
    5), el bot se detiene y alerta.
  - La sincronización inicial va desde el arranque del proceso hasta el
    primer ciclo sin aplazamientos. Si dura más de 10 ciclos con
    aplazamientos, el bot se detiene.
- Cierres de emergencia (drawdown y kill switch): solo órdenes reduceOnly,
  sin límite del circuit breaker, en hasta 5 rondas hasta quedar sin
  posiciones gestionadas. Si algo queda abierto, alerta crítica pidiendo el
  cierre manual.
  - Antes de esta fase el kill switch solo detenía el bot, sin cerrar nada.
    Ahora cierra lo gestionado (`risk.close_all_on_kill_switch = true`,
    desactivable). Lo hace una sola vez por parada, aunque el bot ya estuviera
    detenido.
- `rank_leaders`: se descartan los líderes con más de 8 posiciones
  simultáneas (propuesta). Con unos 550 USD y apalancamiento 2x hay
  1.100 USD de nocional; entre 8 posiciones salen unos 137 USD por posición,
  que coincide con el tope por activo del 25 %, y todas superan con holgura
  el mínimo de 10 USD.
