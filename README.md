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
- Lecture seule du trafic du jeu via Npcap (comme les DPS meters) : rien n'est envoyé au jeu, la mémoire du jeu n'est pas lue.

> Outil non officiel : à utiliser à tes risques vis-à-vis des règles de NCSOFT.

## Dépannage

- `aion2-pet-meter.exe --selftest` puis regarde `aion2_pet_meter.log` à côté de l'exe (ou dans `%LOCALAPPDATA%\AION2-pet-meter`).
- « clé refusée » : la clé a été remplacée ou désactivée sur le site ; crées-en une nouvelle et recolle-la.
- Après un patch du jeu, le format des paquets peut changer : voir `docs/PROTOCOL.md`.

## Publier une version

Onglet **Actions → Build → Run workflow** avec un numéro (ex. `v0.2.0`), ou pousser un tag `vX.Y.Z` : GitHub Actions compile l'exe sur Windows, crée la version et demande à Aion 2 Todo
de se redéployer (secret `TODO_DISPATCH_TOKEN`), qui la propose alors au téléchargement.
