# AION2 Pet Meter

Petite appli Windows (overlay) qui affiche tes âmes de familier quand tu tues un monstre dans **Aion 2**,
et les envoie automatiquement à **[Aion 2 Todo](https://aion2-todo-7275.azurewebsites.net)** (onglet Familiers).

Basée sur [AION2-pet-tracker](https://github.com/elkaida/AION2-pet-tracker) (MIT, voir `THIRD_PARTY_NOTICES.md`).

## Installation

1. Installe **[Npcap](https://npcap.com/#download)** (gratuit ; déjà présent si tu utilises un DPS meter).
2. Télécharge `aion2-pet-meter.exe` depuis Aion 2 Todo (onglet Familiers → Pet Meter) et mets-le dans son propre dossier.
   Windows peut afficher « éditeur inconnu » : *Informations complémentaires → Exécuter quand même*.
3. Sur Aion 2 Todo, onglet Familiers → Pet Meter → **Créer ma clé**, puis dans l'overlay : menu **⋯ → Aion 2 Todo → Clé de synchro Aion 2 Todo…** et colle la clé.
4. Lance le Pet Meter **avant d'entrer en jeu** avec ton perso (c'est là que le jeu envoie toute ta collection).
   Le jeu doit être en **plein écran fenêtré** ou fenêtré pour voir l'overlay.

## Ce qu'il fait

- Pour le monstre que tu viens de tuer : ses 3 niveaux, combien de kills il reste, **MAX +N** si le familier est déjà au max.
- Synchro : quelques secondes après une série de kills, l'état complet de ta collection est envoyé à Aion 2 Todo
  (clé personnelle, en HTTPS). Le pied de l'overlay indique l'heure de la dernière synchro.
- **Masquage auto** (actif par défaut) : sans butin de familier pendant 5 s, l'overlay se cache ; il revient au survol
  de la souris ou au prochain butin. **⋯ → Affichage → Masquage auto** : le couper, ou choisir le délai (3, 5, 10, 30 ou 60 s).
  Un problème (Npcap absent, lecture en échec) reste affiché.
- **Kinah du jour** (masqué par défaut, **⋯ → Kinah du jour → Afficher**) : les Kinah gagnés sur les monstres depuis le
  reset (07:00 UTC), par perso, face au plafond quotidien (1 M par défaut, réglable dans le même menu). Seuls les gains qui suivent un kill comptent
  (pas les ventes ni les récompenses de quêtes). Le compteur ne voit que ce qui est gagné pendant que le Pet Meter tourne.
- **Âmes par carte** : chaque jour de jeu, le Pet Meter garde sur ton PC les âmes gagnées sur chaque carte, par familier,
  avec les monstres tués et le temps passé à farmer la carte, dans `%LOCALAPPDATA%\AION2-pet-meter\souls\AAAA-MM-JJ.json`
  (60 jours, écrit hors combat au plus toutes les 10 s) : Aion 2 Builder peut en tirer où farmer quel familier. Format
  dans `docs/PROTOCOL.md`. Rien n'est envoyé.
- Lecture seule du trafic du jeu via Npcap (comme les DPS meters) : rien n'est envoyé au jeu, la mémoire du jeu n'est pas lue.

> Outil non officiel : à utiliser à tes risques vis-à-vis des règles de NCSOFT.

## Combat : compteur de dégâts en direct

- Onglets **FAMILIERS | COMBAT | RÉCOLTE** en haut de l'overlay. En **AUTO**, la vue Combat s'ouvre dès que tu frappes un boss ou le
  mannequin (la même cible depuis 8 s, ou à plusieurs) et reste affichée 30 s après la fin du combat. Un clic sur un
  onglet fixe la vue (« FIXÉE ») ; un clic sur « FIXÉE » repasse en auto.
- Le tableau, compact comme NotMeter, montre les joueurs qui frappent ta cible, en direct : une ligne d'en-tête (cible,
  durée, total et DPS du groupe), puis une ligne par joueur avec son **nom**, sa **puissance de combat** (en cyan) et les
  colonnes choisies (toi toujours visible). Un clic sur un joueur ouvre le détail : ses chiffres et ses sorts avec leur
  icône officielle (coups, dégâts, part, critique, coup max) ; on le déplace en le faisant glisser, × ou Échap le ferme.
- **Historique des combats** (≡ historique en tête de la vue Combat, ou ⋯ → Combat → Historique des combats), comme le livre de
  NotMeter : les 50 derniers combats finis (boss, mannequin, groupe), gardés d'une session à l'autre dans
  `combat_history.json` (sur ce PC, rien n'est envoyé). Clic sur un combat : son tableau ; clic sur un joueur : son détail.
- **Tableau en image** : **⧉** en tête de la vue Combat (ou ⋯ → Combat → « Copier le tableau en image », ou ⧉ dans un
  combat de l'historique) met le tableau dans le presse-papiers en image (cible, durée, total et DPS du groupe, chaque
  joueur avec sa barre, sa puissance, ses dégâts, son DPS et sa contribution) : **Ctrl+V dans Discord**. Sans combat à
  l'écran, c'est le dernier combat de l'historique.
- Détail d'un joueur, compact par défaut : dégâts, DPS, contribution (la même mesure que le tableau), critique, coup max,
  durée ; par sort : coups, dégâts, %, critique, max. **+ colonnes** ajoute utilisations, de dos, parfaits, puissants,
  multiples et moyenne (choix gardé) ; le survol d'un sort donne tout son détail. Échap ou × le ferme.
- Contrôle sur le mannequin : même DPS que NotMeter à 0,05 % près (26 768 contre 26 782).
- Le tableau suit ta **cible principale** : la plus grosse de celles que tu frappes (PV max lus à son apparition), donc le
  boss et pas ses adds. Il compte depuis le premier coup qu'elle a reçu, même si le combat a commencé sans toi, et se fige
  à sa mort (sinon 15 s après ton dernier coup) : le boss suivant, même juste à côté, ouvre un nouveau combat.
- Seuls les joueurs qui frappent **la même cible que toi** apparaissent, et seulement ceux **à portée** : le serveur n'envoie
  que ce qui se passe près de toi (NotMeter a la même limite). Un joueur qui ne frappe plus la cible depuis 10 s (sorti de
  portée, mort, occupé ailleurs) passe en gris : ses chiffres n'avancent plus. Sur un mannequin, chacun tape le sien : tu es seul au
  tableau, et un mannequin que tu ne frappes pas ne s'affiche jamais.
- Noms et icônes des sorts des 8 classes : armurerie officielle (aion2.plaync.com). Les icônes sont téléchargées depuis le
  site officiel la première fois qu'elles s'affichent, puis gardées dans le dossier `icons` (à côté de l'exe ou dans
  `%LOCALAPPDATA%\AION2-pet-meter`). Les enchaînements que l'armurerie ne liste pas s'affichent « Sort inconnu · code ».
- Noms et puissance viennent de l'apparition des joueurs près de toi : un joueur déjà là quand le Pet Meter démarre
  reste affiché par classe (« Templier 2 ») jusqu'à ce qu'il réapparaisse (changement de zone, téléportation).
- Les invocations comptent pour leur maître : leur apparition dit à qui elles sont ; sinon, leur échelle de puissance a
  les mêmes 3 derniers chiffres que celle de leur maître ; à défaut, c'est le seul joueur de leur classe dans le combat.
  Un joueur n'est jamais pris pour une invocation, même de ta classe et de même échelle que toi.
  Elles ne prennent pas de ligne ; leurs sorts de créature apparaissent dans le détail du maître sous « Invocations ».
- Un familier gagné pendant le combat est bien compté : il s'affiche sur une ligne en bas, sans prendre la place du tableau.
  Rien n'est écrit ni envoyé : le tableau vit en mémoire.
- **⋯ → Combat**, réglages calqués sur NotMeter : colonnes (dégâts totaux, DPS, contribution), DPS en entier ou
  abrégé, dégâts abrégés, barres aux couleurs des classes, puissance de combat, 5 / 7 / 10 lignes, largeur 300, 330 ou
  400 px, lignes de 20, 22 ou 26 px, tableau gardé 10, 30, 60 s ou jusqu'au prochain combat. Si NotMeter est installé, ses
  réglages (`%LOCALAPPDATA%\Not4You-Meter\settings.json`) sont repris au premier lancement, sauf la largeur et la hauteur
  des lignes (le tableau du Pet Meter est plus compact) ; **Reprendre les réglages de NotMeter** les relit.
- **Contribution** : en part des dégâts du groupe (100 % au total), ou, comme NotMeter réglé sur « BossMaxHp », en % des
  PV max du boss (lus à son apparition ; le total dépasse 100 % si le boss se soigne ou se relève). ⋯ → Combat pour
  choisir ; le réglage de NotMeter est repris.
- Contrôle fait sur un enregistrement de groupe : les dégâts lus dans les coups font 97,5 % des PV perdus par le boss
  (dégâts sur la durée et invocations compris).

## Récolte : limite du jour

Depuis le 28/10/2026, la récolte est limitée à **1 000 par jour et par serveur** (commune aux persos du serveur, remise à
zéro à 08:00, heure de Paris). En AUTO, la vue **RÉCOLTE** s'ouvre dès que tu récoltes : récoltes du jour sur 1 000, ce qui
reste, l'heure de la remise à zéro et, au rythme actuel, l'heure du plafond. Elle passe en alerte dès 900.
- Le chiffre est **le compteur du jeu** : le jeu l'envoie à l'entrée en jeu (le Pet Meter sait donc dès le démarrage où
  tu en es, récoltes faites sans lui comprises), puis à chaque récolte commencée. Les récoltes **ratées comptent** : le
  compteur monte dès le début de la récolte. Le nombre de ratées vues par le Pet Meter est indiqué à part.
- Lance le Pet Meter avant d'entrer en jeu pour avoir le chiffre tout de suite ; sinon il arrive à la première récolte.
- Le jeu donne le serveur sous forme de numéro : les serveurs sont donc désignés par leurs persos (« Perso A, Perso B »).
- **Ressources du jour** : sous le compteur, ce que tu as récolté aujourd'hui sur ce serveur, de la plus récoltée à la
  moins (« Minerai d'orichalque ×42 »). Le jeu n'envoie qu'un numéro d'objet : le Pet Meter a la liste des 46 ressources
  de récolte (Od, minerai, bois, gemmes, plantes, ingrédients, chacune dans ses rangs) dans la langue du jeu (français,
  anglais ou russe) ; un numéro inconnu s'affiche « objet 6106… ». Numéros et noms anglais d'après
  [aion2hub.com](https://aion2hub.com/database/items), noms français et russes d'après
  [metabot.gg](https://metabot.gg/fr_FR/aion-2/items) (une page par ressource, à son nom anglais :
  [Minerai d'orichalque](https://metabot.gg/fr_FR/aion-2/items/orichalcum-ore), …).
- **Mode discret** (⋯ → Affichage → « Récolte : petit message en bas à droite », coupé par défaut) : la récolte n'ouvre
  plus l'overlay ; un petit message en bas à droite de la fenêtre du jeu dit ce que tu as récolté (« ✓ Minerai
  d'orichalque ×3 », avec « Rang supérieur » en dessous pour une récolte du rang au-dessus) ou si elle est ratée,
  (une ressource éclatante, ton « Gold », en or ; dans la vue Récolte, les éclatantes passent en tête de liste)
  avec le compteur du jour et, au rythme actuel, l'heure du plafond (« plafond vers 21:40 »), et s'efface au bout
  de 3 s (ou au clic).
- **Résumé de chaque jour** : le Pet Meter garde, sur ton PC, la récolte de chaque jour dans
  `%LOCALAPPDATA%\AION2-pet-meter\gathering\AAAA-MM-JJ.json` (60 jours) : par serveur, le compteur du jeu, les ratées,
  les récoltes au rang supérieur, les persos et chaque ressource (numéro, nom, rang, quantité), avec les Kinah de monstres
  de chaque perso. **Aion 2 Builder** les montre dans sa page **Récolte** (jour par jour, 7 ou 30 jours). Avec ta clé
  Aion 2 Todo, le détail du jour part aussi avec la synchro du jour : le site additionne ce que chacun de tes PC a vu,
  et le Builder montre la récolte de tout ton compte, quel que soit le PC.

## Affichages et thèmes

- **Survol** : ce qui est souligné en pointillés (curseur « ? ») s'explique au survol : le cercle, les pastilles, les
  colonnes du tableau de combat, les chiffres du détail, le compteur de récolte, le pied… Une astuce le rappelle dans le pied
  tant qu'aucune explication n'a été vue.
- Menu **⋯** regroupé : Affichage, Combat, Kinah du jour, Langue, Aion 2 Todo, Outils, puis mise à jour et Quitter.
- Menu **⋯ → Affichage** :
  - le dernier monstre en **Cercle** (par défaut), en **Barre** (sans cercle) ou sur **Une ligne** (minimal) ;
  - **Liste des derniers monstres** : décochée, seul le dernier monstre reste affiché ;
  - **Largeur** des vues Familiers et Récolte : 270, 300 ou 330 px. Le tableau de combat garde sa largeur à lui (menu Combat) ;
  - **Récolte : petit message en bas à droite** (coupé par défaut) : voir « Récolte : limite du jour » ;
  - **Familiers : petit message en bas à droite** (coupé par défaut) : les âmes n'ouvrent plus l'overlay. Un seul petit
    message, mis à jour à chaque âme même quand tu farmes vite : le familier et ses âmes (« ✦ Fossa +3 »), son niveau
    (« niv. 1 · 5 / 25 », ou « Niveau 2 atteint ! » en or, ou « AU MAX ») avec une barre, et la série en cours
    (« Série : 14 monstres · 5 familiers · +38 âmes » ; une pause de plus de 20 s en commence une autre), et combien de
    monstres il reste à peu près pour le niveau suivant (« ≈ 8 monstres », au même nombre d'âmes par monstre). Il s'efface
    4 s après la dernière âme (ou au clic).
  - **Rendement de la session (par heure)** (coupé par défaut) : une ligne en bas de chaque vue, « Session 1 h 12 · 152
    monstres/h · 98 âmes/h · 412,3 k Kinah/h » (et les récoltes/h, en tête sur la vue Récolte), autant qu'il en tient dans
    la largeur, depuis le lancement du Pet Meter ; les totaux au survol. **Remettre le rendement à zéro** repart de
    maintenant (avant un nouveau spot de farm, par exemple). Rien de nouveau n'est lu : ce sont les âmes, cristaux, Kinah de
    monstres et récoltes déjà comptés.
- Menu **⋯ → Affichage → Thème** : Abysse (par défaut), Asmodien, Élyséen (clair), Sobre, Contraste élevé.
- Un monstre dont le familier est au max est surligné en vert avec **AU MAX** : inutile de le farmer.
  Le jeu ne donne plus d'âmes pour ces monstres mais des cristaux d'espèce ; le Pet Meter les reconnaît
  et retrouve le monstre tué (voir `docs/PROTOCOL.md`).

## Mises à jour

Le Pet Meter vérifie au lancement (puis toutes les 6 h) s'il existe une nouvelle version sur Aion 2 Todo et la télécharge
en arrière-plan. Depuis la v0.21.0, **les mises à jour sont automatiques** : la version prête s'installe quand tu quittes le
Pet Meter (ou au lancement suivant s'il a été fermé autrement), jamais pendant que tu joues ; un message le dit à son arrivée.
Pour choisir le moment toi-même : menu **⋯ → Mises à jour → « Mises à jour automatiques »** (décoché : une ligne
**« ⬆ vX.Y.Z prête — clic pour mettre à jour »** en bas de l'overlay, et rien n'est installé sans ce clic). Le même menu
permet de vérifier tout de suite.

**Capture limitée aux connexions du jeu (essai)** : menu ⋯ → Outils (coupée par défaut, prise au lancement suivant). Le Pet
Meter ne fait plus examiner que le trafic des serveurs du jeu au lieu de tout le trafic hors web : moins de CPU quand un
téléchargement ou un stream tourne en fond. En essai tant qu'un enregistrement de contrôle n'a pas confirmé qu'aucun paquet
n'est perdu.

## Synchro au lancement

Avec ta clé Aion 2 Todo, le Pet Meter remplit aussi deux tâches du jour du site : **Récolte — limite du jour** (le compteur
du jeu, par serveur) et **Kinah de monstres** (ceux qu'il a vus tomber aujourd'hui, par perso, retrouvé par son nom). Il
compte aussi les **boss tués avec toi** dans la semaine de jeu (reset le mercredi), par perso et par code de créature :
le site en tire les expéditions et les raids de la semaine dont il connaît le boss final. Un boss compte s'il était ta
cible principale et que tu l'as frappé au moins 25 s avant sa mort, une seule fois. Un envoi toutes les 20 s au plus pendant que tu
joues ; jamais à la baisse, une tâche cochée à la main le reste.

Le jeu envoie **toute ta collection** quand ton perso entre dans le monde (connexion ou retour à la sélection de perso) :
pas besoin de tuer quoi que ce soit, tout est envoyé à Aion 2 Todo. Si tu lances le Pet Meter alors que tu es déjà en jeu,
il renvoie la dernière collection connue ; repasse par la sélection de perso pour la rafraîchir.

## Enregistrement pour la perception d'espèce

Les stats de la perception d'espèce ne sont lisibles ni sur le site officiel ni dans les messages déjà décodés.
Pour trouver le message du jeu qui les décrit : menu **⋯ → Outils → Enregistrer le trafic (5 min, diagnostic)**. Le Pet Meter
écrit alors les messages du serveur qu'il lit déjà (décompressés, avec l'heure) dans `perception-AAAAMMJJ-HHMMSS.jsonl.gz`,
à côté de l'exe (ou dans `%LOCALAPPDATA%\AION2-pet-meter`). Il garde aussi en mémoire ta **dernière entrée en jeu**
(15 s avant et 45 s après l'arrivée de ta collection) et l'écrit en tête de l'enregistrement : il suffit qu'il tourne
quand tu entres en jeu, l'enregistrement peut être lancé ensuite.
Pendant l'enregistrement, **Repère n° N** (en tête du menu ⋯) marque une étape (« fenêtre ouverte », « Fera affichée »…). L'enregistrement s'arrête seul au bout
de 5 min ou par **Arrêter l'enregistrement** (en tête du menu ⋯) ; **⋯ → Outils → Afficher l'enregistrement** ouvre son dossier.
Rien n'est envoyé : le fichier reste sur le PC. Il contient tout ce que le serveur envoie pendant ces minutes
(dont le chat et les persos autour) : ne le partage qu'avec qui tu veux.

## Mes combats (pour Aion 2 Builder)

Le Pet Meter garde aussi tes combats contre un **boss**, le **mannequin** d'entraînement ou un **joueur** (JcJ) : tes
lancers, tes coups et ceux de tes invocations (dégâts, critiques, coups de dos), les coups reçus et tes soins. Un combat se
termine à la mort du boss, ou après 15 s sans coup de ta part ; une ligne **« ⚔ combat enregistré : 72 s · 7 443 DPS »** s'affiche quelques
secondes en bas de l'overlay (depuis la v0.20.11, sur un boss ou le mannequin : **« nouveau record »** ou **« ton record : N »**,
d'après tes 30 derniers combats gardés). Option (⋯ → Affichage → « Combat : rappel des sorts prêts », coupée par défaut) :
pendant un combat, une ligne **« ⟳ prêt : … »** sous le tableau nomme tes sorts à recharge (8 s et plus) déjà lancés dans ce
combat et prêts depuis plus de 2 s sans être relancés ; rien d'autre ne change, rien n'agit sur le jeu. Autre option (⋯ →
Affichage → « Combat : alerte de perte d'aggro », coupée) : **« ⚠ aggro : le boss frappe … »** quand le boss frappe un autre
joueur avec une attaque qui te vise d'habitude (pas ses sorts de zone ni ceux qui choisissent une cible). Depuis la v0.21.2,
le fichier du combat garde aussi qui le boss a frappé, coup par coup : Aion 2 Builder en tire la carte « Aggro du boss ». Les packs de monstres ne sont pas gardés : un boss, c'est une cible frappée pendant au
moins 25 s qui prend au moins la moitié de tes dégâts ; JcJ, une cible qui lance des sorts de joueur. Le combat gardé
est celui du boss, de ton premier à ton dernier coup sur lui (les monstres d'avant et les adds ne comptent pas), avec
tes dégâts sur lui seconde par seconde. Au lancement, le Pet Meter fait son ménage : journal raccourci, seuls les
3 derniers enregistrements gardés. Discret pendant le jeu : le journal ne garde que les erreurs (une même erreur au plus
une fois toutes les 10 min, avec le nombre de répétitions), rien pour les synchros réussies ni les packs de monstres, et
l'état n'est écrit sur le disque qu'au plus toutes les 10 s pendant le farm (toujours en quittant). Au lancement, il note aussi
son dossier de données dans `%LOCALAPPDATA%\AION2-pet-meter\data_dir.txt` : Aion 2 Builder y retrouve ta clé Aion 2 Todo
quand le Pet Meter range son état à côté de son exe (tu n'as pas à la recoller dans le Builder). Depuis la v0.20.9, la fiche
de stats que le jeu envoie à la connexion (puis ses changements) est gardée par perso, hors combat et au plus toutes les 10 s,
dans `%LOCALAPPDATA%\AION2-pet-meter\ingame-stats.json` : Aion 2 Builder y reprend, sur ton clic, tes PV, PM, VIT de combat
et de déplacement et ta Perforation en jeu (Stats → « Stats en jeu » → « reprendre du Pet Meter »). Depuis la v0.21.0, il
garde de la même façon ton inventaire (envoyé par le jeu à l'entrée en jeu, puis chaque changement) dans
`%LOCALAPPDATA%\AION2-pet-meter\inventory.json` : ressources, pièces portées, leurs pierres et options, pour qu'Aion 2
Builder tienne compte de ce que tu possèdes. Rien n'est envoyé.
Chaque combat est écrit dans `%LOCALAPPDATA%\AION2-pet-meter\fights` (un fichier JSON par combat) : l'écran **Combat**
d'Aion 2 Builder les reprend tout seul pour ton perso (lancers face aux recharges,
combos ratés, temps morts, dégâts subis face aux soins). Depuis la v0.19.0, le fichier garde aussi les **joueurs du
combat** vus par le compteur en direct sur la même cible (toi compris) : nom quand il est apparu près de toi, classe,
puissance de combat, DPS, part des dégâts et sorts principaux. Aion 2 Builder les montre sous le combat et peut ouvrir
la fiche de chacun depuis le site officiel. Rien n'est envoyé.
Menu **⋯ → Outils → Mes combats** : activer / désactiver, garder aussi les combats ordinaires, ouvrir le dossier.

## Dépannage

- `aion2-pet-meter.exe --selftest` puis regarde `aion2_pet_meter.log` à côté de l'exe (ou dans `%LOCALAPPDATA%\AION2-pet-meter`).
- « clé refusée » : la clé a été remplacée ou désactivée sur le site ; crées-en une nouvelle et recolle-la.
- Après un patch du jeu, le format des paquets peut changer : voir `docs/PROTOCOL.md`.

## Publier une version

Onglet **Actions → Build → Run workflow** avec un numéro (ex. `v0.2.0`), ou pousser un tag `vX.Y.Z` : GitHub Actions compile l'exe sur Windows, crée la version et demande à Aion 2 Todo
de se redéployer (secret `TODO_DISPATCH_TOKEN`), qui la propose alors au téléchargement.
