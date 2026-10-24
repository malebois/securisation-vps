# Entretenir le VPS

Le VPS tourne sous Debian 12 et fait tourner le panel Pterodactyl, qui lance les serveurs minecraft dans des conteneurs Docker, un site Next.js avec des comptes joueurs derrière nginx, et une base MariaDB partagée par le serveur minecraft et le site. Les performances du serveur minecraft passent avant tout, donc tout ce que j'ajoute sur la machine doit coûter presque rien en processeur et en mémoire.

---

## La base

J'ai mis en place la sécurité de base quand j'ai migré le serveur de notre ancien hébergeur minecraft vers ce VPS. C'était la première fois que je configurais un serveur. Je connaissais iptables et UFW par TryHackMe mais je ne les avais jamais installés moi-même, et je ne savais pas ce qu'était vraiment Docker. Je ne saurais pas retaper toutes ces commandes de mémoire aujourd'hui, mais je sais dans quel ordre faire les choses et ce qu'il faut vérifier à chaque étape.

Le VPS n'accepte en SSH que des clés Ed25519, sur un port non standard. UFW refuse tout en entrée sauf les ports utiles, fail2ban tourne, et MariaDB n'écoute que sur la machine. Pendant chaque modification de sshd, d'UFW ou de fail2ban, je gardais une session SSH ouverte à côté et je testais une nouvelle connexion avant de fermer l'ancienne, et je ne me suis jamais retrouvé enfermé dehors.

Pour vérifier que tout ça marchait vraiment, je me suis connecté depuis un VPN avec une autre adresse que la mienne. Sans la bonne clé, je n'obtenais jamais d'invite de mot de passe, et en multipliant les échecs je me faisais bannir par fail2ban, ce qui m'a permis de voir le ban s'appliquer et de mesurer sa durée.

Comme Docker contourne UFW pour les ports qu'il publie, j'avais aussi posé des règles dans la chaîne `DOCKER-USER`. En les relisant en octobre, j'ai vu qu'elles ne jetaient rien, et je les ai corrigées. C'est raconté dans [la partie DOCKER-USER](../docker-ufw/).

---

## Le site sous un compte de service

Quand j'ai mis le site en ligne en septembre, je l'ai installé sous un compte système à lui, sans mot de passe, sans shell et sans `sudo`.

```bash
sudo adduser --system --group --home /srv/site --shell /usr/sbin/nologin site
```

L'application n'écoute que sur `127.0.0.1:3000`, et seul nginx est joignable depuis internet. Le VPS récupère le code avec sa propre clé, ajoutée sur GitHub comme deploy key sans droit d'écriture. Si le site était compromis, l'attaquant se retrouverait dans un compte qui ne peut ni se connecter, ni modifier le dépôt, ni toucher au reste de la machine.

```bash
sudo -u site mkdir -m 700 /srv/site/.ssh
sudo -u site ssh-keygen -t ed25519 -N "" -f /srv/site/.ssh/id_ed25519 -C "site@vps"
```

---

## Les certificats derrière l'anti-DDoS

L'anti-DDoS de l'hébergeur intercepte tout le trafic du port 80 et répond avec sa propre page de vérification, donc la validation HTTP de Let's Encrypt (HTTP-01), qui sert un fichier sur ce port, ne peut pas aboutir. Les certificats passent par la validation DNS (DNS-01) : certbot crée un enregistrement TXT temporaire dans la zone par l'API du registrar, puis l'efface, et un `--deploy-hook` recharge nginx à chaque renouvellement. La clé d'API est dans un fichier lisible par root seul (`chmod 600`).

En mettant ça en place, je me suis rendu compte que le certificat du panel avait été obtenu en août par la méthode HTTP, que l'anti-DDoS bloque désormais. Il fonctionnait encore, mais son renouvellement automatique aurait échoué à l'expiration. Je l'ai refait par DNS avec `--force-renewal`, et `certbot renew --dry-run` simule maintenant le renouvellement de tous les certificats de la machine.

---

## Une mise à jour de Docker

Les paquets Debian s'installent seuls chaque matin entre 6 h et 7 h, hors des heures d'ouverture. Docker et PHP viennent de dépôts tiers et je les mets à jour à la main, parce que leur mise à jour redémarre des services dont dépend le serveur minecraft.

Le 16 octobre, `apt list --upgradable` proposait 19 paquets, dont Docker, containerd et PHP-FPM. Docker redémarre pendant sa mise à jour et ne laisse qu'une dizaine de secondes aux conteneurs pour s'arrêter, donc le serveur minecraft doit être arrêté proprement depuis le panel avant. Les règles de `DOCKER-USER` ne devaient pas non plus disparaître, donc j'en ai pris une photo avant pour la comparer après :

```bash
sudo iptables -S DOCKER-USER > ~/docker-user-avant.txt
sudo apt upgrade
sudo iptables -S DOCKER-USER | diff ~/docker-user-avant.txt -
systemctl is-active docker wings
ls /var/run/reboot-required
```

Le `diff` n'a rien affiché, Docker et Wings étaient actifs et aucun redémarrage n'était demandé. La version de PHP installée ce jour-là était encore vulnérable à PHP2Shell, publiée cinq jours plus tard, ce que raconte [la partie PHP2Shell](../php2shell/).

---

## MariaDB qui redémarre toute seule

