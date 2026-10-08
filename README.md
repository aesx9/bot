# copybot — Hyperliquid → Kraken Futures

> En construcción por fases. El README completo (instalación, puesta en marcha
> y procedimiento de emergencia) llega en la fase 7.

**Modo por defecto: paper.** El bot no envía órdenes reales salvo que se cumplan
a la vez: `mode = "live"` en config, flag `--live`, `--check` superado y
confirmación escrita.

## Desarrollo

```bash
make venv && make install
.venv/bin/pre-commit install
make check   # lint + mypy + tests + pip-audit + detect-secrets
```
