# Réduire la hauteur d'un monde minecraft sans perdre un bloc

Le monde de notre serveur minecraft utilise Higher Heights, un datapack public qui étend la hauteur à 4 064 blocs, de y -2032 à 2031, alors que la carte jouée ne sort jamais d'une plage qui va à peu près de -300 à 650. Tout en bas, sur le sol plat que le jeu génère à -2032, se trouve la zone du staff, qu'on appelle le hors-map, avec des constructions de plusieurs centaines de blocs. Un chunk est une colonne de 16 × 16 blocs sur toute la hauteur du monde, découpée en sections de 16 blocs, et le serveur garde en mémoire toutes les sections de chaque chunk chargé, même vides. La carte consommait énormément de mémoire, et je voulais réduire la hauteur sans rien perdre, sachant que casser la carte était la pire chose qui pouvait arriver au projet.

---

## La première idée

La première idée était de garder la plage -320 à 703 et de laisser disparaître le reste, le sol plat du hors-map se régénérant plus haut. Ça réglait la carte, mais les constructions du staff étaient justement dans la partie coupée. Les sauvegarder une par une en schematics avec WorldEdit pour les recoller ensuite aurait pris un temps énorme, avec un vrai risque d'en oublier, donc j'ai refusé. Je suis parti sur un script qui remonte le hors-map directement dans les fichiers du monde, sans passer par le jeu.

Avant de commencer, j'ai aussi vérifié qu'il serait possible de remonter le plafond plus tard. Il suffit pour ça de changer `height` et `logical_height` dans le datapack, alors que baisser le plafond ou descendre le fond demanderait une nouvelle conversion.

---

## Pourquoi changer le datapack ne suffit pas

Le plus simple aurait été de modifier `min_y` et `height` dans le `dimension_type` du datapack et de redémarrer. Le code de Paper (`SerializableChunkData`, `Heightmap.setRawData`) montre pourquoi c'était un piège. Chaque chunk enregistre des heightmaps, qui donnent pour chacune de ses 256 colonnes la hauteur du bloc le plus haut, empaquetées dans des entiers de 64 bits. Il faut 12 bits par valeur pour 4 064 blocs et 11 pour la nouvelle hauteur, mais dans les deux cas 5 valeurs tiennent dans un entier et il faut 52 entiers par heightmap. Le jeu vérifie seulement la longueur du tableau, qui ne change pas, et relit donc des valeurs de 12 bits comme si elles en faisaient 11, sans rien signaler.

Je l'ai vérifié sur un serveur Paper de test, en local sur mon PC, avec un petit monde construit pour l'occasion. Avec le datapack changé seul, le journal affichait 180 lignes d'alerte, dont un `Failed to parse light data` suivi d'une `ArrayIndexOutOfBoundsException` pour chaque chunk, les heightmaps plaçaient la surface au mauvais endroit et le hors-map avait disparu. Passé par le script, le même monde donnait 19 contrôles justes sur 19, de la lumière des torches au contenu des coffres, sur une version récente de Paper comme sur la nôtre.

---

## La copie

Rien ne devait toucher au monde d'origine, donc j'ai commencé par une copie complète sur le VPS, serveur arrêté. Dans les commandes, `$SRV` désigne le dossier du serveur.

```
$ sudo cp -a "$SRV/world" /srv/sauvegardes/world-avant-hauteur
$ sudo du -s --apparent-size "$SRV/world" /srv/sauvegardes/world-avant-hauteur
6066855 .../world
6066807 /srv/sauvegardes/world-avant-hauteur
$ sudo sh -c "find $SRV/world -type f | wc -l; find /srv/sauvegardes/world-avant-hauteur -type f | wc -l"
3124
3124
```

Les deux dossiers avaient le même nombre de fichiers, mais 48 Ko d'écart. `du` compte aussi la taille des dossiers eux-mêmes, et un dossier qui a beaucoup servi reste plus gros que sa copie neuve, ce qui pouvait l'expliquer. Pour en être sûr, j'ai comparé le contenu fichier par fichier :

```
$ sudo diff -rq "$SRV/world" /srv/sauvegardes/world-avant-hauteur && echo "COPIE IDENTIQUE"
COPIE IDENTIQUE
```

---

## La mesure

J'ai copié le script sur le VPS avec `scp` depuis WSL, en vérifiant à chaque nouvelle version son empreinte avec `sha256sum`. La première étape ne fait que lire la copie et rendre un rapport.