Le même matin, j'ai trouvé cette alerte dans la console du serveur minecraft, à 6 h 12 :

```
[06:12:30 WARN]: [<plugin>.libs.hikari.pool.PoolBase] <pool> - Failed to validate connection <plugin>.libs.mariadb.Connection@60e5361e ((conn=19457) Connection.setNetworkTimeout cannot be called on a closed connection). Possibly consider using a shorter maxLifetime value.
```

HikariCP est le pool de connexions du plugin, qui garde des connexions ouvertes vers la base et les vérifie avant de les prêter. Une de ses connexions avait été fermée côté base, et la suggestion de baisser `maxLifetime` laissait penser à une connexion restée inactive trop longtemps. J'ai voulu lire le journal de MariaDB autour de 6 h, et `journalctl` a refusé ma syntaxe :

```
$ journalctl -u mariadb --since "today 05:45" --until "today 06:30"
Failed to parse timestamp: today 05:45
```

`journalctl` accepte `--since "05:45"` ou une date complète, mais pas `today` suivi d'une heure. L'historique d'apt a répondu plus vite :

```
$ grep -i mariadb /var/log/apt/history.log
Upgrade: mariadb-server:amd64 (...), mariadb-client:amd64 (...), libmariadb3:amd64 (...), [...]
```

MariaDB avait été mise à jour ce matin-là, ce qui la redémarre. Pour le confirmer, j'ai demandé son uptime. La première tentative a échoué parce que mon compte n'a pas d'utilisateur dans la base, ce qui est voulu :

```
$ mariadb -e "SHOW GLOBAL STATUS LIKE 'Uptime'; SHOW GLOBAL VARIABLES LIKE 'wait_timeout';"
ERROR 1045 (28000): Access denied for user '<moi>'@'localhost' (using password: NO)
$ sudo mariadb -e "SHOW GLOBAL STATUS LIKE 'Uptime'; SHOW GLOBAL VARIABLES LIKE 'wait_timeout';"
+---------------+-------+
| Variable_name | Value |
+---------------+-------+
| Uptime        | 14296 |
+---------------+-------+
+---------------+-------+
| Variable_name | Value |
+---------------+-------+
| wait_timeout  | 28800 |
+---------------+-------+
```

14 296 secondes font un peu moins de 4 heures, ce qui place le démarrage vers 6 h 12, à la minute de l'alerte. Avec un `wait_timeout` à sa valeur par défaut de 8 heures, la piste de la connexion inactive ne tenait pas, et c'est le redémarrage qui avait coupé la connexion. Le pool en a ouvert une nouvelle et aucune erreur n'a suivi. Il me reste à vérifier dans la ligne `Commandline:` de `history.log` que c'est bien `unattended-upgrades` qui a lancé cette mise à jour.

---

## Deux scripts d'audit

[`audit.sh`](audit.sh) vérifie en lecture seule SSH, UFW, `DOCKER-USER`, les ports en écoute, MariaDB, fail2ban, les comptes, les mises à jour et le swap, et classe chaque point en `[ok]`, `[à lire]` ou `[alerte]`. [`controle-maj.sh`](controle-maj.sh) automatise la photo d'avant et d'après une mise à jour de Docker.

```bash
sudo ./audit.sh
sudo ./controle-maj.sh avant
sudo apt upgrade
sudo ./controle-maj.sh apres
```

La première version d'`audit.sh` aurait marqué ma chaîne `DOCKER-USER` d'avant la correction comme correcte, parce qu'elle comptait seulement les règles. Le script vérifie maintenant que la chaîne contient au moins un `DROP` ou un `REJECT`. Je l'ai lancé juste après la correction, et voici un extrait de la sortie, sans les noms de comptes ni la liste des ports :

```
== SSH
  [ok]      mot de passe refusé
  [ok]      clavier interactif refusé
  [ok]      authentification par clé active
  [ok]      connexion directe de root refusée
  [ok]      port non standard
  [ok]      <compte> : ssh-ed25519 x1
  [...]

== Pare-feu
  [ok]      UFW actif
  [ok]      refus par défaut en entrée
  [ok]      11 règle(s) dans DOCKER-USER, dont au moins une qui jette
  [à lire]  DOCKER-USER filtre sur --dport : après la redirection de Docker, c'est le port du conteneur, pas le port publié (voir --ctorigdstport)
  [à lire]  ports publiés par des conteneurs : chacun doit être voulu et filtré dans DOCKER-USER
  [...]

== Bilan
  0 alerte(s), 4 point(s) à lire
```

Le premier point à lire était une fausse alerte du script. Il signalait toute règle écrite avec `--dport`, alors que les plages du bloc ufw-docker (`0:32767`, `1024:65535`) visent justement les ports des conteneurs après la redirection. Il ne signale plus qu'une règle qui jette un port précis, comme `--dport 8080 -j DROP`. Les autres points étaient attendus : les ports publiés sont ceux du jeu, du textures pack et du vocal, et MariaDB écoute aussi sur l'adresse du réseau interne de Pterodactyl, par lequel le serveur minecraft joint la base, UFW n'autorisant ce port que depuis les conteneurs.

---

L'alerte de 6 h 12 et le certificat du panel venaient tous les deux de mécanismes qui tournent seuls. Le redémarrage de MariaDB n'a eu aucune conséquence, alors que le renouvellement du certificat aurait échoué sans bruit le jour de l'expiration.
