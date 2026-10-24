#!/usr/bin/env bash
# Audit de durcissement d'un VPS Debian : SSH, pare-feu, Docker, ports en écoute, comptes, mises à jour.
# Lecture seule : le script ne modifie rien.
#
# Usage : sudo ./audit.sh
# Code de sortie : 0 sans alerte, 1 s'il y a au moins une alerte, 2 si le script n'a pas pu tourner.

set -u
export LC_ALL=C

ALERTES=0
A_LIRE=0

titre()  { printf '\n== %s\n' "$1"; }
ok()     { printf '  [ok]      %s\n' "$1"; }
a_lire() { printf '  [à lire]  %s\n' "$1"; A_LIRE=$((A_LIRE + 1)); }
alerte() { printf '  [alerte]  %s\n' "$1"; ALERTES=$((ALERTES + 1)); }
detail() { sed 's/^/             /' | head -n "${1:-20}"; }
existe() { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -ne 0 ]; then
    echo "À lancer avec sudo : sshd -T, /etc/shadow et les règles du pare-feu demandent root."
    exit 2
fi

# ---------------------------------------------------------------- SSH
titre "SSH"
SSHD=$(command -v sshd || echo /usr/sbin/sshd)
CONF=""
[ -x "$SSHD" ] && CONF=$("$SSHD" -T 2>/dev/null)
if [ -z "$CONF" ]; then
    a_lire "configuration effective illisible (sshd absent, ou sshd -T en erreur)"
else
    val() { printf '%s\n' "$CONF" | awk -v k="$1" '$1 == k { print $2; exit }'; }

    if [ "$(val passwordauthentication)" = "no" ]; then ok "mot de passe refusé"
    else alerte "authentification par mot de passe autorisée"; fi

    KBD=$(val kbdinteractiveauthentication)
    [ -z "$KBD" ] && KBD=$(val challengeresponseauthentication)
    if [ "$KBD" = "no" ]; then ok "clavier interactif refusé"
    else alerte "authentification au clavier interactif autorisée"; fi

    if [ "$(val pubkeyauthentication)" = "yes" ]; then ok "authentification par clé active"
    else a_lire "authentification par clé désactivée"; fi

    case "$(val permitrootlogin)" in
        no) ok "connexion directe de root refusée" ;;
        prohibit-password|without-password) a_lire "root peut se connecter par clé (permitrootlogin prohibit-password)" ;;
        *) alerte "root peut se connecter avec un mot de passe (permitrootlogin $(val permitrootlogin))" ;;
    esac

    if [ "$(val port)" = "22" ]; then a_lire "port 22 : rien de grave, mais beaucoup de bruit des robots dans les journaux"
    else ok "port non standard"; fi
fi

# Types des clés autorisées, pour chaque compte qui en a.
while IFS=: read -r COMPTE _ _ _ _ DOSSIER _; do
    F="$DOSSIER/.ssh/authorized_keys"
    [ -f "$F" ] || continue
    TYPES=$(grep -vE '^[[:space:]]*(#|$)' "$F" \
        | grep -oE 'sk-ssh-ed25519@openssh\.com|ssh-ed25519|ssh-rsa|ssh-dss|sk-ecdsa-sha2-nistp256@openssh\.com|ecdsa-sha2-nistp(256|384|521)' \
        | sort | uniq -c | awk '{ printf "%s%s x%s", (NR > 1 ? ", " : ""), $2, $1 }')
    if [ -z "$TYPES" ]; then
        a_lire "$COMPTE : $F existe mais aucune clé reconnue"
    elif printf '%s' "$TYPES" | grep -q 'ssh-dss'; then
        alerte "$COMPTE : clé DSA, obsolète ($TYPES)"
    elif printf '%s' "$TYPES" | grep -qE 'ssh-rsa|ecdsa'; then
        a_lire "$COMPTE : autre chose que Ed25519 ($TYPES)"
    else
        ok "$COMPTE : $TYPES"
    fi
done < <(getent passwd)

# ---------------------------------------------------------------- Pare-feu
titre "Pare-feu"
if existe ufw; then
    STATUT=$(ufw status verbose 2>/dev/null)
    if printf '%s\n' "$STATUT" | grep -q '^Status: active'; then ok "UFW actif"
    else alerte "UFW inactif"; fi
    if printf '%s\n' "$STATUT" | grep -qE 'Default: (deny|reject) \(incoming\)'; then ok "refus par défaut en entrée"
    else alerte "UFW n'est pas en refus par défaut en entrée"; fi
else
    a_lire "UFW absent : vérifier quel pare-feu est utilisé"
fi

