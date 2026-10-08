# copybot — Hyperliquid → Kraken Futures

Copia a escala las posiciones de una wallet de Hyperliquid (el **líder**) en
perpetuos `PF_*USD` de Kraken Futures. No copia órdenes sueltas: en cada
ciclo compara las posiciones objetivo con las tuyas y envía la diferencia.

> **Aviso.** Operar con apalancamiento puede hacerte perder todo el capital
> depositado, y más deprisa de lo que esperas. Este bot no es asesoramiento
> financiero ni fiscal. **El modo live nunca se ha probado con dinero real.**
> Empieza siempre en paper y después pasa a live con el capital mínimo.

- Especificación y registro de decisiones: [`SPEC.md`](SPEC.md).
- Modo por defecto: **paper** (cuenta simulada con precios reales, sin claves).

## Índice

1. [Cómo funciona](#cómo-funciona)
2. [Seguridad: lo que el bot nunca hace](#seguridad-lo-que-el-bot-nunca-hace)
3. [Instalación para desarrollo](#instalación-para-desarrollo)
4. [Configuración](#configuración)
5. [Despliegue en el VPS](#despliegue-en-el-vps)
6. [Puesta en marcha por fases](#puesta-en-marcha-por-fases)
7. [Operación diaria](#operación-diaria)
8. [Procedimiento de emergencia](#procedimiento-de-emergencia)
9. [Paradas automáticas y cómo reanudar](#paradas-automáticas-y-cómo-reanudar)
10. [Informes, fiscalidad y elección de líder](#informes-fiscalidad-y-elección-de-líder)
11. [Actualizar el bot](#actualizar-el-bot)
12. [Limitaciones conocidas](#limitaciones-conocidas)

---

## Cómo funciona

1. **Disparador:** el WebSocket `userFills` del líder (con debounce) más un
   ciclo de respaldo por REST cada `reconcile_interval_seconds`. Tras cada
   reconexión del WebSocket se fuerza un ciclo.
2. **Lectura del líder:** `clearinghouseState` (capital y posiciones) y
   `allMids`. Antes de operar, controles de cordura: datos obsoletos, capital
   no positivo, saltos absurdos de capital o de posición. Si algo falla, no
   se opera; si falla 3 ciclos seguidos, el bot se detiene.
3. **Filtros:** allow/deny, posiciones que el líder ya tenía al empezar
   (no se copian hasta que las cierre) y mapeo de símbolos (`BTC` →
   `PF_XBTUSD`, con overrides y `size_factor`).
4. **Tamaño:** modo `equity` (proporción de capitales × multiplier) o `fixed`
   (ratio fijo sobre el tamaño del líder), con topes por activo, por % de
   capital y de apalancamiento total.
5. **Plan:** abrir, aumentar, reducir, cerrar o cambiar de dirección (cierre
   reduceOnly más apertura aparte). Prioridad: cierres, después reducciones,
   después aperturas, de mayor a menor nocional.
6. **Ejecución:** órdenes límite IOC con tope de slippage del 0,5 %,
   redondeadas al paso de cada mercado y con `cliOrdId` único guardado antes
   de enviarlas. Si la respuesta se pierde, se pregunta a Kraken antes de
   hacer nada: nunca se duplica una orden.
7. **Protecciones:** circuit breaker, drawdown, kill switch, stops de
   catástrofe en el exchange y parada tras N ciclos con error.

Capital propio: en live, `marginEquity` de Kraken (incluye haircut y PnL no
realizado). En paper, 500 EUR simulados valorados con el EUR/USD de Kraken y un
haircut del 2,2 %.

## Seguridad: lo que el bot nunca hace

- **No retira ni transfiere fondos.** El cliente de Kraken solo puede llamar a
  9 endpoints de lectura y órdenes, y un test comprueba que en el código no
  hay ninguna ruta de retiro o transferencia.
- **No arranca en live por error.** Necesita las cuatro cosas a la vez:
  `mode = "live"` en la config, el flag `--live`, un `--check` superado con
  esa misma config y esa misma clave, y tu confirmación escrita.
- **No escribe secretos** en logs, CSV, alertas ni excepciones: se redactan.
- **No toca tus posiciones manuales** en mercados que no gestiona. Solo opera
  activos del líder o que el bot ya tenga registrados.
- **No puede pasar de los topes absolutos** de `src/copybot/limits.py`. Si la
  config los supera, el bot no arranca.

| Tope absoluto (código) | Valor |
| - | - |
| Apalancamiento total | 3x |
| Nocional por activo / total | 600 USD / 1.500 USD |
| Slippage máximo de una orden | 0,5 % |
| Órdenes por minuto | 10 (las que no caben se aplazan) |
| Nocional enviado por hora (aperturas y aumentos) | 2.000 USD (si se supera, el bot se detiene; reducciones y cierres no cuentan) |
| Drawdown desde el máximo | 15 % (cierra lo gestionado y se detiene) |
| Ciclos seguidos con error | 5 |
| Perfil de arranque live | 1x y 100 USD por activo hasta quitarlo a mano |

## Instalación para desarrollo

Requisitos: Python 3.12 y `uv` solo si vas a regenerar los locks.

```bash
make venv && make install          # dependencias fijadas con hash
.venv/bin/pre-commit install       # bloquea secretos, .env, CSV y logs en commits
make check                         # ruff + mypy + tests + pip-audit + detect-secrets

cp config.example.toml config.toml # pon leader_address
PYTHONPATH=src .venv/bin/python -m copybot.main --once     # un ciclo paper
PYTHONPATH=src .venv/bin/python -m copybot.main --status
```

En paper **no hace falta ningún `.env`**.

## Configuración

Todo está en `config.toml` (copia de [`config.example.toml`](config.example.toml),
comentado). Los valores se validan al arrancar y cualquier error impide el
arranque, con un mensaje claro.

Lo esencial:

| Sección | Qué decide |
| - | - |
| `leader_address` | Wallet del líder (0x…). |
| `[sizing]` | `mode` equity/fixed, `multiplier`, `fixed_ratio` y topes por activo, por % y de apalancamiento. |
| `[planner]` | Orden mínima (10 USD) y umbral de reajuste (5 %). |
| `[filters]` | `allow`, `deny` e ignorar las posiciones preexistentes del líder. |
| `[symbols]` | `overrides` (coin → `PF_…USD`) y `size_factor` (p. ej. `kPEPE`). |
| `[risk]` | Drawdown, circuit breaker, kill switch y stop de catástrofe (drawdown / apalancamiento: 15 % en el perfil de arranque, 7,5 % a 2x). |
| `[sanity]` | Controles del líder (salto ×20, capital ±50 %, N = 3). |
| `[paper]` | Colateral simulado, haircut EUR y comisión taker (0,05 %, verificada). |
| `[telegram]` | `enabled = true` para recibir alertas. |
| `[healthcheck]` | `enabled = true` + `HEALTHCHECK_URL` en `.env`: aviso externo (healthchecks.io) si el bot deja de dar señales. |
| `[logging]` | `external_rotation = true` en el VPS (rota logrotate). |

Secretos, **solo** en `.env` y con permisos `600` (ver
[`.env.example`](.env.example)):

```
KRAKEN_FUTURES_API_KEY=...       # solo live
KRAKEN_FUTURES_API_SECRET=...
TELEGRAM_BOT_TOKEN=...           # opcional
TELEGRAM_CHAT_ID=...
HEALTHCHECK_URL=...            # opcional: ping externo (healthchecks.io)
```

Clave de Kraken Futures: permisos **General = Full Access** (lectura y
trading), **Transferencias/retiros = No Access** y **restricción de IP** a la
del VPS. `--check` aborta si la clave puede transferir o retirar.

> Si un token o una clave aparece alguna vez en un chat, un correo o un
> commit, **revócalo y crea otro**. Un token de Telegram se revoca en
> @BotFather con `/revoke`; una clave de Kraken, desde la web de Kraken.

## Despliegue en el VPS

Ubuntu 24.04. Estructura:

| Ruta | Contenido | Propietario |
| - | - | - |
| `/opt/copybot/app` | Código (git clone) y `.venv` | root (solo lectura) |
| `/var/lib/copybot` | `config.toml`, `.env`, `service.env`, `STOP` | copybot (700) |
| `/var/lib/copybot/data/paper` | Estado, CSV, `logs/` y lockfile del modo **paper** | copybot (700) |
| `/var/lib/copybot/data/live` | Lo mismo para el modo **live** | copybot (700) |

### 1. Preparar el servidor

Desde tu PC, con un usuario administrador que ya entre por SSH **con clave**:

```bash
sudo git clone <url-del-repositorio> /opt/copybot/app
cd /opt/copybot/app
sudo ADMIN_USER=$USER bash deploy/setup_vps.sh
```

El script ([`deploy/setup_vps.sh`](deploy/setup_vps.sh)) es idempotente y hace:

- actualizaciones y parches de seguridad automáticos;
- SSH solo con clave, sin root y solo para `ADMIN_USER`. Antes comprueba
  que tienes clave para no dejarte fuera y después verifica con `sshd -T` que la configuración
  efectiva se aplica (otro fichero de `sshd_config.d` no puede anularla);
- `ufw` cerrando todo lo entrante salvo SSH, y `fail2ban` para SSH;
- usuario de sistema `copybot` sin privilegios ni shell;
- entorno virtual con dependencias verificadas por hash;
- `config.toml` en **paper**, el servicio systemd endurecido
  ([`deploy/copybot.service`](deploy/copybot.service)), logrotate
  ([`deploy/logrotate.conf`](deploy/logrotate.conf)) y el atajo
  `copybot-cli`.

**No arranca el bot.**

### 2. Dos formas de ejecutarlo, nunca a la vez

- **Servicio** (`systemctl start|stop|status copybot`): el modo normal.
- **`sudo copybot-cli …`**: comandos a mano, ejecutados como el usuario
  `copybot` y con las mismas rutas que el servicio.

Hay dos protecciones contra dos instancias a la vez:

1. `copybot-cli` se niega a ejecutar nada que no sea `--status` (solo lectura: no toma el bloqueo
   y dice si hay una instancia en marcha) mientras el
   servicio está activo. Para hacer otra cosa:
   `sudo systemctl stop copybot`, después el comando, después
   `sudo systemctl start copybot`.
2. El bot toma un bloqueo (`flock`) sobre `data/<modo>/copybot.lock`: una segunda
   instancia termina con "ya hay una instancia en marcha", venga de donde
   venga.

No ejecutes el bot con tu usuario ni con otras rutas: tendría otro estado y
otro bloqueo, y sí podrían operar dos a la vez.

Códigos de salida: `0` correcto, `1` error, `2` uso o confirmación pendiente
y `3` detenido por una protección. Con `2` y `3`, systemd **no** reinicia el
bot: hay que revisarlo a mano.

## Puesta en marcha por fases

### Fase A — Paper (sin claves), mínimo 2–4 semanas

```bash
sudoedit /var/lib/copybot/config.toml   # leader_address; mode = "paper"
sudo copybot-cli --once                 # un ciclo de prueba
sudo systemctl start copybot            # paper continuo
sudo copybot-cli --status
journalctl -u copybot -f                # o /var/lib/copybot/data/paper/logs/copybot.log
```

Revisa cada pocos días con `report` ([más abajo](#informes-fiscalidad-y-elección-de-líder)):
slippage, retraso, funding y drawdown. Si el líder no te convence, prueba
otro con `rank_leaders`.

### Fase B — Live con capital mínimo

> El modo live arranca con un estado **nuevo** en `data/live`: nada del paper (posiciones
> gestionadas, pico de capital, posiciones previas del líder) se hereda.

1. **Deposita en Kraken Futures solo lo que aceptes perder** (el plan parte
   de unos 500 EUR como colateral).
   La cuenta debe estar **sin posiciones abiertas**: `--check` y el primer `--live` se
   niegan si hay alguna (el bot la tomaría como suya y podría cerrarla). Lo ideal es una
   cuenta de Kraken solo para el bot.
2. **Crea la clave** (General Full Access, sin transferencias, IP del VPS) y
   escríbela en `/var/lib/copybot/.env`:
   ```bash
   sudoedit /var/lib/copybot/.env
   sudo chown copybot:copybot /var/lib/copybot/.env && sudo chmod 600 /var/lib/copybot/.env
   ```
3. **Cambia a live** en `config.toml` (`mode = "live"`) y comprueba todo:
   ```bash
   sudo systemctl stop copybot
   sudo copybot-cli --check
   ```
   `--check` no envía órdenes. Revisa conectividad, permisos de la clave,
   cuenta, mercados y líder, y guarda el resultado ligado a esta config y a
   esta clave. Si cambias cualquiera de las dos, hay que repetirlo.
4. **Primer arranque live, a mano:**
   ```bash
   sudo copybot-cli --live
   ```
   Muestra el resumen de topes y pide escribir `OPERAR CON DINERO REAL`. La
   primera vez activa el **perfil de arranque** (1x y 100 USD por activo).
   Deja que haga uno o dos ciclos y para con `Ctrl+C`. Comprueba en la web de
   Kraken las posiciones y los stops de catástrofe (órdenes stop reduceOnly al
   15 % de la entrada con el perfil de arranque: el drawdown dividido por el apalancamiento).
5. **Pasa a servicio:**
   ```bash
   echo "COPYBOT_EXTRA_ARGS=--live" | sudo tee /var/lib/copybot/service.env
   sudo systemctl start copybot
   ```
   La confirmación escrita queda guardada y systemd puede reiniciar el bot
   tras una caída sin pedirla. **Deja de valer** (y el servicio sale con
   código 2 sin operar) si cambia la config, la clave o el código, tras
   cualquier parada del bot y tras `--reset-halt`. Entonces repite el paso 4.
6. **Después de semanas sin sorpresas**, si quieres quitar el perfil de
   arranque:
   ```bash
   sudo systemctl stop copybot
   sudo copybot-cli --release-startup-profile   # escribe la frase pedida
   sudo copybot-cli --live && sudo systemctl start copybot
   ```

En el **primer cobro o pago de funding real**, el bot comprueba su signo
(con tasa positiva paga el largo). Si no cuadra, recibes una alerta
crítica: avísalo antes de fiarte de `funding.csv`.

## Operación diaria

```bash
sudo copybot-cli --status                 # parada, motivo, pico, gestionados, pendientes
sudo systemctl status copybot
journalctl -u copybot --since today
tail -f /var/lib/copybot/data/live/logs/copybot.log     # o paper/
```

Ficheros en `/var/lib/copybot/data/<modo>` (`paper` o `live`; **cada modo tiene su
propio estado, bloqueo, logs y CSV y no comparten nada**: el estado guarda el modo y el bot
se niega a arrancar si no coincide):

| Fichero | Contenido |
| - | - |
| `state.json` | Estado persistente (escritura atómica). No lo edites con el bot en marcha. |
| `trades.csv` | Cada ejecución del bot: precios, slippage, comisión y retraso (paper/live). |
| `funding.csv` | Funding por evento, con pagado y cobrado separados. |
| `equity.csv` | Capital propio y del líder, cada 15 minutos. |
| `kraken_fills.csv` | **Solo live:** todos los fills reales de la cuenta, con origen. |
| `fees.csv` | **Solo live:** comisiones reales del log de cuenta de Kraken, con su moneda. |
| `positions.csv` | **Solo live:** foto de las posiciones reales de Kraken, tomada en cada actualización del libro (cada ciclo, también con el bot detenido) pero escrita solo si cambian o, sin cambios, cada hora; el export fiscal las concilia con los fills. |

**Vigilancia externa (recomendada en live):** el bot es su propio único canal de alerta, así
que si el proceso muere o el VPS cae nadie avisa. Dos defensas independientes del bot:

- `OnFailure=copybot-failure.service`: si el servicio queda en *failed* (salida 2/3 por una
  protección, o reinicios agotados), esa unidad avisa por Telegram desde fuera del bot.
- Healthcheck externo (opcional): crea un check en healthchecks.io (periodo 10 min, gracia
  5 min), pon su URL en `HEALTHCHECK_URL` y `[healthcheck] enabled = true`. El heartbeat
  hace ping si el último ciclo terminó bien y hace menos de 3 intervalos; ante un fallo,
  una parada o un ciclo colgado marca `/fail`; si dejan de llegar pings (VPS caído), el
  servicio externo te avisa.

`systemctl stop copybot` y Ctrl+C son una **parada ordenada**: el bot termina el ciclo en
curso, guarda el estado y sale con código 0 (posiciones y stops se quedan como están).

**Alertas por Telegram:** pon `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en
`.env` y `enabled = true` en `[telegram]`. Avisan del arranque, de las
paradas, del drawdown, de los errores, de los ciclos saltados y del signo
del funding. Los avisos repetidos se agrupan cada 10 minutos.

## Procedimiento de emergencia

**Quiero cerrar todo YA:**

1. **Kill switch** (el bot cierra lo que gestiona y se detiene):
   ```bash
   sudo -u copybot touch /var/lib/copybot/STOP
   ```
   Un vigilante lo detecta en ~1 s (aunque `reconcile_interval_seconds` sea alto)
   y cierra todas las posiciones gestionadas con órdenes reduceOnly,
   sin límite de órdenes por minuto y en hasta 5 rondas, y se detiene. Si
   algo queda abierto, envía una alerta crítica y sigue reintentando el cierre mientras el
   proceso esté vivo (cada símbolo se cierra por separado: un mercado con problemas no bloquea los
   demás); si aun así no se cierra, hazlo a mano.
   **No toca tus posiciones manuales.**
2. Comprueba el resultado con `sudo copybot-cli --status` y en la web de
   Kraken.
3. **Si el bot no responde o el VPS ha caído**, cierra a mano en
   la web o la app de Kraken Futures. Los stops de catástrofe del exchange
   siguen protegiendo cada posición mientras tanto.

**Quiero parar el bot sin tocar las posiciones:**
`sudo systemctl stop copybot`. Las posiciones y los stops de catástrofe
quedan abiertos en Kraken.

**Sospecho que la clave está comprometida:**

1. Revócala en Kraken (API → borrar clave). El bot dejará de poder operar.
2. Cierra posiciones a mano en Kraken si hace falta.
3. Crea otra clave (sin transferencias, IP del VPS), actualiza `.env` y
   repite `--check` y el primer `--live` a mano.

**Después de una emergencia:** revisa el motivo con `--status` y los logs;
borra `/var/lib/copybot/STOP` si lo creaste; ejecuta
`sudo copybot-cli --sync-ledger` (ver abajo); ejecuta
`sudo copybot-cli --reset-halt` (escribe `REANUDAR`). Después, repite el
primer arranque live a mano (la confirmación se invalidó).

**Libro fiscal con el bot detenido (`--sync-ledger`):** el bot actualiza el libro
(`kraken_fills.csv`, `fees.csv`, `funding.csv`, `positions.csv`) al detenerse y en cada ciclo
mientras sigue vivo reintentando un cierre, pero un bot detenido y parado no ve lo que pase
después en la cuenta (un stop de catástrofe que salta, una liquidación, un cierre manual).
`sudo copybot-cli --sync-ledger` lo trae: solo hace lecturas en Kraken (fills, log de cuenta y
posiciones), no envía ni cancela órdenes y no quita la parada. Ejecútalo tras cualquier parada,
antes de abandonar el bot y antes del export fiscal.

## Paradas automáticas y cómo reanudar

> Solo el drawdown y el fichero `STOP` cierran posiciones. **Cualquier otra parada deja las
> posiciones abiertas** (con los stops de catástrofe del exchange: distancia = drawdown /
> apalancamiento) y envía una alerta CRÍTICA que lista lo que queda abierto: revísalo y ciérralo
> a mano si hace falta, porque un bot detenido no vigila nada.

| Motivo | Qué hace el bot | Qué hacer |
| - | - | - |
| Fichero `STOP` | Cierra lo gestionado (configurable) y se detiene | Ver emergencia |
| Drawdown ≥ 15 % desde el máximo | Cierra lo gestionado y se detiene | Revisar antes de reanudar |
| Nocional de aperturas y aumentos por hora > 2.000 USD | Se detiene | Revisar qué hizo el líder |
| Órdenes aplazadas > 3 ciclos seguidos (fuera de la sincronización inicial) | Se detiene | Revisar la actividad del líder |
| 3 ciclos seguidos con datos del líder sospechosos | Se detiene sin operar | ¿Depósito o retiro del líder? |
| 5 ciclos seguidos con error | Se detiene | Logs: red, API o claves |
| Tope absoluto superado | Se detiene | Revisar la config |

Las paradas **persisten tras reiniciar**. Para reanudar:
`sudo copybot-cli --reset-halt`, que pide escribir `REANUDAR` y reinicia la
referencia de los controles del líder. En live hay que repetir después el
primer arranque a mano.

## Informes, fiscalidad y elección de líder

Con el servicio en marcha se pueden ejecutar sin pararlo (solo leen los CSV):

```bash
cd /opt/copybot/app
sudo -u copybot env PYTHONPATH=src .venv/bin/python scripts/report.py \
    --data-dir /var/lib/copybot/data/live [--mode paper|live] [--json]
```

**`report`**: rentabilidad y drawdown máximo (sobre `equity.csv`; incluye
depósitos y retiros), PnL realizado, slippage medio y ponderado,
comisiones, funding pagado, cobrado y neto y retraso medio, global y por
activo. Nunca mezcla paper y live.

**`export_fiscal`** (solo operaciones reales; **paper se excluye siempre**):

```bash
sudo -u copybot env PYTHONPATH=src .venv/bin/python scripts/export_fiscal.py \
    --year 2026 --data-dir /var/lib/copybot/data/live --out /var/lib/copybot/data/live
```

Genera cuatro ficheros:

- `fiscal_posiciones_<año>.csv`: una fila por posición cerrada en el año,
  con resultado bruto, comisiones, funding pagado y cobrado (columnas
  separadas) y neto, en USD y en EUR. Incluye el origen del cierre (bot, stop
  de catástrofe, liquidación o manual) y todos los orígenes que intervinieron.
- `fiscal_funding_<año>.csv`: cada pago o cobro de funding, también de
  posiciones aún abiertas.
- `fiscal_resumen_<año>.csv`: subtotales por origen y total.
- `fiscal_conciliacion_<año>.csv`: por cada foto de `positions.csv`, el neto de los
  fills frente a la posición real de Kraken. Si no cuadra (`NO CUADRA`) falta o sobra
  algún fill y el resultado de ese mercado no es fiable: el script lo avisa. Los fills,
  comisiones y funding repetidos en los CSV se cuentan una sola vez (por `fill_id` y
  `booking_uid`).

Conversión a EUR con el **tipo de referencia diario del BCE**, cada flujo en
su fecha **en hora de Madrid** (también el año fiscal; los CSV siguen en UTC): el resultado, al tipo del día de cierre; cada comisión, al del día
en que se cobró; cada funding, al del día de su pago. Si ese día no hay tipo
publicado, se usa el último anterior; la fuente y la fecha usada constan en
el fichero. Si el BCE no responde, descarga `eurofxref-hist.csv` de la web
del BCE y pásalo con `--ecb-csv`. Las posiciones aún abiertas no se declaran
hasta que se cierran (el script avisa). Revisa los ficheros con tu asesor.

**`rank_leaders`**: ranking por Sharpe de los últimos 30 días.

```bash
.venv/bin/python scripts/rank_leaders.py --wallets ~/candidatas.txt --out ~/ranking.csv
.venv/bin/python scripts/rank_leaders.py --leaderboard --top 30 --out ~/ranking.csv  # NO OFICIAL
```

Descarta:

- cuentas en unified account o portfolio margin;
- menos de 30 días de historial;
- scalpers (más de 40 fills al día) y wallets inactivas (menos de 5 fills
  en 30 días);
- más de un 10 % del volumen en activos sin mercado en Kraken;
- más de 8 posiciones simultáneas.

El leaderboard no forma parte de la API documentada de Hyperliquid y puede
cambiar o desaparecer.

## Copias de seguridad

`state.json` (parada, pico de capital, posiciones gestionadas) y los CSV (en live, el libro
fiscal: `kraken_fills.csv`, `fees.csv`, `funding.csv`) viven solo en el VPS. El timer
`copybot-backup.timer` guarda cada día una copia (`state.json` + `*.csv`, **sin** `.env`, config
ni logs) en `/var/backups/copybot`, 0600, y conserva las 14 últimas.

Es una copia **en la misma máquina**: protege de un borrado o de un fichero corrupto, no de
perder el VPS. Trae las copias a otro sitio desde tu PC, p. ej.:

```bash
rsync -a --rsync-path="sudo rsync" tu_usuario@tu_vps:/var/backups/copybot/ ~/copias-copybot/
```

Restaurar: con el bot parado, `tar -xzf copybot-<fecha>.tar.gz -C /var/lib/copybot/data` y
`chown -R copybot:copybot /var/lib/copybot/data`. Kraken conserva el historial de fills y de
cuenta: es la segunda fuente si el libro local se pierde.

## Actualizar el bot

```bash
sudo systemctl stop copybot
cd /opt/copybot/app && sudo git pull
sudo .venv/bin/python -m pip install --require-hashes --no-deps -r requirements.lock
sudo install -m 644 deploy/copybot*.service deploy/copybot-backup.timer /etc/systemd/system/ && sudo install -m 755 deploy/copybot-backup /usr/local/bin/ && sudo systemctl daemon-reload
sudo copybot-cli --check        # solo live
sudo copybot-cli --live         # solo live: el código cambió, hay que confirmar de nuevo
sudo systemctl start copybot
```

## Limitaciones conocidas

- **Live nunca se ha ejecutado con dinero real.** Está probado contra una API
  privada de Kraken simulada a partir de la documentación oficial.
- **Verificados contra el servicio real el 2026-10-08:** el SDMX-CSV de la API
  del BCE y el JSON del leaderboard no oficial de Hyperliquid (47.107 filas, sin
  ninguna fila ilegible). **Siguen sin verificar:** el formato de
  `eurofxref-hist.csv` (`--ecb-csv`; `www.ecb.europa.eu` estaba bloqueado) y el
  signo del funding real (el bot lo comprueba y alerta en el primer pago).
- **La comisión real de cada operación live** no viene en la respuesta de la
  orden: queda vacía en `trades.csv` y se toma del log de cuenta
  (`fees.csv`).
- **El funding en paper** se aplica en cada marca horaria con el tamaño de
  la posición en ese momento. Kraken lo devenga de forma continua.
- **Kraken demo** (`demo-futures.kraken.com`) redirige a la web comercial: no
  hay entorno de pruebas. Por eso existe el perfil de arranque.
