#!/usr/bin/env bash
# Comprueba la configuración EFECTIVA de sshd (sshd -T), no solo su sintaxis (sshd -t).
#
# sshd usa el PRIMER valor que encuentra de cada directiva y los ficheros de
# /etc/ssh/sshd_config.d/ se leen en orden alfabético: un 50-cloud-init.conf con
# "PasswordAuthentication yes" anula a un 99-copybot.conf. Por eso copybot usa 00-.
#
#   verify_sshd.sh ADMIN_USER     (sale con 1 y dice qué directiva no se aplica)
set -euo pipefail

admin="${1:?uso: verify_sshd.sh ADMIN_USER}"
effective="$(sshd -T)"

fail=0
for want in \
    "passwordauthentication no" \
    "kbdinteractiveauthentication no" \
    "pubkeyauthentication yes" \
    "permitrootlogin no" \
    "permitemptypasswords no" \
    "x11forwarding no" \
    "maxauthtries 3" \
    "allowusers $admin"; do
    if ! grep -qix -- "$want" <<<"$effective"; then
        got="$(grep -i -m1 -- "^${want%% *} " <<<"$effective" || echo '(sin valor)')"
        echo "ERROR: sshd NO aplica '$want' (efectivo: $got)." >&2
        echo "       Otro fichero de /etc/ssh/sshd_config.d/ la anula; revisa: sshd -T" >&2
        fail=1
    fi
done
exit "$fail"