if existe docker; then
    if iptables -S DOCKER-USER >/dev/null 2>&1; then
        REGLES=$(iptables -S DOCKER-USER | grep '^-A DOCKER-USER' | grep -vx -- '-A DOCKER-USER -j RETURN')
        if [ -z "$REGLES" ]; then
            alerte "DOCKER-USER vide : les ports publiés par Docker ne passent pas par le pare-feu"
        elif ! printf '%s\n' "$REGLES" | grep -qE -- '-j (DROP|REJECT|ufw-docker-logging-deny)'; then
            # Des RETURN ou des sauts vers ufw-user-forward ne suffisent pas : un paquet qui sort de la chaîne
            # sans verdict est accepté par les chaînes de Docker.
            alerte "DOCKER-USER ne jette rien ($(printf '%s\n' "$REGLES" | wc -l) règle(s), aucune en DROP ou REJECT) : les ports publiés restent joignables"
            printf '%s\n' "$REGLES" | detail 20
        else
            ok "$(printf '%s\n' "$REGLES" | wc -l) règle(s) dans DOCKER-USER, dont au moins une qui jette"
            # Une règle qui jette un port précis avec --dport vise souvent le port publié, qu'elle ne verra jamais :
            # la redirection de Docker l'a déjà remplacé. Les plages de ufw-docker (0:32767, 1024:65535) sont voulues.
            if printf '%s\n' "$REGLES" | grep -E -- '-j (DROP|REJECT|ufw-docker-logging-deny)' | grep -qE -- '--dport [0-9]+( |$)'; then
                a_lire "DOCKER-USER jette un port précis avec --dport : après la redirection de Docker, c'est le port du conteneur, pas le port publié (voir --ctorigdstport)"
            fi
        fi
    else
        a_lire "pas de chaîne DOCKER-USER (Docker arrêté, ou sans iptables)"
    fi

    PUBLIES=$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null | awk -F'|' '{
        n = split($2, m, ", "); out = ""
        for (i = 1; i <= n; i++) if (m[i] ~ /->/ && m[i] !~ /^127\./ && m[i] !~ /^\[::1\]/) out = out m[i] " "
        if (out != "") print $1 " : " out }')
    if [ -z "$PUBLIES" ]; then
        ok "aucun port de conteneur publié hors de la machine"
    else
        a_lire "ports publiés par des conteneurs : chacun doit être voulu et filtré dans DOCKER-USER"
        printf '%s\n' "$PUBLIES" | detail 30
    fi
fi

# ---------------------------------------------------------------- Écoute
titre "Ports en écoute hors de la machine"
EXPOSES=$(ss -H -tulpn 2>/dev/null | awk '$5 !~ /^(127\.|\[::1\])/ { print $1 " " $5 " " $7 }')
if [ -z "$EXPOSES" ]; then
    ok "rien n'écoute ailleurs que sur la machine elle-même"
else
    a_lire "à comparer avec la liste de ce qui doit être joignable"
    printf '%s\n' "$EXPOSES" | detail 40
fi

BASE=$(ss -H -tulpn 2>/dev/null | awk '$1 == "tcp" && $5 ~ /:3306$/ { print $5 }')
if [ -z "$BASE" ]; then
    ok "MariaDB/MySQL n'écoute pas en TCP sur le port 3306"
else
    for ADR in $BASE; do
        case "$ADR" in
            127.*|\[::1\]*) ok "MariaDB/MySQL sur $ADR (machine seulement)" ;;
            10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) a_lire "MariaDB/MySQL sur $ADR : réseau privé (pont Docker ?), à justifier" ;;
            *) alerte "MariaDB/MySQL sur $ADR : joignable hors de la machine" ;;
        esac
    done
fi

# ---------------------------------------------------------------- fail2ban
titre "fail2ban"
if existe fail2ban-client; then
    if systemctl is-active --quiet fail2ban; then ok "fail2ban actif"
    else alerte "fail2ban installé mais arrêté"; fi
    PRISONS=$(fail2ban-client status 2>/dev/null | sed -n 's/.*Jail list:[[:space:]]*//p')
    if [ -n "$PRISONS" ]; then ok "prisons : $PRISONS"
    else a_lire "aucune prison active"; fi
else
    a_lire "fail2ban absent"
fi

# ---------------------------------------------------------------- Comptes
titre "Comptes"
UID0=$(awk -F: '$3 == 0 && $1 != "root" { print $1 }' /etc/passwd)
if [ -z "$UID0" ]; then ok "root est le seul compte UID 0"
else alerte "autres comptes UID 0 : $(printf '%s ' $UID0)"; fi

VIDES=$(awk -F: '$2 == "" { print $1 }' /etc/shadow)
if [ -z "$VIDES" ]; then ok "aucun mot de passe vide"
else alerte "comptes sans mot de passe : $(printf '%s ' $VIDES)"; fi

SYSTEME=$(awk -F: '$3 > 0 && $3 < 1000 && $7 !~ /(nologin|false|sync|halt|shutdown)$/ { print $1 " (" $7 ")" }' /etc/passwd)
if [ -z "$SYSTEME" ]; then ok "aucun compte de service avec un vrai shell"
else a_lire "comptes de service avec un vrai shell :"; printf '%s\n' "$SYSTEME" | detail 20; fi

# ---------------------------------------------------------------- Mises à jour
titre "Mises à jour"
if dpkg -s unattended-upgrades >/dev/null 2>&1; then
    if apt-config dump 2>/dev/null | grep -q '^APT::Periodic::Unattended-Upgrade "1"'; then ok "mises à jour automatiques actives"
    else alerte "unattended-upgrades installé mais pas activé"; fi
else
    alerte "unattended-upgrades absent : les correctifs de sécurité ne s'installent pas seuls"
fi

ATTENTE=$(apt list --upgradable 2>/dev/null | grep -c '/')
SECU=$(apt list --upgradable 2>/dev/null | grep -c -- '-security')
if [ "$SECU" -gt 0 ]; then alerte "$SECU correctif(s) de sécurité en attente (d'après le dernier apt update)"
elif [ "$ATTENTE" -gt 0 ]; then a_lire "$ATTENTE paquet(s) en attente (d'après le dernier apt update)"
else ok "aucun paquet en attente (d'après le dernier apt update)"; fi

if [ -f /var/run/reboot-required ]; then
    a_lire "redémarrage demandé par : $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"
else
    ok "aucun redémarrage demandé"
fi

# ---------------------------------------------------------------- Système
titre "Système"
if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then ok "swap actif"
else a_lire "aucun swap actif"; fi

titre "Bilan"
echo "  $ALERTES alerte(s), $A_LIRE point(s) à lire"
exit $(( ALERTES > 0 ? 1 : 0 ))
