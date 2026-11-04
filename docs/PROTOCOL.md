# Protocol notes

What is known about the Aion 2 packets the overlay uses. All of this was found by recording traffic with `tools/aion2_capture.py` and comparing it with in-game actions. A game patch can change any of it.

Only **server → client** traffic is used. It is not encrypted. Client → server traffic is encrypted and not needed.

## Connection

- The game keeps one long-lived TCP connection to the game server, on a non-standard port.
- The overlay doesn't hard-code the address. It finds the TCP connections of `AION2.exe` with `psutil` and listens to every port they use except 80 and 443.
- If the PC uses a VPN, the plain traffic is visible on the VPN's virtual adapter, so the overlay listens on all adapters.

## Framing

The TCP stream is a sequence of frames:

```
varint  length      # protobuf-style LEB128
u16     opcode      # stored as two bytes; written below in stream order
...     payload
```

The whole frame, including the length varint, takes `length - 4 + size_of_varint` bytes.

### Compressed bundles

Opcode `ff ff` marks a compressed bundle:

```
varint  length
ff ff
u32le   uncompressed_size
...     LZ4 block (raw, no frame header)
```

Decompressed, a bundle is again a sequence of frames in the same format. The big bundles arrive when a character enters the world.

## Spirit point packets

### `00 90` — full state on entering the world

It sits inside an LZ4 bundle.

```
u32le   unknown
varint  n_levels
n_levels × { u32le pet_id, u32le pet_id (same), u32le level }
varint  n_points
n_points × { u32le pet_id, u32le points }
...     more data (not used)
```

- `level` is 0–3. `points` is the progress inside the current level and resets on every level-up.
- Pets that aren't listed are at 0.
- At level 3 the server always reports 0 points: it stops counting.

Example: `1097 → level 1, points 21` showed in game as 21/25 toward level 2.

IDs run from about 1001 to 1220 — one per pet, the same pets that can be ridden once unlocked (about 206 of them).

### `0d 90` — spirit points gained (one per kill)

```
u8      kind        # always 1 so far
u32le   pet_id
u32le   amount      # the "xN" in the chat line
```

This packet carries no total, so the client (and the overlay) adds `amount` to the last known state. Level-ups happen locally with thresholds 5 / 25 / 75.

## Pet names

Names aren't in the traffic. The overlay uses its own ID → name database, `data/names.json`, built with `tools/fetch_pet_names.py` from the MetaBot.GG database. For IDs missing there, it falls back to reading the chat. In the game client itself, names live in encrypted localization paks.

## Re-checking after a patch

1. Run `python tools/aion2_capture.py capture.jsonl` before entering the world.
2. Re-enter the world, then kill a few monsters and press F9 after each kill.
3. Look for frames that appear exactly once per F9 mark and for the state packet in the login burst. Search for a pet ID you know as `u32le`.

## Familier au max : cristaux d'espèce (ajout Pet Meter)

