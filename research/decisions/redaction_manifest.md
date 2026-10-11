# Manifiesto de redacción de las copias públicas

Fecha: 2026-10-11. Alcance: tres prerregistros congelados cuya versión publicada en
este repositorio lleva redactado un fragmento de texto descriptivo.

## Qué se ha redactado y por qué

Los originales citan la procedencia de la tarifa taker nombrando el nivel de
comisiones de la cuenta del usuario y el tramo de volumen correspondiente. Esa
información identifica datos de la cuenta y no es necesaria para reproducir ningún
ensayo: lo necesario es la tarifa empleada, que se conserva íntegra y sin cambios en
todos los ficheros (`fee_taker: 0.0005`, `futures_taker_fee: 0.0005`).

Las redacciones son exclusivamente de texto descriptivo. **No alteran parámetros,
datos, umbrales, universos, reglas, criterios de aceptación ni resultados de ningún
experimento.** El protocolo operativo congelado no cambia.

## Dónde está cada versión

- **Original interno:** se conserva fuera de este repositorio, en la carpeta de
  trabajo de investigación. No se publica. Es la versión cuyo hash aparece en el
  registro histórico de decisiones y que verifican los validadores y el código de
  los motores.
- **Copia pública:** es el fichero de este repositorio, con el fragmento redactado.
  Su hash es distinto por construcción.

Los originales no se han sobrescrito ni se ha alterado su identidad histórica: sus
SHA-256 siguen siendo los registrados el día de su congelación, verificados el
2026-10-11 antes de derivar las copias.

## Hashes

| Fichero | SHA-256 del original interno | SHA-256 de la copia pública |
|---|---|---|
| `hypotheses/funding_carry_cross_sectional_v1.yaml` | `adf6b882e5dc7eca3bbf053f97acb8a0bd98295ef4e2f7a7226caed36a35904d` | `7d63433bc55e4c9af3759122dfc093191ef4e22ecf0426a6bb51a7d1636ff5c1` |
| `hypotheses/funding_carry_cross_sectional_v1.proposal.yaml` | `aa882507d8152975584931e92a5d9c5ad87b789af79e7045d68720d9029725c5` | `5d92a2ec62c213fb9d4161594488c38ec0330b58222e0a3c046f99b3834d15ad` |
| `hypotheses/ml_daily_direction_multiasset_v4.yaml` | `3440417ea3bd65b5662b90b701bc4a07548072f79f361f1597aa4d3840760196` | `53d946d922ab6bcb61d496e4313245d1fdd56e15e74c313a05ce5b0d062d12af` |

## Fragmentos redactados, uno por uno

### 1. `hypotheses/funding_carry_cross_sectional_v1.yaml`, clave `costs.fee_source`

- Original: nombraba el nivel de comisiones de la cuenta, la fecha de verificación y
  el fichero de evidencia.
- Copia pública: `"[REDACTADO] Tarifa taker verificada el 2026-10-10 contra evidencia
  archivada fuera del repositorio; ver decisions/redaction_manifest.md."`
- `fee_taker: 0.0005` no cambia. El resto del fichero es idéntico al original,
  byte a byte, incluido el número de líneas (280).

### 2. `hypotheses/funding_carry_cross_sectional_v1.proposal.yaml`, clave `costs.fee_source`

- Misma clave y mismo texto original que el anterior; misma redacción.
- El resto del fichero es idéntico al original, byte a byte (278 líneas).
- Nota: el campo `proposal_history` del prerregistro congelado cita el hash
  `aa882507...25a5`, que es el del **original interno** de esta propuesta.

### 3. `hypotheses/ml_daily_direction_multiasset_v4.yaml`, clave `costs.fee_schedule.tier`

- Original: describía el tramo de la tabla de tarifas nombrando el intervalo de
  volumen a 30 días.
- Copia pública: `"[REDACTADO] tramo de la tabla pública de Kraken Futures
  correspondiente a la tarifa taker de 0,0005; ver decisions/redaction_manifest.md."`
- `futures_taker_fee: 0.0005` y el resto de tarifas no cambian. El resto del fichero
  es idéntico al original, byte a byte (648 líneas).

## Consecuencias que hay que tener presentes

1. Las citas de hash del registro histórico de decisiones y de `experiments.csv`
   se refieren al **original interno**. No coinciden con el fichero publicado, y eso
   es intencionado: el registro histórico no se reescribe.
2. El código de los motores que comprueba el hash del prerregistro (por ejemplo
   `funding_carry_v1/config.py`, con `YAML_SHA256`) espera el original interno. Ese
   código no se publica en este repositorio. Quien reproduzca los ensayos desde la
   copia pública debe usar el original interno o actualizar la constante, y en
   cualquier caso debe partir del original.
3. Los validadores de contenido (`hypotheses/validate_funding_carry_v1.py`,
   `hypotheses/validate_v4.py`) comprueban el contenido, no el hash, y siguen dando
   el mismo resultado sobre la copia pública, porque la redacción no afecta a ningún
   campo que validen.
4. La evidencia de la tarifa (captura de la cuenta) no se versiona. Está fuera del
   repositorio y queda referenciada por su SHA-256 en el registro de decisiones.

## Cómo comprobar una copia pública

Partiendo del original interno, aplicar la sustitución del fragmento indicado para
ese fichero y comparar el SHA-256 resultante con la columna de copia pública de la
tabla. El número de líneas y los finales de línea se conservan, así que el resto del
contenido debe coincidir byte a byte.
