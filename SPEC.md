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
- "Órdenes a mercado" se implementan como límite IOC con tope de slippage.

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
- **Hyperliquid WebSocket:** la documentación dice que los mensajes en
  streaming de `userFills` llevan `isSnapshot: false`; en la API real ese
  campo no viene. Ausente se trata como `false`.
- **Hyperliquid, modos de cuenta:** con "unified account" o "portfolio
  margin", la documentación indica que el capital no está en
  `clearinghouseState` sino en el estado spot. Se consulta
  `userAbstraction` y esos modos no se operan (pendiente de decisión).
