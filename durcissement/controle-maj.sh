#!/usr/bin/env bash
# Photographie avant une mise à jour manuelle, comparaison après.
# Compare les règles DOCKER-USER (une mise à jour de Docker ne doit pas les perdre), vérifie que les
# services tournent toujours et liste les paquets mis à jour.
#
# Usage : sudo ./controle-maj.sh avant     (avant apt upgrade)
#         sudo ./controle-maj.sh apres     (après apt upgrade)
# Services suivis : SERVICES="docker nginx mariadb fail2ban" par défaut, plus PHP-FPM et wings s'ils existent.

set -u
export LC_ALL=C

DOSSIER="${CONTROLE_MAJ_DOSSIER:-/root/controle-maj}"
SERVICES="${SERVICES:-docker nginx mariadb fail2ban}"

if [ "$(id -u)" -ne 0 ]; then
    echo "À lancer avec sudo."
    exit 2
fi

services() {
    for S in $SERVICES; do echo "$S"; done
    systemctl list-unit-files --no-legend 'php*-fpm.service' 'wings.service' 2>/dev/null \
        | awk '{ print $1 }' | sed 's/\.service$//'
}

photo() {
    iptables -S DOCKER-USER > "$1.iptables" 2>&1
    ip6tables -S DOCKER-USER > "$1.ip6tables" 2>&1
    for S in $(services | sort -u); do
        printf '%s %s\n' "$S" "$(systemctl is-active "$S" 2>/dev/null)"
    done > "$1.services"
    dpkg-query -W -f='${Package} ${Version}\n' > "$1.paquets"
}

case "${1:-}" in
    avant)
        mkdir -p "$DOSSIER" && chmod 700 "$DOSSIER"
        photo "$DOSSIER/avant"
        echo "Photo prise dans $DOSSIER : règles DOCKER-USER, état des services, versions des paquets."
        ;;
    apres)
        if [ ! -f "$DOSSIER/avant.services" ]; then
            echo "Pas de photo « avant » dans $DOSSIER : lancer d'abord ./controle-maj.sh avant"
            exit 2
        fi
        photo "$DOSSIER/apres"
        ALERTES=0

        for F in iptables ip6tables; do
            if diff -q "$DOSSIER/avant.$F" "$DOSSIER/apres.$F" >/dev/null; then
                echo "[ok]      règles DOCKER-USER ($F) identiques"
            else
                echo "[alerte]  règles DOCKER-USER ($F) changées :"
                diff -u "$DOSSIER/avant.$F" "$DOSSIER/apres.$F" | tail -n +3 | sed 's/^/             /'
                ALERTES=$((ALERTES + 1))
            fi
        done

        while read -r S ETAT; do
            AVANT=$(awk -v s="$S" '$1 == s { print $2 }' "$DOSSIER/avant.services")
            if [ "$ETAT" = "active" ]; then
                echo "[ok]      $S actif"
            elif [ "$AVANT" = "active" ]; then
                echo "[alerte]  $S était actif avant, maintenant : $ETAT"
                ALERTES=$((ALERTES + 1))
            else
                echo "[à lire]  $S : $ETAT (déjà $AVANT avant)"
            fi
        done < "$DOSSIER/apres.services"

        echo
        echo "Paquets mis à jour :"
        diff "$DOSSIER/avant.paquets" "$DOSSIER/apres.paquets" | sed -n 's/^> /             /p'

        if [ -f /var/run/reboot-required ]; then
            echo "[à lire]  redémarrage demandé par : $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"
        fi
        exit $(( ALERTES > 0 ? 1 : 0 ))
        ;;
    *)
        echo "Usage : sudo $0 avant | apres"
        exit 2
        ;;
esac
