#!/usr/bin/env bash
# Contrôle de compromission d'une application PHP-FPM, en lecture seule.
# Écrit après la faille PHP2Shell (CVE-2026-109618, CVE-2026-109619) pour un panel Pterodactyl,
# utilisable pour toute application servie par PHP-FPM sous un compte dédié.
#
# Usage : sudo ./controle-intrusion.sh [date de début AAAA-MM-JJ] [dossier de l'application] [compte]
# Exemple : sudo ./controle-intrusion.sh 2026-10-21 /var/www/pterodactyl www-data
#
# Le script ne modifie rien. Une ligne [alerte] ne prouve pas une intrusion et une sortie propre
# ne prouve pas qu'il n'y en a pas eu : il repère les traces habituelles des attaques automatiques.
# En cas d'alerte, ne rien supprimer, garder la sortie et enquêter.

set -u

DEPUIS="${1:-2026-10-21}"
APP="${2:-/var/www/pterodactyl}"
COMPTE="${3:-www-data}"

ALERTES=0
A_LIRE=0

titre()  { printf '\n== %s\n' "$1"; }
ok()     { printf '  [ok]      %s\n' "$1"; }
a_lire() { printf '  [à lire]  %s\n' "$1"; A_LIRE=$((A_LIRE + 1)); }
alerte() { printf '  [alerte]  %s\n' "$1"; ALERTES=$((ALERTES + 1)); }
detail() { sed 's/^/             /' | head -n "${1:-20}"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "À lancer avec sudo : les journaux et les fichiers de l'application ne sont lisibles que par root."
    exit 2
fi
if ! date -d "$DEPUIS" >/dev/null 2>&1; then
    echo "Date invalide : $DEPUIS (format attendu AAAA-MM-JJ)"
    exit 2
fi
if [ ! -d "$APP" ]; then
    echo "Dossier introuvable : $APP"
    exit 2
fi
if ! id "$COMPTE" >/dev/null 2>&1; then
    echo "Compte introuvable : $COMPTE"
    exit 2
fi

echo "Contrôle depuis le $DEPUIS, application $APP, compte $COMPTE"

titre "1. Plantages dans les journaux de PHP-FPM et nginx"
JOURNAUX=$(ls /var/log/php*-fpm.log* /var/log/nginx/*error.log* 2>/dev/null)
if [ -z "$JOURNAUX" ]; then
    a_lire "aucun journal de PHP-FPM ou nginx trouvé à l'endroit habituel"
else
    # shellcheck disable=SC2086
    PLANTAGES=$(zgrep -ihE 'signal (11|7|6)|SIGSEGV|SIGBUS|Allowed memory size' $JOURNAUX 2>/dev/null)
    if [ -z "$PLANTAGES" ]; then
        ok "aucun plantage ni manque de mémoire"
    else
        a_lire "$(printf '%s\n' "$PLANTAGES" | wc -l) ligne(s) : regarder leurs dates (suspect si plusieurs, rapprochées, après le $DEPUIS)"
        printf '%s\n' "$PLANTAGES" | tail -n 20 | detail 20
    fi
fi

titre "2. Programmes qui tournent sous $COMPTE"
ATTENDUS='php-fpm: pool |nginx: (worker|cache)|artisan (queue:work|schedule:run)'
INCONNUS=$(ps -u "$COMPTE" -o pid=,lstart=,args= 2>/dev/null | grep -vE "$ATTENDUS")
if [ -z "$INCONNUS" ]; then
    ok "seulement les processus attendus (workers PHP-FPM, nginx, file d'attente)"
else
    alerte "processus inattendus sous $COMPTE"
    printf '%s\n' "$INCONNUS" | detail 30
fi

titre "3. Fichiers PHP dans le dossier public"
if [ -d "$APP/public" ]; then
    PHP_PUBLICS=$(find "$APP/public" -type f -name '*.php' ! -path "$APP/public/index.php" 2>/dev/null)
    if [ -z "$PHP_PUBLICS" ]; then
        ok "seulement public/index.php"
    else
        alerte "fichiers PHP en plus de index.php (webshell possible)"
        printf '%s\n' "$PHP_PUBLICS" | detail 30
    fi
else
    a_lire "pas de dossier public dans $APP : vérification à adapter"
fi

titre "4. Code modifié depuis le $DEPUIS"
MODIFIES=$(find "$APP" -type f -newermt "$DEPUIS" \
    -not -path '*/storage/*' -not -path '*/bootstrap/cache/*' -not -path '*/.git/*' 2>/dev/null)
if [ -z "$MODIFIES" ]; then
    ok "aucun fichier de code modifié"
else
    a_lire "$(printf '%s\n' "$MODIFIES" | wc -l) fichier(s) modifié(s) : normal seulement après une mise à jour de l'application"
    printf '%s\n' "$MODIFIES" | detail 30
fi

titre "5. Fichiers de $COMPTE dans les dossiers temporaires"
TEMP=$(find /tmp /var/tmp /dev/shm -user "$COMPTE" -type f 2>/dev/null)
if [ -z "$TEMP" ]; then
    ok "rien"
else
    alerte "fichiers de $COMPTE dans /tmp, /var/tmp ou /dev/shm"
    printf '%s\n' "$TEMP" | detail 30
fi

titre "6. Moyens de revenir"
TACHES=$(crontab -u "$COMPTE" -l 2>/dev/null | grep -vE '^[[:space:]]*(#|$)' | grep -v 'artisan schedule:run')
if [ -z "$TACHES" ]; then
    ok "crontab de $COMPTE : vide ou seulement la tâche planifiée de l'application"
else
    alerte "lignes inattendues dans la crontab de $COMPTE"
    printf '%s\n' "$TACHES" | detail 20
fi

HOME_COMPTE=$(getent passwd "$COMPTE" | cut -d: -f6)
if [ -n "$HOME_COMPTE" ] && [ -e "$HOME_COMPTE/.ssh" ]; then
    alerte "dossier $HOME_COMPTE/.ssh présent (clé SSH posée pour $COMPTE ?)"
    ls -la "$HOME_COMPTE/.ssh" 2>/dev/null | detail 20
else
    ok "pas de dossier .ssh pour $COMPTE"
fi

SHELL_COMPTE=$(getent passwd "$COMPTE" | cut -d: -f7)
case "$SHELL_COMPTE" in
    */nologin|*/false) ok "shell de $COMPTE : $SHELL_COMPTE" ;;
    *) alerte "shell de $COMPTE : $SHELL_COMPTE (un compte de service ne doit pas pouvoir se connecter)" ;;
esac

titre "7. À vérifier à la main dans l'application"
echo "             - comptes administrateurs : aucun compte inconnu"
echo "             - clés d'API : aucune clé que personne n'a créée"

titre "Bilan"
echo "  $ALERTES alerte(s), $A_LIRE point(s) à lire"
if [ "$ALERTES" -gt 0 ]; then
    echo "  Ne rien supprimer. Garder cette sortie, enquêter, et changer les secrets que $COMPTE pouvait lire."
fi
exit $(( ALERTES > 0 ? 1 : 0 ))
