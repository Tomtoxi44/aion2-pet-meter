# AION2 Pet Meter

Petite appli Windows (overlay) qui affiche tes âmes de familier quand tu tues un monstre dans **Aion 2**,
et les envoie automatiquement à **[Aion 2 Todo](https://aion2-todo-7275.azurewebsites.net)** (onglet Familiers).

Basée sur [AION2-pet-tracker](https://github.com/elkaida/AION2-pet-tracker) (MIT, voir `THIRD_PARTY_NOTICES.md`).

## Installation

1. Installe **[Npcap](https://npcap.com/#download)** (gratuit ; déjà présent si tu utilises un DPS meter).
2. Télécharge `aion2-pet-meter.exe` depuis Aion 2 Todo (onglet Familiers → Pet Meter) et mets-le dans son propre dossier.
   Windows peut afficher « éditeur inconnu » : *Informations complémentaires → Exécuter quand même*.
3. Sur Aion 2 Todo, onglet Familiers → Pet Meter → **Créer ma clé**, puis dans l'overlay : menu **⋯ → Clé de synchro Aion 2 Todo…** et colle la clé.
4. Lance le Pet Meter **avant d'entrer en jeu** avec ton perso (c'est là que le jeu envoie toute ta collection).
   Le jeu doit être en **plein écran fenêtré** ou fenêtré pour voir l'overlay.

## Ce qu'il fait

- Pour le monstre que tu viens de tuer : ses 3 niveaux, combien de kills il reste, **MAX +N** si le familier est déjà au max.
- Synchro : quelques secondes après une série de kills, l'état complet de ta collection est envoyé à Aion 2 Todo
  (clé personnelle, en HTTPS). Le pied de l'overlay indique l'heure de la dernière synchro.
- **Masquage auto** (actif par défaut) : sans butin de familier pendant 5 s, l'overlay se cache ; il revient au survol
  de la souris ou au prochain butin. **⋯ → Masquage auto** : le couper, ou choisir le délai (3, 5, 10, 30 ou 60 s).
  Un problème (Npcap absent, lecture en échec) reste affiché.
- **Kinah du jour** (masqué par défaut, **⋯ → Kinah du jour → Afficher**) : les Kinah gagnés sur les monstres depuis le
  reset (07:00 UTC), par perso, face au plafond quotidien (1 M par défaut, réglable dans le même menu). Seuls les gains qui suivent un kill comptent
  (pas les ventes ni les récompenses de quêtes). Le compteur ne voit que ce qui est gagné pendant que le Pet Meter tourne.
- Lecture seule du trafic du jeu via Npcap (comme les DPS meters) : rien n'est envoyé au jeu, la mémoire du jeu n'est pas lue.

> Outil non officiel : à utiliser à tes risques vis-à-vis des règles de NCSOFT.

## Combat : compteur de dégâts en direct

- Onglets **FAMILIERS | COMBAT | RÉCOLTE** en haut de l'overlay. En **AUTO**, la vue Combat s'ouvre dès que tu frappes un boss ou le
  mannequin (la même cible depuis 8 s, ou à plusieurs) et reste affichée 30 s après la fin du combat. Un clic sur un
  onglet fixe la vue (« FIXÉE ») ; un clic sur « FIXÉE » repasse en auto.
- Le tableau, compact comme NotMeter, montre les joueurs qui frappent ta cible, en direct : une ligne d'en-tête (cible,
  durée, total et DPS du groupe), puis une ligne par joueur avec son **nom**, sa **puissance de combat** (en cyan) et les
  colonnes choisies (toi toujours visible). Un clic sur un joueur ouvre le détail : ses chiffres et ses sorts (coups,
  dégâts, part, critique, coup max).
- Noms et puissance viennent de l'apparition des joueurs près de toi : un joueur déjà là quand le Pet Meter démarre
  reste affiché par classe (« Templier 2 ») jusqu'à ce qu'il réapparaisse (changement de zone, téléportation).
- Les invocations comptent pour leur maître (leur apparition dit à qui elles sont) : elles ne prennent plus de ligne.
- Un familier gagné pendant le combat est bien compté : il s'affiche sur une ligne en bas, sans prendre la place du tableau.
  Rien n'est écrit ni envoyé : le tableau vit en mémoire.
- **⋯ → Combat (tableau)**, réglages calqués sur NotMeter : colonnes (dégâts totaux, DPS, contribution), DPS en entier ou
  abrégé, dégâts abrégés, barres aux couleurs des classes, puissance de combat, 5 / 7 / 10 lignes, largeur 300, 330 ou
  400 px, lignes de 20, 22 ou 26 px, tableau gardé 10, 30, 60 s ou jusqu'au prochain combat. Si NotMeter est installé, ses
  réglages (`%LOCALAPPDATA%\Not4You-Meter\settings.json`) sont repris au premier lancement, sauf la largeur et la hauteur
  des lignes (le tableau du Pet Meter est plus compact) ; **Reprendre les réglages de NotMeter** les relit. La
  contribution reste la part des dégâts du groupe : les PV du boss ne sont pas lus.

## Récolte : limite du jour

Depuis le 28/10/2026, la récolte est limitée à **1 000 par jour et par serveur** (commune aux persos du serveur, remise à
zéro à 08:00, heure de Paris). En AUTO, la vue **RÉCOLTE** s'ouvre dès que tu récoltes : récoltes du jour sur 1 000, ce qui
reste, l'heure de la remise à zéro et, au rythme actuel, l'heure du plafond. Elle passe en alerte dès 900.
- Le chiffre est **le compteur du jeu** : le jeu l'envoie à l'entrée en jeu (le Pet Meter sait donc dès le démarrage où
  tu en es, récoltes faites sans lui comprises), puis à chaque récolte commencée. Les récoltes **ratées comptent** : le
  compteur monte dès le début de la récolte. Le nombre de ratées vues par le Pet Meter est indiqué à part.
- Lance le Pet Meter avant d'entrer en jeu pour avoir le chiffre tout de suite ; sinon il arrive à la première récolte.
- Le jeu donne le serveur sous forme de numéro : les serveurs sont donc désignés par leurs persos (« Perso A, Perso B »).
  Le nom des ressources n'est pas dans le trafic (seulement un numéro) : pas de liste par ressource.

## Affichages et thèmes

- Menu **⋯ → Affichage** :
  - le dernier monstre en **Cercle** (par défaut), en **Barre** (sans cercle) ou sur **Une ligne** (minimal) ;
  - **Liste des derniers monstres** : décochée, seul le dernier monstre reste affiché ;
  - **Largeur** des vues Familiers et Récolte : 270, 300 ou 330 px. Le tableau de combat garde sa largeur à lui (menu Combat).
- Menu **⋯ → Thème** : Abysse (par défaut), Asmodien, Élyséen (clair), Sobre, Contraste élevé.
- Un monstre dont le familier est au max est surligné en vert avec **AU MAX** : inutile de le farmer.
  Le jeu ne donne plus d'âmes pour ces monstres mais des cristaux d'espèce ; le Pet Meter les reconnaît
  et retrouve le monstre tué (voir `docs/PROTOCOL.md`).

## Mises à jour

Le Pet Meter vérifie au lancement (puis toutes les 6 h) s'il existe une nouvelle version sur Aion 2 Todo et la télécharge
en arrière-plan. Une ligne **« ⬆ vX.Y.Z prête — clic pour mettre à jour »** apparaît alors en bas de l'overlay (aussi dans le menu ⋯) :
un clic remplace l'exe et relance le Pet Meter. Rien n'est installé sans ce clic.
Pour vérifier tout de suite : menu **⋯ → Vérifier les mises à jour**.

## Synchro au lancement

Le jeu envoie **toute ta collection** quand ton perso entre dans le monde (connexion ou retour à la sélection de perso) :
pas besoin de tuer quoi que ce soit, tout est envoyé à Aion 2 Todo. Si tu lances le Pet Meter alors que tu es déjà en jeu,
il renvoie la dernière collection connue ; repasse par la sélection de perso pour la rafraîchir.

## Enregistrement pour la perception d'espèce

Les stats de la perception d'espèce ne sont lisibles ni sur le site officiel ni dans les messages déjà décodés.
Pour trouver le message du jeu qui les décrit : menu **⋯ → Enregistrer pour la perception d'espèce (5 min)**. Le Pet Meter
écrit alors les messages du serveur qu'il lit déjà (décompressés, avec l'heure) dans `perception-AAAAMMJJ-HHMMSS.jsonl.gz`,
à côté de l'exe (ou dans `%LOCALAPPDATA%\AION2-pet-meter`). Il garde aussi en mémoire ta **dernière entrée en jeu**
(15 s avant et 45 s après l'arrivée de ta collection) et l'écrit en tête de l'enregistrement : il suffit qu'il tourne
quand tu entres en jeu, l'enregistrement peut être lancé ensuite.
**⋯ → Repère n° N** marque une étape (« fenêtre ouverte », « Fera affichée »…). L'enregistrement s'arrête seul au bout
de 5 min ou par **⋯ → Arrêter l'enregistrement** ; **⋯ → Afficher l'enregistrement** ouvre son dossier.
Rien n'est envoyé : le fichier reste sur le PC. Il contient tout ce que le serveur envoie pendant ces minutes
(dont le chat et les persos autour) : ne le partage qu'avec qui tu veux.

## Mes combats (pour Aion 2 Builder)

Le Pet Meter garde aussi tes combats contre un **boss**, le **mannequin** d'entraînement ou un **joueur** (JcJ) : tes
lancers, tes coups et ceux de tes invocations (dégâts, critiques, coups de dos), les coups reçus et tes soins. Un combat se
termine après 15 s sans coup de ta part ; une ligne **« ⚔ combat enregistré : 72 s · 7 443 DPS »** s'affiche quelques
secondes en bas de l'overlay. Les packs de monstres ne sont pas gardés : un boss, c'est une cible frappée pendant au
moins 25 s qui prend au moins la moitié de tes dégâts ; JcJ, une cible qui lance des sorts de joueur. Le combat gardé
est celui du boss, de ton premier à ton dernier coup sur lui (les monstres d'avant et les adds ne comptent pas), avec
tes dégâts sur lui seconde par seconde. Au lancement, le Pet Meter fait son ménage : journal raccourci, seuls les
3 derniers enregistrements gardés.
Chaque combat est écrit dans `%LOCALAPPDATA%\AION2-pet-meter\fights` (un fichier JSON par combat) : l'écran **Combat**
d'Aion 2 Builder les reprend tout seul pour ton perso (lancers face aux recharges,
combos ratés, temps morts, dégâts subis face aux soins). Rien n'est envoyé.
Menu **⋯ → Mes combats** : activer / désactiver, garder aussi les combats ordinaires, ouvrir le dossier.

## Dépannage

- `aion2-pet-meter.exe --selftest` puis regarde `aion2_pet_meter.log` à côté de l'exe (ou dans `%LOCALAPPDATA%\AION2-pet-meter`).
- « clé refusée » : la clé a été remplacée ou désactivée sur le site ; crées-en une nouvelle et recolle-la.
- Après un patch du jeu, le format des paquets peut changer : voir `docs/PROTOCOL.md`.

## Publier une version

Onglet **Actions → Build → Run workflow** avec un numéro (ex. `v0.2.0`), ou pousser un tag `vX.Y.Z` : GitHub Actions compile l'exe sur Windows, crée la version et demande à Aion 2 Todo
de se redéployer (secret `TODO_DISPATCH_TOKEN`), qui la propose alors au téléchargement.