```
$ sudo python3 convertir_hauteur.py mesure /srv/sauvegardes/world-avant-hauteur --processus=8
Mesure de 1490 fichiers de région dans /srv/sauvegardes/world-avant-hauteur/dimensions/minecraft/overworld (8 processus, lecture seule).
  [...]
CHUNKS : 1412545 au total
  carte       1244526   (dont 480145 pas finis de générer)
  hors-map     102467   (dont 12279 pas finis de générer)
  vide          64127   (dont 60618 pas finis de générer)
  haut              0   (dont 0 pas finis de générer)
  mixte          1425   (dont 0 pas finis de générer)
  trop-haut         0   (dont 0 pas finis de générer)
  yPos lus : {-127: 912412, -16: 500133}

CARTE : du contenu de la tranche -16 à la tranche 41, soit entre y = -256 et y = 671 (à la tranche de 16 près).
```

Le rapport comptait 1 425 chunks mixtes, où le hors-map et la carte se partagent la même colonne, parce qu'à un endroit le bord de la carte est posé sur le sol plat du hors-map et qu'à un autre des constructions du staff sont sous la carte. La première version du script refusait ces colonnes, et je l'ai fait reprendre pour qu'elle laisse la carte en place et remonte le hors-map en dessous.

La ligne `yPos lus` donne la section de départ de chaque chunk. 912 412 chunks partent bien de -127, soit y -2032, mais 500 133 partent de -16, soit y -256, donc la hauteur du monde avait déjà été changée une fois dans le passé, sans conversion.

La deuxième mesure, avec la version corrigée, a rendu la plage de départ impossible :

```
FOND
  Haut du hors-map : y -1473 (tranche -93). Bas de la carte : y -288 (tranche -18). Haut de la carte : y 671.
  Le plus haut possible, sans aucun chevauchement : --fond=-464 (73 tranches de haut, contre 254 aujourd'hui).
  Conseillé, avec 64 blocs de vide entre le haut du hors-map et le bas de la carte : --fond=-912 (101 tranches de haut, contre 254 aujourd'hui).

CHUNKS avec --fond=-320 : 1412545 au total
  [...]
  mixte               910   (dont 0 pas finis de générer)
  chevauchement       515   (dont 0 pas finis de générer)
```

Avec un fond à -320, le hors-map remonté serait entré dans la carte sur 515 chunks.

---

## Le choix du fond

Le rapport conseillait -912, qui gardait toutes les constructions du staff invisibles sous la carte. Mes tests de charge montraient que les chunks étaient ce qui coûtait le plus au serveur, et j'avais peur qu'avec 101 sections au lieu de 254 le gain ne suffise pas. J'ai choisi -464, le plus haut possible sans chevauchement, avec 73 sections, en acceptant que les plus hautes constructions du hors-map arrivent vers y 95 et puissent se voir depuis un bord de la carte. Le staff les déplacera.

```
CHUNKS avec --fond=-464 : 1412545 au total
CONFLITS avec --fond=-464 (la conversion refusera de tourner tant qu'il en reste) : 0
JOUEURS DÉCONNECTÉS sous y -320 ou hors de la plage : 1
ERREURS DE LECTURE : 0
```

Il restait au moins 16 blocs de vide entre le hors-map remonté et la carte dans la colonne la plus serrée, et le même essai sur le serveur de test, refait à -464 avec des colonnes mixtes, a donné 27 contrôles justes sur 27.

---

## La conversion à blanc

Le script écrit un monde converti dans un dossier de travail, puis relit chaque section pour la comparer à l'original, octet pour octet pour la carte et décalée pour le hors-map. Sur le VPS, il a terminé en 474 secondes sur `VÉRIFICATION : aucun écart`.

C'est là que j'ai douté. Le rapport donnait 41,5 Ko de données par chunk sur le disque au lieu de 55,3, soit seulement 25 % de moins. J'ai donc mesuré la mémoire sur le serveur de test, avec 1 536 chunks chargés, après un garbage collection complet. Une section vide tient en quelques octets sur le disque, mais une fois chargée elle devient une structure complète en mémoire, avec ses blocs, ses biomes et sa lumière.

| Mémoire par chunk chargé | Avant (-2032 à 2031) | Après (-464 à 703) |
|---|---|---|
| Sections par chunk | 254 | 73 |
| Mémoire occupée | 645,5 Ko | 208,6 Ko (-68 %) |

