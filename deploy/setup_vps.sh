#!/usr/bin/env bash
# Preparación de un VPS Ubuntu 24.04 para copybot. Ejecutar como root:
#
#   sudo ADMIN_USER=tu_usuario bash deploy/setup_vps.sh
#
# Requisitos ANTES de ejecutarlo:
# - ADMIN_USER existe, está en el grupo sudo y ya entra por SSH con CLAVE
#   (~ADMIN_USER/.ssh/authorized_keys no vacío). Si no, el script se detiene:
#   desactivar las contraseñas te dejaría fuera del servidor.
# - El código está en /opt/copybot/app (git clone del repositorio).
#
# Qué hace (es idempotente: se puede repetir):
# 1. Actualizaciones y paquetes: python3.12-venv, git, ufw, fail2ban,
#    unattended-upgrades (solo parches de seguridad automáticos).
# 2. SSH solo con clave, sin root ni contraseñas.
# 3. Cortafuegos: todo cerrado salvo SSH. El bot solo hace conexiones salientes.
# 4. fail2ban para SSH.
# 5. Usuario de sistema "copybot" sin privilegios ni shell.
# 6. Entorno virtual con dependencias fijadas por hash.
# 7. /var/lib/copybot (700) con config de ejemplo en modo PAPER.
# 8. Servicio systemd, logrotate y el atajo copybot-cli. NO arranca el bot.
set -euo pipefail

APP=/opt/copybot/app
DATA=/var/lib/copybot

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "ejecútalo como root (sudo)"
[[ -n "${ADMIN_USER:-}" ]] || die "indica ADMIN_USER=usuario_con_sudo_y_clave_ssh"
id "$ADMIN_USER" >/dev/null 2>&1 || die "el usuario $ADMIN_USER no existe"
id -nG "$ADMIN_USER" | grep -qw sudo || die "$ADMIN_USER no está en el grupo sudo"
AUTH_KEYS="$(getent passwd "$ADMIN_USER" | cut -d: -f6)/.ssh/authorized_keys"
[[ -s "$AUTH_KEYS" ]] || die "$AUTH_KEYS vacío: añade tu clave SSH antes (te quedarías fuera)"
[[ -f "$APP/pyproject.toml" ]] || die "no encuentro el código en $APP (git clone ... $APP)"
grep -q 'VERSION_ID="24.04"' /etc/os-release || echo "AVISO: probado para Ubuntu 24.04"

echo "== 1. Paquetes y actualizaciones"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install python3.12 python3.12-venv git ufw fail2ban unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
systemctl enable --now unattended-upgrades

echo "== 2. SSH solo con clave"
# 00-: sshd usa el primer valor de cada directiva y lee los ficheros en orden alfabético;
# un 50-cloud-init.conf (típico en VPS) con PasswordAuthentication yes anularía un 99-.
rm -f /etc/ssh/sshd_config.d/99-copybot.conf   # nombre antiguo, que perdía ese orden
cat > /etc/ssh/sshd_config.d/00-copybot.conf <<CONF
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
PermitEmptyPasswords no
X11Forwarding no
MaxAuthTries 3
AllowUsers $ADMIN_USER
CONF
sshd -t || die "configuración de SSH no válida; no se recarga"
# Sintaxis válida no significa que se aplique: se verifica la configuración efectiva
bash "$APP/deploy/verify_sshd.sh" "$ADMIN_USER" || die "sshd no aplica la configuración segura; no se recarga"
systemctl reload ssh

echo "== 3. Cortafuegos"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "== 4. fail2ban"
cat > /etc/fail2ban/jail.d/copybot-sshd.local <<'CONF'
[sshd]
enabled = true
maxretry = 4
findtime = 10m
bantime = 1h
CONF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "== 5. Usuario de servicio"
id copybot >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin copybot
install -d -o copybot -g copybot -m 700 "$DATA" "$DATA/data"

echo "== 6. Entorno virtual (dependencias con hash)"
chown -R root:root "$APP"
chmod -R go-w "$APP"
git config --system --get-all safe.directory | grep -qx "$APP" \
    || git config --system --add safe.directory "$APP"
[[ -d "$APP/.venv" ]] || python3.12 -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install -q --upgrade pip
"$APP/.venv/bin/python" -m pip install -q --require-hashes --no-deps -r "$APP/requirements.lock"
# El paquete se ejecuta desde src/ (PYTHONPATH en el servicio y en copybot-cli):
# así no hace falta instalar setuptools sin hash.

echo "== 7. Configuración (paper por defecto)"
if [[ ! -f "$DATA/config.toml" ]]; then
    sed -e 's|^data_dir = "data".*|data_dir = "/var/lib/copybot/data"|' \
        -e 's|^external_rotation = false.*|external_rotation = true|' \
        "$APP/config.example.toml" > "$DATA/config.toml"
    echo "   creada $DATA/config.toml (modo paper): pon la wallet del líder"
fi
[[ -f "$DATA/service.env" ]] || echo "COPYBOT_EXTRA_ARGS=" > "$DATA/service.env"
chown copybot:copybot "$DATA/config.toml" "$DATA/service.env"
chmod 600 "$DATA/config.toml" "$DATA/service.env"
if [[ -f "$DATA/.env" ]]; then
    chown copybot:copybot "$DATA/.env"
    chmod 600 "$DATA/.env"
fi

echo "== 8. systemd, logrotate y copybot-cli"
install -m 644 "$APP/deploy/copybot.service" /etc/systemd/system/copybot.service
install -m 644 "$APP/deploy/logrotate.conf" /etc/logrotate.d/copybot
install -m 755 "$APP/deploy/copybot-cli" /usr/local/bin/copybot-cli
systemctl daemon-reload
systemctl enable copybot

echo
echo "Listo. El bot NO se ha arrancado. Siguientes pasos (README, 'Puesta en marcha'):"
echo "  1. sudoedit $DATA/config.toml       # leader_address, revisar topes"
echo "  2. sudo copybot-cli --once          # un ciclo paper de prueba"
echo "  3. sudo systemctl start copybot     # paper continuo"
