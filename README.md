# Administration du VPS d'un serveur minecraft

J'administre le VPS Debian 12 d'un serveur minecraft communautaire que j'ai monté avec un ami. Il fait tourner le panel Pterodactyl, qui lance les serveurs minecraft dans des conteneurs Docker, un site Next.js avec des comptes joueurs derrière nginx, et une base MariaDB. C'était le premier serveur que je configurais, et j'ai appris en le faisant.

Ce dépôt rassemble des interventions réelles sur cette machine, avec les commandes et les sorties d'outils. Les noms, adresses, ports et chemins qui permettraient de l'identifier ou de l'attaquer ont été retirés.

## Les interventions

- [PHP2Shell](php2shell/) : la mise à jour du panel, exposé à deux failles critiques de PHP, puis la recherche de traces d'intrusion pendant la journée d'exposition.
- [Les règles DOCKER-USER qui ne bloquaient rien](docker-ufw/) : des règles censées empêcher Docker de contourner le pare-feu, relues ligne par ligne, corrigées et vérifiées depuis l'extérieur.
- [Entretenir la machine](durcissement/) : la base de la sécurité, un site sous un compte de service, des certificats derrière un anti-DDoS, une mise à jour de Docker et une alerte de base de données à 6 h du matin, avec un script d'audit.
- [Réduire la hauteur du monde](hauteur-du-monde/) : la conversion de 1,4 million de chunks sans perte, après une corruption silencieuse repérée dans le code du jeu.
## Les scripts

Je travaille avec Claude Code. Les scripts de ce dépôt ont été écrits avec lui, et je n'aurais pas su écrire seul certains d'entre eux, comme celui de la conversion des chunks. Les décisions, l'exécution sur la machine et les vérifications sont les miennes.