Avant la bascule, un `grep` sur les fichiers des plugins a cherché les coordonnées sorties de la nouvelle plage. Il a remonté surtout des coordonnées de modèles 3D sans rapport, et deux vrais points de téléportation du staff à y -2028, que j'ai passés à -460. Un monde secondaire non converti avait aussi son point d'apparition sous le nouveau fond, donc je l'ai retiré du chargement automatique sans y toucher.

---

## La bascule

L'échange se fait par des `mv`, sans rien effacer, et le retour arrière consiste à remettre les anciens noms.

```bash
W="$SRV/world"
# l'ancien overworld part de côté, le converti prend sa place
sudo mv "$W/dimensions/minecraft/overworld" /srv/travail/overworld-ancien
sudo mv /srv/travail/hauteur-464/overworld "$W/dimensions/minecraft/overworld"
# le datapack modifié garde le même nom de fichier, pour que le monde le garde activé
sudo mv "$W/datapacks/HigherHeights.zip" /srv/travail/HigherHeights-ancien.zip
sudo mv /srv/travail/HigherHeights.zip "$W/datapacks/HigherHeights.zip"
# le joueur déconnecté dans le hors-map, remonté avec lui
sudo cp /srv/travail/hauteur-464/joueurs/<uuid>.dat "$W/players/data/"
# les deux points de téléportation, après une copie du fichier
sudo cp "$WARPS" /srv/travail/warps-ancien.yml
sudo sed -i '434s/y: -2028.0/y: -460.0/; 455s/y: -2028.0/y: -460.0/' "$WARPS"
# tout reprend le propriétaire du serveur
sudo chown -R pterodactyl:pterodactyl "$W/dimensions/minecraft/overworld" "$W/datapacks/HigherHeights.zip" "$W/players/data/<uuid>.dat" "$WARPS"
```

La sortie de mon `ls` de contrôle était coupée et je n'étais pas sûr que ça avait marché, donc j'ai relu le datapack en place et compté les fichiers de région :

```
$ sudo python3 -c "import zipfile,json,sys; z=zipfile.ZipFile(sys.argv[1]); d=json.loads(z.read('data/minecraft/dimension_type/overworld.json')); print(d['min_y'], d['height'])" "$W/datapacks/HigherHeights.zip"
-464 1168
$ sudo sh -c "ls $W/dimensions/minecraft/overworld/region | wc -l"
1490
```

Au démarrage, la seule erreur du journal était celle, déjà connue, d'un jar de plugin invalide :

```bash
sudo zgrep -nE "ERROR|Exception|Failed to parse|corrupt|Ignoring heightmap" "$LAST" | head -30
```

Ça ne disait encore rien des chunks que personne n'avait chargés. Avant d'aller dormir, j'ai lancé le pré-calcul d'un plugin qui prépare les niveaux de détail du mod Distant Horizons, et qui fait charger par Paper chaque chunk existant, ce qui en faisait un contrôle complet du monde converti.

```
[09:22:17 INFO]: Generation progress: 100.00%
[09:22:17 INFO]: Processed LODs: 169745 / 169744 (270.54 CPS)
[09:22:17 INFO]: Time elapsed: 2 hours, 47 minutes, 19 seconds
[09:22:17 INFO]: Generation is complete.
```

Le même `zgrep` sur les journaux de la nuit n'a rien renvoyé, et j'ai validé en jeu le 12 octobre. J'ai aussi trouvé quelques fichiers de région autour de x = 10 000 000, très loin de la carte, que je n'ai pas encore examinés. La copie d'avant la migration est toujours sur le VPS, et je la supprimerai à la main.

---

Ce qui ressemblait à deux valeurs à changer dans un fichier JSON aurait faussé les heightmaps et la lumière de tout le monde, et la première solution aurait effacé les constructions du staff. La mesure a en plus montré que le plan de départ était impossible et que le monde avait déjà subi un changement de hauteur sans conversion. Sur le disque, le gain n'était que de 25 %, et c'est la mesure de la mémoire, faite parce que ce chiffre m'inquiétait, qui a montré les 68 %.

## Le script

Les deux fichiers de cette partie sont le script tel qu'il a tourné, en Python 3.11 sans dépendance. `nbt_region.py` lit et réécrit à l'identique le format NBT et les fichiers de région de minecraft.

```bash
python3 convertir_hauteur.py mesure    <monde> [--fond=Y]          # lecture seule : rapport et fond conseillé
python3 convertir_hauteur.py convertir <monde> <sortie> --fond=Y   # monde converti dans un nouveau dossier, vérifié
python3 convertir_hauteur.py datapack  <zip> <zip neuf> --fond=Y   # le datapack à la même hauteur
```