Quand le familier d'un monstre est déjà au niveau 3, le serveur n'envoie plus `0d 90` (âmes) :
il envoie des **cristaux d'espèce**, sans dire de quel monstre ils viennent. Le Pet Meter retrouve le monstre
grâce à sa mort, juste avant. Dispositions publiées par la communauté (DPS meters, puis
[Nyerk](https://github.com/xCROv/aion2-pet-tracker)) ; le code du Pet Meter est écrit indépendamment.

```
41 36  apparition   <entité varint> ... <code créature u24 LE> 00 (00|40) 02 ...   (le code précède le marqueur, dans les 60 octets)
42 36  mort         <entité varint> <varint> <raison varint>
11 90  cristaux     01 <espèce u8> <quantité u32 LE> ...   (espèce : 1 = Fera, 2 = Cogni ; Natura / Varian pas encore observés)
```

- Code créature → familier : `data/npc_pets.json`, généré depuis le catalogue d'Aion 2 Todo.
- Les cristaux sont rattachés à la mort la plus récente (moins de 3 s), une mort ne sert qu'une fois,
  et l'espèce doit correspondre quand elle est connue.

## Kinah du jour (ajout Pet Meter)

Dispositions publiées par [Nyerk](https://github.com/xCROv/aion2-pet-tracker) ; code écrit indépendamment.

```
1b 56  pile d'inventaire   01 <8 octets> ... <objet 4 octets> <quantité u64 LE>   (Kinah : objet 3f 33 70 37 aux octets 11-14, solde aux octets 15-22)
33 36  ton perso           <entité varint> <5 octets> <longueur u8> <nom UTF-8> <serveur u16 LE> ...   (à chaque entrée dans une zone)
```

- Le solde de Kinah est renvoyé après chaque kill qui en rapporte. Le gain = nouveau solde − solde précédent.
- Seuls les gains arrivés moins de 5 s après la mort d'un monstre (`42 36`) comptent pour le plafond quotidien.
- Compteur par perso et par jour de jeu (reset quotidien à 07:00 UTC).

## Récolte (ajout Pet Meter)

Établi sur un enregistrement du 28/10/2026 (trois récoltes : réussie, ratée, réussie), jour où la limite de 1 000 récoltes
par jour et par serveur est arrivée.

```
a7 ff  compteur du jour  <récoltes du jour u64 LE> <heure u64 LE, ms>   (à l'entrée en jeu, ~9 s avant 33 36)
a8 ff  compteur du jour  même disposition, à chaque récolte commencée (avant son résultat)
33 8d  ta récolte        <résultat u8 : 0 réussie, 1 réussie au rang supérieur, 2 ratée> <u8, 3a> <filon varint> <objet 4 octets> <quantité u32 LE> <12 octets>
3e 8d  ?                 <u32> <heure u64 LE, ms>   (après une récolte réussie seulement : 1, puis 2 ; pas utilisé)
34 8d  fin de récolte    <joueur varint> <résultat u8> 3a <filon varint>   (envoyé aussi pour les autres joueurs : pas utilisé)
1b 56  pile d'inventaire de l'objet récolté (même disposition que les Kinah)
```

- `a7 ff` valait 12 à l'entrée en jeu avant les trois récoltes, 15 à l'entrée en jeu d'après ; `a8 ff` a donné 13, 14,
  15 pendant ces récoltes (dont une ratée) : c'est le compteur de la limite, ratées comprises.
- `33 8d` n'arrive que pour tes récoltes. Une récolte ratée a une quantité de 0 (résultat 2). Le résultat 1 n'est pas un
  échec : c'est l'objet du rang au-dessus (code voisin, ex. `6e526524` au lieu de `6d526524`), ajouté à l'inventaire
  (`1b 56`) comme une récolte normale (enregistrement du 29/10/2026 : 3 récoltes à 1, une ratée à 2).
- L'objet de `33 8d` est le numéro d'objet du jeu en u32 little-endian : `6d526524` = 610620013, Minerai d'orichalque ;
  le rang au-dessus est le numéro suivant (610620014, … d'une grande pureté). Ressources de récolte : 61061 Od, 61062
  minerai, 61065 bois, 61066 gemmes, 61067 plantes, 61068 ingrédients (`GATHER_ITEMS`, numéros d'après
  [aion2hub.com](https://aion2hub.com/database/items), noms d'après [metabot.gg](https://metabot.gg/fr_FR/aion-2/items),
  30/10/2026).
- `33 36` (ton perso) : le u16 qui suit le nom est le numéro du serveur.

## Joueurs et invocations, pour le tableau de combat (ajout Pet Meter)

Établi sur un enregistrement de combat de groupe du 28/10/2026, recoupé avec la puissance de combat de l'armurerie officielle.

```
45 36  un joueur apparaît   <entité varint> <5 octets> <longueur u8> <nom UTF-8> ... <xx> f4 cb 1f <u32> <u32> <puissance u32> <5 octets>
41 36  apparition           ... ff×8 <8 octets> <entité varint> ...   (l'apparu lui-même, ou le maître pour une invocation)
56 36  ta puissance         <puissance u64 LE> <u64>
02 97  liste du groupe      ... <longueur u8> <nom> <classe u32> <niveau u32> <niveau d'objet u32> <u16> <u16> 04 <puissance u32> ...   (pas utilisé)
```

- L'entité de `45 36` est celle des coups (`04 38`) : elle donne le nom et la puissance des joueurs du tableau.
- Les invocations lancent aussi leurs propres sorts (d'une autre classe parfois, ou des sorts de créature à 7 chiffres) :
  sans le maître de `41 36`, chacune devenait un « joueur » de plus.
- L'échelle de puissance d'un coup (`04 38`) change de millier pendant le combat (11714, 13714…) mais garde ses 3 derniers
  chiffres, propres au joueur ; ses invocations ont les mêmes (Spiritualiste : 11714 et 13714 pour lui comme pour ses
  invocations). Une échelle ronde (10000) est commune à plusieurs joueurs. C'est ce qui rattache une invocation dont on
  n'a pas vu l'apparition.

## PV des boss (ajout Pet Meter)

```
41 36  apparition   ... <code u24> 00 (00|40) 02 <4 floats : position> 00 <varint> <PV varint> <PV max varint> 64 00 00 00 64 00 00 00 ...
00 8d  PV           <entité varint> 02 01 00 <PV u32 LE> <u32>   (à chaque changement)
```

- Boss tué de l'enregistrement du 28/10/2026 : PV max 8 208 000 à l'apparition, premier relevé 8 040 938, puis 0 ; la somme
  des dégâts lus dans les coups (`04 38`) sur la même période fait 97,5 % des PV perdus.
- Le Pet Meter lit les PV max pour la contribution « % des PV max du boss » (comme NotMeter réglé sur BossMaxHp) ; `00 8d`
  n'est pas utilisé.

## Combats pour Aion 2 Builder (ajout Pet Meter)

Lecture seule du trafic, comme le reste. Varints = LEB128 (7 bits par octet).

```
04 38  un coup      <cible varint> <switch varint> <varint> <attaquant varint> <sort u32 LE> <1 octet> <type varint>
                    switch & 0x0F = 0 ou 2 : sans valeur (esquive, résistance, ou simple marque sans type) ; 4 à 7 : avec valeur
                    puis un bloc fixe de 8 octets (switch&0x0F = 4), 12 (5), 10 (6) ou 14 (7) ;
                    si switch & 0x02 : ce bloc commence par <drapeaux 1 octet> <PV rendus varint> <angle 1 octet>
                    (angle 1 = de dos, 2 = de face) et s'allonge de (longueur du varint - 1)
                    puis <échelle varint> <dégâts varint> ; échelle 0 = bourrage : <échelle> <dégâts> suivent
                    type 3 = critique ; l'échelle est la puissance de l'attaquant (centièmes de %), la même pour ses invocations
02 38  un lancer    <lanceur varint> 00 <sort u32 LE> <n° 1 octet> <forme 1 octet> ...   forme 2 = sur la cible (cible varint, position), 0 = la suite ; une utilisation = les deux, à moins de 0,6 s
21 36  une carte    <u32> <carte u32> ...   (à chaque chargement ; un donjon garde la même d'un boss à l'autre)
4a 36  tes stats    <ton entité varint> ...   (envoyé régulièrement : dit qui est « toi »)
```

## Âmes par carte, pour Aion 2 Builder (ajout Pet Meter)

Rien de nouveau n'est lu : ce sont les âmes (`0d 90`) et les cristaux (`11 90`, rattachés à leur monstre comme plus haut)
déjà comptés, rangés sur la carte en cours (`21 36`). Un fichier par jour de jeu (reset à 07:00 UTC) :
`%LOCALAPPDATA%\AION2-pet-meter\souls\AAAA-MM-JJ.json`, gardé 60 jours, écrit hors combat au plus toutes les 10 s (et en
quittant), repris s'il existe quand le Pet Meter est relancé dans la journée.

```json
{"day": "2026-11-05", "client": "pet-meter vX.Y.Z",
 "maps": {"<carte>": {"seconds": 1834.5, "kills": 412, "souls": {"<familier>": 96}, "crystals": {"<familier>": 30}}}}
```

- `<carte>` : le n° de carte de `21 36` en texte (le même que `map` dans les fichiers de combat) ; `"0"` = carte pas encore
  lue (Pet Meter lancé après le dernier chargement de zone).
- `seconds` : temps passé à farmer la carte, en secondes (une décimale) = la somme des écarts entre deux de tes monstres
  successifs sur la même carte, quand l'écart fait au plus 60 s (au-delà : une pause, pas comptée).
- `kills` : tes monstres sur la carte qui ont donné des âmes ou des cristaux (un `0d 90` ou un `11 90` = un monstre).
- `souls` : âmes gagnées par familier ; `<familier>` = le numéro de familier de `0d 90` / `00 90` (1001–1220, les clés de
  `data/names.json`).
- `crystals` (absent s'il n'y en a pas) : cristaux d'espèce gagnés sur les monstres dont le familier est au max, par
  familier retrouvé ; des cristaux dont le monstre n'a pas été retrouvé comptent seulement dans `kills`.

## Inventaire (ajout Pet Meter, décodé le 04/11/2026)

```
11 56  inventaire complet   u16 | varint n | n objets | 6 octets        (une fois à l'entrée en jeu, avant 33 36)
1b 56  changements          varint n | n × (opération u8 : 0 nouvelle pile, 1 nouvelle quantité, 2 retirée | objet | raison 3 octets)
objet  genre u8 (1 = équipement) | exemplaire 8 octets | n° d'objet u32 | quantité u64 | conteneur u8 | emplacement u32 |
       booléen | longueur u8 + nom du fabricant | fin de validité u64 (ms UTC, 0 = jamais) | booléen
       équipement en plus : enchantement u32 | 10 octets | score u32 | booléen | emplacement porté u8 |
       n × (pierre u32, stat u16, grade u8) | lien 9 octets | n × pierre divine u32 | arcane (u8 présent, stat u16, i32) |
       options n × (id u16, valeur i32) | 2e liste | compétences n × (code u32, +niveau u8) | 2e liste | 3 octets
```

- Les booléens sont rangés 8 par octet (bit 0 d'abord) : un nouvel octet est pris dans le flux, là où l'on en est, quand
  l'octet en cours est plein, et il sert aux objets suivants (objets de 35 ou 36 octets). Vérifié sur 218 et 222 objets.
- Les numéros d'objet sont les `id` du site officiel ; conteneurs 1 = sac, 2 et 15 = coffre, 11 = pièces portées
  (emplacement = `slotPos`), 4 = passage (âmes de familier, pas gardé). Le coffre arrive en entier dans `11 56` ; l'ouvrir
  n'envoie rien. Un dépôt = un `1b 56` (pile du coffre : op 1, raison `0f 00 00` ; pile du sac vidée : op 2, raison
  `00 19 00` ; pile déplacée entière : op 1, même exemplaire, raison `00 00 00`) et un résumé
  `43 56` : 6 octets, varint n, n × (exemplaire 8 octets, quantité déplacée u64), conteneur d'arrivée u8 (vu le 04/11/2026). Les options des pièces utilisent les identifiants de la fiche `49 36`.
- Le Pet Meter garde l'inventaire par perso, hors combat, dans `%LOCALAPPDATA%\AION2-pet-meter\inventory.json`.

## Fiche de stats (ajout Pet Meter, décodée le 04/11/2026)

```
49 36  fiche complète   00 00 <n varint> n × (<id u16 LE> <valeur i32 LE signée>) <u32> <u32 0>
                        à la connexion et après chaque téléportation
4a 36  changements      <ton entité varint> <n varint> n × (<id u16> <valeur i32>) <u32> <u32 0>
                        seulement ce qui change (à chaque buff en combat) ; une stat à 0 n'est jamais envoyée
4b 36  autre entité     <entité varint> <n varint> n × (<id u16> <valeur i32>)   (sans les 8 octets de fin)
```

- Pourcentages × 100 (11,1 % → 1110), aucun flottant. Les objets de l'inventaire (`11 56`) utilisent les mêmes identifiants.
- Sûrs : PV max = ⌈0xc1 × (1 + 0xc4 / 10 000)⌉, PM max = ⌈0xc7 × (1 + 0xc5 / 10 000)⌉ (recoupés avec les jauges `00 8d`) ;
  VIT de combat 0x11a, VIT de déplacement 0xcd, Perforation 0x11c ; primaires 1, 2, 4, 5 ; divines 7–11 et 13–17.
- ATQ, DÉF, Précision, Esquive, CRIT, RÉS CRIT et Blocage ne sont pas dans la fiche : le jeu les calcule.
- Le Pet Meter garde la fiche par perso, hors combat, dans `%LOCALAPPDATA%\AION2-pet-meter\ingame-stats.json`
  (`{"chars": {perso: {"at", "version", "stats": {id décimal: valeur}}}}`) ; Aion 2 Builder en reprend PV, PM, VIT et Perforation.

- Marques d'un coup (enregistrement sur mannequin du 28/10/2026, recoupé sort par sort avec le détail de NotMeter) :
  dans l'octet de drapeaux du bloc (switch & 0x02), 0x04 = coup parfait, 0x08 = coup puissant ; dans le switch, 0x20 =
  coups multiples. Ex. Frappes successives : 16 % de parfaits chez les deux ; Coup opportun : 82 % de multiples
  (NotMeter 80 %) ; Faveur de condamnation : puissants seulement, ni parfait ni multiple, chez les deux.
- Sort sur 8 chiffres : un joueur ; les 2 premiers chiffres disent la classe (11 Gladiateur … 18 Aède), les 4 derniers la
  variante (spécialisations). Sur 7 chiffres : un monstre.
- Un combat (au moins 10 s et 10 coups) = tes coups (et ceux de tes invocations : créatures apparues en 41 36, jamais un
  joueur apparu en 45 36 ; maître donné par l'apparition, sinon même échelle et sorts de ta classe) tant qu'ils se suivent
  à moins de 15 s ; on y compte aussi tes lancers, les coups reçus et tes soins (coup de toi sur toi avec valeur).
- Cible principale : parmi les cibles qui ont pris au moins 20 % de tes dégâts sur ta plus frappée, la plus grosse (PV max
  lus en 41 36), sinon la plus frappée. Sa mort (42 36) finit le combat tout de suite (tableau figé, fichier écrit).
- Vue Combat : les coups des autres hors de ton combat sont retenus par cible (sans pause de plus de 15 s) ; quand tu
  frappes une de ces cibles, son combat est compté depuis son premier coup (pas sur un mannequin).
- Chaque combat est écrit dans `%LOCALAPPDATA%\AION2-pet-meter\fights\<date>-<perso>.json` (les 30 derniers, avec la carte) ; Aion 2 Builder les lit.
- Seuls les combats contre un boss (cible principale frappée pendant au moins 25 s et qui prend au moins la moitié de tes
  dégâts), le mannequin (créature 2400032) ou un joueur (cible qui lance des sorts à 8 chiffres) sont écrits, avec
  `kind` = `boss` | `dummy` | `pvp` ; l'option « Aussi les combats ordinaires » écrit le reste en `other`.
- Boss, mannequin et JcJ (fichier `version` 3) : le combat écrit est celui de la cible principale, de ton premier à ton
  dernier coup sur elle (dégâts, coups, sorts, lancers, coups reçus et soins dans cette fenêtre), avec `timeline` (tes
  dégâts sur elle, seconde par seconde) et `whole` (durée et dégâts du combat entier, adds compris ; `offset` = moment du
  premier coup sur elle).
- Au lancement : le journal est ramené à ses 200 derniers Ko s'il dépasse 1 Mo, seuls les 3 derniers enregistrements
  sont gardés, et les combats ordinaires des versions d'avant sont retirés du dossier des combats.
- Diagnostic : `aion2-pet-meter.exe --fights perception-….jsonl.gz [--me <entité>] [--all]` rejoue un enregistrement
  (`--all` : écrit aussi les combats ordinaires, pour voir leur type).
- Disposition établie en lisant les enregistrements et recoupée avec ce que décrivent publiquement les meters de la
  communauté ; aucun code repris.
