"""AION2 Pet Meter - overlay des âmes de familier pour Aion 2, synchronisé avec Aion 2 Todo.

Basé sur AION2-pet-tracker (licence MIT, voir THIRD_PARTY_NOTICES.md). Ajouts : interface
en français, clé de synchro et envoi automatique des âmes à https://aion2-todo-7275.azurewebsites.net.

--- Notes d'origine (AION2-pet-tracker) ---
AION2 pet tracker - оверлей очков духа (питомцев) для Aion 2.

Слушает трафик игры через Npcap (только чтение) и показывает прогресс
по монстрам, которых вы убиваете.

  python aion2_pet_tracker.py                  - оверлей
  python aion2_pet_tracker.py --selftest       - проверка окружения (результат в логе)
  python aion2_pet_tracker.py --replay F.jsonl - прогнать запись tools/aion2_capture.py

Реальные значения приходят от сервера при входе персонажа в мир,
поэтому запускайте оверлей до входа (или перезайдите персонажем).
Между запусками состояние хранится в aion2_pet_tracker_state.json.

Управление: перетаскивание ЛКМ, ПКМ по строке - переименовать/скрыть,
ПКМ по заголовку - меню.
"""
import json, os, queue, struct, sys, threading, time, webbrowser

import lz4.block

FROZEN = getattr(sys, "frozen", False)  # собран в exe (PyInstaller)
# файлы, вшитые в сборку (общий список имён), и папка с данными пользователя
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))


APP_NAME = "AION2 Pet Meter"
SYNC_URL = os.environ.get("AION2_TODO_URL", "https://aion2-todo-7275.azurewebsites.net") + "/api/sync/pets"
DAILY_URL = SYNC_URL.rsplit("/api/", 1)[0] + "/api/sync/daily"  # récoltes et Kinah du jour (tâches du jour d'Aion 2 Todo)


def app_version():
    try:
        return open(os.path.join(RES_DIR, "version.txt"), encoding="utf-8").read().strip() or "dev"
    except OSError:
        return "dev"


VERSION = app_version()


def data_dir():
    """Рядом с программой; если туда нельзя писать - в %LOCALAPPDATA%\\AION2-pet-tracker."""
    try:
        probe = os.path.join(APP_DIR, ".write_test")
        open(probe, "w").close()
        os.remove(probe)
        return APP_DIR
    except OSError:
        d = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "AION2-pet-meter")
        os.makedirs(d, exist_ok=True)
        return d


DATA_DIR = data_dir()
STATE_FILE = os.path.join(DATA_DIR, "aion2_pet_meter_state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "combat_history.json")  # combats gardés pour la fenêtre Historique
LOG_FILE = os.path.join(DATA_DIR, "aion2_pet_meter.log")
NAMES_FILE = os.path.join(RES_DIR, "data", "names.json")
NPC_PETS_FILE = os.path.join(RES_DIR, "data", "npc_pets.json")  # créature -> familier (catalogue Aion 2 Todo)  # имена питомцев, общие для всех игроков
_OLD_STATE = os.path.join(DATA_DIR, "aion2_spirits_state.json")  # файл первых версий
if not os.path.exists(STATE_FILE) and os.path.exists(_OLD_STATE):
    os.replace(_OLD_STATE, STATE_FILE)
NPCAP_URL = "https://npcap.com/#download"

if sys.stderr is None:  # exe без консоли: библиотеки пишут предупреждения в stderr
    sys.stderr = sys.stdout = open(os.devnull, "w")

REQ = [5, 25, 75]  # очков для уровней 1, 2, 3
MAX_LEVEL = len(REQ)

OP_STATE = b"\x00\x90"  # при входе в мир: уровни и очки по всем монстрам
OP_GAIN = b"\x0d\x90"   # убийство: id питомца (вида монстра) + полученные очки
OP_LZ4 = b"\xff\xff"    # сжатая пачка пакетов
# Familier au max : le jeu ne donne plus d'âmes mais des cristaux d'espèce, sans dire de quel monstre.
# On retrouve le monstre par sa mort, juste avant : apparition (code de la créature) puis mort.
OP_SPAWN = b"\x41\x36"    # apparition d'un monstre : <entité varint> ... <code u24> 00 (00|40) 02
OP_DEATH = b"\x42\x36"    # mort : <entité varint> <varint> <raison varint>
OP_CRYSTAL = b"\x11\x90"  # cristaux d'espèce (familier au max) : 01 <espèce u8> <quantité u32>
CRYSTAL_WINDOW = 3.0        # secondes entre la mort et les cristaux
CRYSTAL_GENUS = {1: "fera", 2: "cogni"}  # octet d'espèce observé ; Natura / Varian pas encore vus
# Kinah : le serveur renvoie le solde de la pile de Kinah après chaque kill qui en rapporte.
OP_ITEM = b"\x1b\x56"    # nouvelle quantité d'une pile d'inventaire : 01 ... <objet 4 octets> <quantité u64>
KINA_ITEM = bytes.fromhex("3f337037")
OP_SELF = b"\x33\x36"    # ton perso, à chaque entrée dans une zone : <entité varint> ... <long. u8> <nom>
KINA_WINDOW = 5.0         # secondes entre une mort et les Kinah, pour ne compter que ceux des monstres
KINA_CAP = 1_000_000      # plafond quotidien des Kinah de monstres (serveurs Global / EU)
GATHER_CAP = 1_000        # récoltes par jour et par serveur (notes du 28/10/2026)
GATHER_NEAR = 0.9         # dès 90 % du plafond, la vue Récolte passe en alerte
# Récolte (enregistrements du 28/10/2026) : le compteur du jour de la limite, tenu par le serveur (récoltes commencées,
# ratées comprises), arrive à l'entrée en jeu puis à chaque récolte commencée ; le résultat de chaque récolte ensuite.
OP_GATHER = b"\x33\x8d"        # ta récolte : <résultat u8 : 0 réussie> <u8> <filon varint> <objet 4 octets> <quantité u32>
OP_GATHER_LOGIN = b"\xa7\xff"  # à l'entrée en jeu, avant ton perso : <récoltes du jour u64> <heure ms u64>
OP_GATHER_TALLY = b"\xa8\xff"  # à chaque récolte commencée : <récoltes du jour u64> <heure ms u64>
# Ressources de récolte par numéro d'objet du jeu (les 4 octets « objet » de 33 8d, en u32 little-endian) : (fr, en, ru).
# Numéros d'après aion2hub.com, noms du jeu d'après metabot.gg (30/10/2026). Le dernier chiffre est le rang : une récolte
# supérieure donne l'objet suivant (610620013 Minerai d'orichalque → 610620014 … d'une grande pureté).
GATHER_ITEMS = {
    610610013: ("Od", "Odyle", "Эфир"),
    610610014: ("Od d'une grande pureté", "Fine Odyle", "Эфир высокой чистоты"),
    610610015: ("Od pur", "Pure Odyle", "Чистый эфир"),
    610610016: ("Od éclatant", "Radiant Odyle", "Сияющий эфир"),
    610620013: ("Minerai d'orichalque", "Orichalcum Ore", "Орихарконовая руда"),
    610620014: ("Minerai d'orichalque d'une grande pureté", "Fine Orichalcum Ore", "Орихарконовая руда высокой чистоты"),
    610620015: ("Minerai d'orichalque pur", "Pure Orichalcum Ore", "Чистая орихарконовая руда"),
    610620016: ("Minerai d'orichalque éclatant", "Radiant Orichalcum Ore", "Сияющая орихарконовая руда"),
    610650011: ("Bois d'Yggdrasil", "Yggdrasil Log", "Древесина Иггдрасиля"),
    610650012: ("Bois d'Asvata", "Asvata Log", "Асватовая древесина"),
    610650013: ("Bois d'Yggdrasil d'une grande pureté", "Fine Yggdrasil Log", "Древесина Иггдрасиля высокой чистоты"),
    610650014: ("Bois d'Asvata d'une grande pureté", "Fine Asvata Log", "Асватовая древесина высокой чистоты"),
    610650015: ("Bois d'Yggdrasil pur", "Pure Yggdrasil Log", "Чистая древесина Иггдрасиля"),
    610650016: ("Bois d'Asvata pur", "Pure Asvata Log", "Чистая асватовая древесина"),
    610650017: ("Bois d'Yggdrasil éclatant", "Radiant Yggdrasil Log", "Сияющая древесина Иггдрасиля"),
    610650018: ("Bois d'Asvata éclatant", "Radiant Asvata Log", "Сияющая асватовая древесина"),
    610660025: ("Saphir brut", "Sapphire Gemstone", "Самородок сапфира"),
    610660026: ("Diamant brut", "Diamond Gemstone", "Самородок алмаза"),
    610660027: ("Rubis brut", "Ruby Gemstone", "Самородок рубина"),
    610660028: ("Saphir brut d'une grande pureté", "Fine Sapphire Gemstone", "Самородок сапфира высокой чистоты"),
    610660029: ("Diamant brut d'une grande pureté", "Fine Diamond Gemstone", "Самородок алмаза высокой чистоты"),
    610660030: ("Rubis brut d'une grande pureté", "Fine Ruby Gemstone", "Самородок рубина высокой чистоты"),
    610660031: ("Saphir brut pur", "Pure Sapphire Gemstone", "Чистый самородок сапфира"),
    610660032: ("Diamant brut pur", "Pure Diamond Gemstone", "Чистый самородок алмаза"),
    610660033: ("Rubis brut pur", "Pure Ruby Gemstone", "Чистый самородок рубина"),
    610660034: ("Saphir brut éclatant", "Radiant Sapphire Gemstone", "Сияющий самородок сапфира"),
    610660035: ("Diamant brut éclatant", "Radiant Diamond Gemstone", "Сияющий самородок алмаза"),
    610660036: ("Rubis brut éclatant", "Radiant Ruby Gemstone", "Сияющий самородок рубина"),
    610670041: ("Aria", "Aria", "Вереск"),
    610670042: ("Targena", "Targena", "Полынь"),
    610670043: ("Azpha", "Azpha", "Астильба"),
    610670044: ("Calendula", "Calendula", "Календула"),
    610670045: ("Aria fraîche", "Fresh Aria", "Свежий вереск"),
    610670046: ("Targena fraîche", "Fresh Targena", "Свежая полынь"),
    610670047: ("Azpha fraîche", "Fresh Azpha", "Свежая астильба"),
    610670048: ("Calendula fraîche", "Fresh Calendula", "Свежая календула"),
    610680043: ("Coriolus", "Coriolus", "Кориолус"),
    610680044: ("Kukuru", "Kukuru", "Кукур"),
    610680045: ("Méla", "Mela", "Мелла"),
    610680047: ("Inina", "Inina", "Шери"),
    610680048: ("Cypri", "Cypri", "Килли"),
    610680049: ("Morfa", "Morfa", "Морфа"),
    610680050: ("Ohkra", "Ohkra", "Окра"),
    610680051: ("Raydam", "Raydam", "Рейдам"),
    610680053: ("Conide", "Conide", "Конидэ"),
    610680054: ("Pujery", "Pujery", "Фуджери"),
}
# Joueurs autour de toi (enregistrement du 28/10/2026) : nom et puissance de combat, pour le tableau de combat.
OP_PLAYER = b"\x45\x36"   # un joueur apparaît : <entité varint> ... <long. u8> <nom> ... f4 cb 1f ... <puissance u32> (fin)
OP_POWER = b"\x56\x36"    # ta puissance de combat : <puissance u64> <u64>
GATHER_READY = True
TABS_H = 26               # hauteur des onglets Familiers / Récolte
HIDDEN_ALPHA = 0.02       # overlay caché (masquage auto) : presque invisible, il reçoit encore le survol
HIDE_DELAYS = (3, 5, 10, 30, 60)  # secondes sans butin avant de se cacher (menu Masquage auto)
DAILY_RESET_H = 7         # reset quotidien à 07:00 UTC (comme Aion 2 Todo)


LOG_MAX = 1_000_000     # au-delà, le journal est raccourci…
LOG_KEEP = 200_000      # … à ses dernières lignes
RECORDINGS_KEEP = 3     # enregistrements perception-*.jsonl.gz gardés (les plus récents)
_log_writes = [0]


def trim_log():
    """Garde la fin du journal s'il dépasse LOG_MAX (il grossissait sans fin)."""
    try:
        if os.path.getsize(LOG_FILE) <= LOG_MAX:
            return
        with open(LOG_FILE, "rb") as f:
            f.seek(-LOG_KEEP, os.SEEK_END)
            tail = f.read()
        tail = tail[tail.find(b"\n") + 1:]
        with open(LOG_FILE, "wb") as f:
            f.write(tail)
    except OSError:
        pass


def log(*a):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + " ".join(map(str, a)) + "\n")
    _log_writes[0] += 1
    if _log_writes[0] % 500 == 0:
        trim_log()


def tidy(all_fights=False):
    """Ménage au lancement : journal raccourci, seuls les derniers enregistrements gardés, et les combats ordinaires
    (packs de monstres) des versions d'avant retirés du dossier des combats, sauf si on les garde tous."""
    trim_log()
    try:
        recordings = sorted(n for n in os.listdir(DATA_DIR) if n.startswith("perception-") and n.endswith(".jsonl.gz"))
        for name in recordings[:-RECORDINGS_KEEP]:
            os.remove(os.path.join(DATA_DIR, name))
    except OSError:
        pass
    if all_fights or not os.path.isdir(FIGHTS_DIR):
        return
    for name in os.listdir(FIGHTS_DIR):
        path = os.path.join(FIGHTS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                fight = json.load(f)
            kind = fight.get("kind")
            if kind is None:  # avant la v0.7.3 : même règle que le Builder (mannequin, ou cible principale ≥ 25 s et ≥ 50 %)
                target = fight.get("target") or {}
                kind = ("dummy" if target.get("code") in TRAINING_DUMMIES else
                        "boss" if target.get("code") and fight.get("duration", 0) >= BOSS_MIN_SPAN
                        and target.get("damage", 0) >= BOSS_MIN_SHARE * fight.get("damage", 0) else "other")
            if kind == "other":
                os.remove(path)
        except (OSError, ValueError):
            pass


def npcap_installed():
    """Npcap ставит wpcap.dll в System32\\Npcap; без него перехват невозможен."""
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Npcap")
    return os.path.isfile(os.path.join(sysdir, "wpcap.dll"))


# Языки клиента Aion 2 (папки L10N\Text): имена питомцев у каждого свои.
GAME_LANGS = {"en": "English", "ru": "Русский", "de": "Deutsch", "fr": "Français",
              "es": "Español", "pt": "Português", "ko": "한국어", "ja": "日本語"}


def load_shared_names():
    """{язык: {id: имя}} из общего файла, вшитого в сборку."""
    try:
        d = json.load(open(NAMES_FILE, encoding="utf-8"))
        return {lang: {int(k): v for k, v in names.items()} for lang, names in d.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------- протокол

def read_varint(b, i):
    v = s = 0
    while True:
        if i >= len(b):
            raise IndexError
        x = b[i]; i += 1
        v |= (x & 0x7F) << s; s += 7
        if not x & 0x80:
            return v, i


def split_frames(buf):
    """Режет буфер на кадры. Возвращает (кадры, остаток) или (кадры, None) при мусоре."""
    i, out = 0, []
    while i < len(buf):
        try:
            n, j = read_varint(buf, i)
        except IndexError:
            break
        total = n - 4 + (j - i)
        if total < (j - i) + 2 or total > 4_000_000:
            return out, None
        if i + total > len(buf):
            break
        out.append(buf[i:i + total])
        i += total
    return out, buf[i:]


def unpack(frame):
    """Разворачивает сжатые кадры; отдаёт (opcode, payload)."""
    _, j = read_varint(frame, 0)
    op, body = frame[j:j + 2], frame[j + 2:]
    if op == OP_LZ4:
        size = int.from_bytes(body[:4], "little")
        try:
            raw = lz4.block.decompress(body[4:], uncompressed_size=size)
        except Exception as e:
            log("lz4:", e)
            return
        inner, _ = split_frames(raw)
        for f in inner:
            yield from unpack(f)
    else:
        yield op, body


def parse_state(p):
    _, i = struct.unpack_from("<I", p, 0)[0], 4
    n1, i = read_varint(p, i)
    levels = {}
    for _ in range(n1):
        mid, mid2, lvl = struct.unpack_from("<III", p, i); i += 12
        if mid != mid2 or lvl > 10:
            raise ValueError("уровни: неожиданный формат")
        levels[mid] = lvl
    n2, i = read_varint(p, i)
    points = {}
    for _ in range(n2):
        mid, pts = struct.unpack_from("<II", p, i); i += 8
        points[mid] = pts
    return levels, points


def parse_spawn(p):
    """(entité, code de la créature) ou None."""
    eid, i = read_varint(p, 0)
    for k in range(i + 3, min(len(p) - 2, i + 60)):
        if p[k] == 0 and p[k + 1] in (0x00, 0x40) and p[k + 2] == 0x02:
            return eid, p[k - 3] | p[k - 2] << 8 | p[k - 1] << 16
    return None


def parse_max_hp(p):
    """PV max d'une créature qui apparaît (41 36), ou None : après le code et la position, <PV varint> <PV max varint>
    suivis de 64 00 00 00 64 00 00 00 (boss : 8 208 000 PV, premier relevé 8 040 938, enregistrement du 28/10/2026)."""
    _, i = read_varint(p, 0)
    start = next((k + 3 for k in range(i + 3, min(len(p) - 2, i + 60)) if p[k] == 0 and p[k + 1] in (0x00, 0x40) and p[k + 2] == 0x02), None)
    if start is None:
        return None
    for j in range(start, min(len(p) - 10, start + 40)):
        try:
            hp, k = read_varint(p, j)
            top, k = read_varint(p, k)
        except IndexError:
            return None
        if hp == top >= 10_000 and p[k:k + 8] == b"\x64\x00\x00\x00\x64\x00\x00\x00":
            return top
    return None


def parse_owner(p):
    """(invocation, maître) d'une apparition (41 36), ou None : après 8 octets ff et 8 octets, l'entité qui suit est
    l'apparu lui-même, ou le maître pour une invocation."""
    eid, _ = read_varint(p, 0)
    k = p.find(b"\xff" * 8)
    if k < 0 or k + 17 > len(p):
        return None
    owner, _ = read_varint(p, k + 16)
    return (eid, owner) if owner != eid else None


def parse_player(p):
    """(entité, nom, puissance de combat ou None) d'un joueur qui apparaît près de toi (45 36), ou None. La puissance
    est 11 octets après le dernier f4 cb 1f, vers la fin du message."""
    name, _ = parse_self(p)
    if not name:
        return None
    k = p.rfind(b"\xf4\xcb\x1f")
    power = int.from_bytes(p[k + 11:k + 15], "little") if 0 <= k and k + 15 <= len(p) else 0
    return read_varint(p, 0)[0], name, (power if 1_000 <= power < 10_000_000 else None)


def parse_death(p):
    eid, _ = read_varint(p, 0)
    return eid


def parse_crystal(p):
    """(espèce, quantité) ou None."""
    if len(p) < 6 or p[0] != 1:
        return None
    amount = int.from_bytes(p[2:6], "little")
    return (p[1], amount) if 0 < amount < 1000 else None


def parse_kina(p):
    """Solde de Kinah, ou None si la pile n'est pas celle des Kinah."""
    if len(p) < 23 or p[0] != 1 or p[11:15] != KINA_ITEM:
        return None
    return int.from_bytes(p[15:23], "little")


def parse_self(p):
    """(nom, serveur) de ton perso : chaîne préfixée par sa longueur juste après l'entité, puis le n° de serveur (u16)."""
    _, pos = read_varint(p, 0)
    for k in range(pos, min(pos + 12, len(p))):
        n = p[k]
        if 1 <= n <= 32 and k + 1 + n <= len(p):
            try:
                name = p[k + 1:k + 1 + n].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name.isalnum():
                end = k + 1 + n
                return name, (int.from_bytes(p[end:end + 2], "little") if end + 2 <= len(p) else None)
    return None, None


def parse_gather(p):
    """(objet, quantité, ratée, supérieure) d'une de tes récoltes, ou None. Résultat (1er octet) : 0 réussie, 1 réussie au
    rang supérieur (l'objet du rang au-dessus, ajouté à l'inventaire), 2 ratée (quantité 0, rien ajouté) ; enregistrement du
    29/10/2026. Une récolte n'est ratée que sans objet : le 1 n'est pas un échec."""
    if len(p) < 12:
        return None
    _, i = read_varint(p, 2)
    if i + 8 > len(p):
        return None
    qty = int.from_bytes(p[i + 4:i + 8], "little")
    failed = qty == 0 or p[0] == 2
    return (p[i:i + 4].hex(), qty, failed, p[0] == 1 and not failed) if qty < 10_000 else None


def game_day(t=None):
    """Jour de jeu (le reset quotidien est à 07:00 UTC)."""
    return time.strftime("%Y-%m-%d", time.gmtime((t or time.time()) - DAILY_RESET_H * 3600))


def load_npc_pets():
    try:
        d = json.load(open(NPC_PETS_FILE, encoding="utf-8"))["npcs"]
        return {int(k): [(int(pid), g) for pid, g in v] for k, v in d.items()}
    except Exception as e:
        log("npc_pets:", repr(e))
        return {}


def parse_gain(p):
    kind, mid, amount = struct.unpack_from("<BII", p, 0)
    return kind, mid, amount


class Stream:
    """Сборка TCP-потока сервер->клиент с учётом повторов и перестановок."""
    MASK, HALF = 0xFFFFFFFF, 0x80000000

    def __init__(self):
        self.next = None
        self.pending = {}
        self.buf = b""
        self.resync = True  # захват мог начаться с середины кадра

    def add(self, seq, data):
        if self.next is None:
            self.next = seq
        if len(data) > len(self.pending.get(seq, b"")):
            self.pending[seq] = data
        out = bytearray()
        moved = True
        while moved:
            moved = False
            for s in list(self.pending):
                d = (s - self.next) & self.MASK
                if d != 0 and d < self.HALF:
                    continue
                dat = self.pending.pop(s)
                skip = 0 if d == 0 else (self.next - s) & self.MASK
                if skip < len(dat):
                    out += dat[skip:]
                    self.next = (self.next + len(dat) - skip) & self.MASK
                    moved = True
        if len(self.pending) > 8:  # сегмент потерян при захвате: перескакиваем дырку
            log("gap, resync")
            self.buf, self.resync = b"", True
            self.next = min(self.pending, key=lambda s: (s - self.next) & self.MASK)
            return self.add(self.next, self.pending.pop(self.next))
        self.buf += bytes(out)
        if self.resync:
            off = find_sync(self.buf)
            if off is None:
                if len(self.buf) > 65536:
                    self.buf = b""
                return []
            self.buf, self.resync = self.buf[off:], False
        frames, rest = split_frames(self.buf)
        if rest is None:
            self.buf, self.resync = b"", True
            return frames
        self.buf = rest
        for f in frames:
            KNOWN_OPS.add(frame_op(f))
        return frames


# Коды операций, встречавшиеся в потоке; пополняется на лету. По ним после
# потери сегмента находим начало следующего кадра.
KNOWN_OPS = {bytes.fromhex(h) for h in (
    "0036 1d37 1c37 0438 008d 0238 0638 1a37 1b37 4b36 0338 0138 3b38 3d38 2a37 0191 2937 8456 "
    "2a38 218d ffff 2b38 2c38 1b56 3538 4738 4136 1838 1938 0336 3438 3138 1f56 4a36 3436 4236 "
    "1e56 4736 1f37 2156 2037 0d90 3237 048d 2237 3536 0090 1190").split()}


def frame_op(frame):
    _, j = read_varint(frame, 0)
    return frame[j:j + 2]


def find_sync(buf, need=8):
    """Смещение, с которого подряд идут need кадров с известными кодами
    (или кадры с известными кодами ровно до конца буфера)."""
    n = len(buf)
    for off in range(n):
        i, good = off, 0
        while good < need:
            try:
                ln, j = read_varint(buf, i)
            except IndexError:
                break
            total = ln - 4 + (j - i)
            if total < (j - i) + 2 or buf[j:j + 2] not in KNOWN_OPS:
                good = -1
                break
            if i + total > n:
                break
            i += total
            good += 1
        if good >= need or (good > 0 and i == n):
            return off
    return None


class Recorder:
    """Enregistrement de diagnostic, lancé depuis le menu : les messages du serveur tels que le Pet Meter les lit
    déjà (décompressés), avec l'heure, dans un .jsonl.gz du dossier de données. Sert à trouver comment le jeu
    décrit la perception d'espèce. Rien n'est envoyé : le fichier reste sur le PC."""
    DURATION = 5 * 60
    # Dernière entrée en jeu, gardée en mémoire même sans enregistrement (c'est là que le jeu envoie les collections) :
    # les BACK secondes avant le message 00 90 et les AFTER secondes après. Écrite en tête du prochain enregistrement.
    BACK, AFTER = 15, 45

    def __init__(self):
        import collections
        self.lock = threading.Lock()
        self.f = self.path = None
        self.t0 = self.until = 0.0
        self.marks = 0
        self.recent = collections.deque()  # (heure, opcode, message) des dernières secondes
        self.login = None                  # entrée en jeu gardée
        self.login_until = 0.0

    def keep(self, op, p):
        """Hors enregistrement : garde les dernières secondes, et l'entrée en jeu complète quand elle passe."""
        now = time.time()
        if self.login is not None and now < self.login_until:
            self.login.append((now, op, p))
            return
        self.recent.append((now, op, p))
        while self.recent and now - self.recent[0][0] > self.BACK:
            self.recent.popleft()
        if op == OP_STATE:
            self.login = list(self.recent)
            self.login_until = now + self.AFTER

    @property
    def active(self):
        return self.f is not None

    def remaining(self):
        return max(0, int(self.until - time.time())) if self.f else 0

    def start(self):
        import gzip
        with self.lock:
            if self.f:
                return
            self.path = os.path.join(DATA_DIR, time.strftime("perception-%Y%m%d-%H%M%S.jsonl.gz"))
            self.f = gzip.open(self.path, "wt", encoding="utf-8")
            self.t0, self.marks = time.time(), 0
            self.until = self.t0 + self.DURATION
            login = list(self.login or [])
            self.f.write(json.dumps({"version": VERSION, "start": time.strftime("%Y-%m-%d %H:%M:%S"), "login": len(login)}) + "\n")
            # La dernière entrée en jeu d'abord (heures négatives : avant le début de l'enregistrement).
            for t, op, p in login:
                self.f.write(json.dumps({"t": round(t - self.t0, 3), "op": op.hex(), "p": p.hex(), "login": 1}) + "\n")
        log("enregistrement :", self.path, "| entrée en jeu gardée :", len(login), "messages")

    def mark(self):
        with self.lock:
            if self.f:
                self.marks += 1
                self.f.write(json.dumps({"t": round(time.time() - self.t0, 3), "mark": self.marks}) + "\n")

    def frame(self, op, p):
        if not self.f:  # le cas courant, sans verrou
            self.keep(op, p)
            return
        with self.lock:
            if self.f:
                self.f.write(json.dumps({"t": round(time.time() - self.t0, 3), "op": op.hex(), "p": p.hex()}) + "\n")

    def stop(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None
                log("enregistrement terminé :", self.path)


RECORDER = Recorder()


# ---------------------------------------------------------------- combats

OP_HIT = b"\x04\x38"    # un coup : <cible> <switch> <varint> <attaquant> <sort u32> <octet> <type> ... (voir docs/PROTOCOL.md)
OP_CAST = b"\x02\x38"   # un lancer : <lanceur> 00 <sort u32> ...
OP_STATS = b"\x4a\x36"  # tes stats, envoyées régulièrement : <ton entité> ... (dit qui est « toi »)
OP_MAP = b"\x21\x36"    # chargement d'une carte : <u32> <carte u32> ... (un donjon garde la même d'un boss à l'autre)
FIGHT_GAP = 15.0        # secondes sans coup donné : le combat est fini (ou tout de suite, à la mort de la cible principale)
MAIN_MIN_SHARE = 0.2    # cible principale : la plus grosse de celles qui ont pris au moins 20 % de tes dégâts sur ta plus frappée
PRE_TARGETS = 300       # cibles retenues hors combat (coups des autres), pour reprendre leur combat si tu le rejoins
FIGHT_MIN_HITS = 10     # en dessous : pas un combat (un monstre achevé en passant)
FIGHT_MIN_SECONDS = 10
FIGHTS_KEEP = 30        # combats gardés dans le dossier (Aion 2 Builder en garde 20 par perso)
TRAINING_DUMMIES = {2400032}  # mannequins d'entraînement (code de créature)
BOSS_MIN_SPAN = 25.0    # boss : la cible principale encaisse tes coups pendant au moins 25 s…
BOSS_MIN_SHARE = 0.5    # … et prend au moins la moitié de tes dégâts (un pack de monstres meurt plus vite et se partage)
FIGHTS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "AION2-pet-meter", "fights")


# Marques d'un coup (enregistrement du 28/10/2026, recoupé sort par sort avec le détail de NotMeter) : dans l'octet de
# drapeaux du bloc, 0x04 = coup parfait, 0x08 = coup puissant ; dans le switch, 0x20 = coups multiples.
HIT_PERFECT, HIT_POWERFUL, HIT_MULTI = 1, 2, 4
HIT_COUNTS = ("hits", "crits", "back", "perfect", "powerful", "multi")  # ce que le tableau compte pour chaque coup


def parse_hit(p):
    """Un coup : (cible, attaquant, sort, critique, angle, échelle, dégâts, marques) ; angle 1 = de dos, 2 = de face, None =
    inconnu ; marques : HIT_PERFECT (coup parfait), HIT_POWERFUL (coup puissant), HIT_MULTI (coups multiples).
    Les enregistrements sans valeur (esquive, résistance, marque) ne comptent pas. None si le message ne se lit pas ou n'est pas un coup."""
    try:
        target, i = read_varint(p, 0)
        switch, i = read_varint(p, i)
        _, i = read_varint(p, i)
        actor, i = read_varint(p, i)
        skill = int.from_bytes(p[i:i + 4], "little")
        i += 5  # sort, puis un octet
        kind, i = read_varint(p, i)
        low = switch & 0x0F
        if low in (0, 2):  # sans valeur : esquive, résistance, ou marque sur le coup précédent - pas un coup (recoupé avec NotMeter)
            return None
        if not 4 <= low <= 7:
            return None
        skip = {5: 12, 6: 10, 7: 14}.get(low, 8)
        angle = None
        marks = HIT_MULTI if switch & 0x20 else 0
        if low & 2:  # drapeaux, PV rendus (varint), angle : la longueur du varint décale la suite
            marks |= (HIT_PERFECT if p[i] & 0x04 else 0) | (HIT_POWERFUL if p[i] & 0x08 else 0)
            j = i + 1
            _, j = read_varint(p, j)
            angle = p[j]
            skip += j - i - 2
        i += skip
        scale, i = read_varint(p, i)
        damage, i = read_varint(p, i)
        if scale == 0:  # bourrage : l'échelle et les dégâts viennent après
            scale, damage = damage, read_varint(p, i)[0]
        return target, actor, skill, kind == 3, angle, scale, damage, marks
    except (IndexError, ValueError):
        return None


def parse_cast(p):
    """Un lancer : (lanceur, sort, forme) ou None. Forme 2 : le lancer sur sa cible (cible et position suivent) ;
    0 : sa suite, 0,15 à 0,4 s après. Chaque utilisation envoie en général les deux."""
    try:
        actor, i = read_varint(p, 0)
        return actor, int.from_bytes(p[i + 1:i + 5], "little"), p[i + 6] if len(p) > i + 6 else -1
    except IndexError:
        return None


def scale_sig(scale):
    """Signature d'une échelle de puissance : ses 3 derniers chiffres. Les milliers bougent pendant le combat (11 714,
    13 714…), pas eux, et les invocations d'un joueur ont les mêmes que lui (enregistrement du 28/10/2026). None pour une
    échelle ronde (10 000 : commune à plusieurs joueurs)."""
    return scale % 1000 or None


def skill_base(code):
    """Sort sans sa variante : les 4 derniers chiffres (spécialisations) ne changent pas le sort."""
    return code // 10000 * 10000 if code >= 10_000_000 else code


def main_target(damage, maxhp):
    """Cible principale d'un combat : parmi celles qui ont pris au moins 20 % de tes dégâts sur ta cible la plus frappée, la
    plus grosse (PV max lus à son apparition, 41 36) : le boss, pas un add que tu tapes longtemps. PV inconnus : la plus
    frappée. None sans cible."""
    if not damage:
        return None
    top = max(damage.values())
    return max((t for t, d in damage.items() if d >= MAIN_MIN_SHARE * top), key=lambda t: (maxhp.get(t, 0), damage[t]))


class Fights:
    """Tes combats, pour Aion 2 Builder : tes lancers et tes coups (ceux de tes invocations compris : même échelle de
    puissance, sorts de ta classe), les coups reçus et tes soins, tant qu'ils se suivent à moins de 15 s. Chaque combat
    est écrit dans %LOCALAPPDATA%\\AION2-pet-meter\\fights. Rien n'est envoyé."""

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = True
        self.me = None        # ton entité (stats 4a 36, ou entrée dans une zone 33 36)
        self.name = ""        # ton perso (33 36, ou le dernier connu du Tracker)
        self.scale = None     # ton échelle de puissance (tes invocations ont la même)
        self.prefix = None    # 2 premiers chiffres de tes sorts (ta classe)
        self.fight = None
        self.recent_casts = []  # tes derniers lancers, pour garder ceux d'avant le premier coup
        self.emit = lambda ev: None
        self.last_path = None
        self.pinned = False   # diagnostic (--fights --me) : « toi » imposé, les stats du jeu ne le changent pas
        self.map = 0          # carte en cours (21 36)
        self.players = set()  # entités vues avec un sort de joueur (8 chiffres) : une cible joueur = combat JcJ
        self.all_kinds = False  # False : seuls les boss, mannequins et combats JcJ sont écrits

    def set_me(self, entity, name=None):
        with self.lock:
            if entity and entity != self.me and not self.pinned:
                self.flush()
                self.me, self.scale, self.prefix = entity, None, None
            if name:
                self.name = name

    def stats(self, p):
        try:
            self.set_me(read_varint(p, 0)[0])
        except IndexError:
            pass

    def tick(self, now):
        with self.lock:
            if self.fight and now - self.fight["last"] > FIGHT_GAP:
                self.flush()

    def mine(self, actor, skill, scale, mobs):
        """Coup à toi : le tien, ou celui d'une de tes invocations : une créature (apparue en 41 36) rattachée à toi par son
        apparition, sinon de même échelle de puissance que toi avec un sort de ta classe. Jamais un joueur : un autre joueur
        de ta classe peut avoir la même échelle que toi (une valeur ronde comme 10 000), ses coups ouvraient alors un combat
        « à toi » sur sa cible."""
        if actor == self.me:
            return True
        owner = LIVE.owners.get(actor)
        if owner is not None:
            return owner in ("me", self.me)
        return (actor in mobs and actor not in LIVE.names and self.scale is not None and scale == self.scale
                and self.prefix is not None and skill >= 10_000_000 and skill // 1_000_000 == self.prefix)

    def seen(self, actor, skill):
        """Un sort de joueur (8 chiffres) : l'entité est un joueur."""
        if skill >= 10_000_000:
            if len(self.players) > 5000:
                self.players.clear()
            self.players.add(actor)

    def cast(self, p, now=None):
        r = parse_cast(p)
        if r:
            self.seen(r[0], r[1])
            LIVE.cast(r[0], r[1], self.me)
        if not r or not self.enabled or self.me is None or r[0] != self.me:
            return
        now = time.time() if now is None else now
        with self.lock:
            code = skill_base(r[1])
            if self.fight:
                self.fight["casts"].append((now, code, r[2]))
                self.fight["last"] = max(self.fight["last"], now)
            else:
                self.recent_casts = [c for c in self.recent_casts if now - c[0] < 5] + [(now, code, r[2])]

    def hit(self, p, mobs, now=None):
        h = parse_hit(p)
        if not h or self.me is None:
            return
        target, actor, skill, crit, angle, scale, damage, marks = h
        now = time.time() if now is None else now
        self.seen(actor, skill)
        # Compteur de dégâts en direct : tous les joueurs, même si l'écriture des combats est coupée.
        LIVE.hit(target, actor, skill, crit, scale, damage, now, actor != target and self.mine(actor, skill, scale, mobs), mobs, self.me, angle, marks)
        if not self.enabled:
            return
        with self.lock:
            if actor == self.me and 1_000 <= scale <= 200_000:
                self.scale = scale
            if actor == self.me and skill >= 10_000_000:
                self.prefix = skill // 1_000_000
            f = self.fight
            if actor != target and self.mine(actor, skill, scale, mobs):
                if f is None:
                    f = self.fight = {"first": now, "last": now, "hits": [], "taken": [], "heals": [], "casts": list(self.recent_casts),
                                      "targets": {}, "codes": {}, "spans": {}, "map": self.map}
                    self.recent_casts = []
                f["last"] = now
                # Chaque coup avec son heure et sa cible : le combat écrit sera celui de la cible principale (le boss).
                f["hits"].append((now, target, skill_base(skill), damage, crit, angle, actor != self.me))
                f["targets"][target] = f["targets"].get(target, 0) + damage
                span = f["spans"].setdefault(target, [now, now])  # premier et dernier coup sur cette cible
                span[1] = now
                if target not in f["codes"] and target in mobs:  # code de la créature, gardé : sa mort l'efface des apparitions
                    f["codes"][target] = mobs[target]
            elif f is not None and target == self.me and actor != self.me:
                f["taken"].append((now, damage))
            elif f is not None and actor == target == self.me and damage > 1:
                f["heals"].append((now, damage))

    def died(self, entity):
        """Mort de ta cible principale : le combat est écrit tout de suite, sans attendre 15 s ni y mêler le pack suivant."""
        with self.lock:
            if self.fight and entity == main_target(self.fight["targets"], LIVE.maxhp):
                self.flush()

    def kind(self, f, main, total):
        """Mannequin (code connu), JcJ (la cible lance des sorts de joueur), boss (cible principale frappée pendant au moins
        25 s et qui prend au moins la moitié de tes dégâts) ou « other » : un pack de monstres, un monstre achevé."""
        target, damage = main
        code = f["codes"].get(target, 0)
        first, last = f["spans"].get(target, (0, 0))
        if code in TRAINING_DUMMIES:
            return "dummy"
        if target in self.players:
            return "pvp"
        if code and last - first >= BOSS_MIN_SPAN and damage >= BOSS_MIN_SHARE * total:
            return "boss"
        return "other"

    def flush(self):
        """Écrit le combat en cours (appelé sous verrou) s'il en est un : boss, mannequin ou JcJ (tous si all_kinds).
        Boss, mannequin et JcJ : ce qui est écrit, c'est la cible principale, de ton premier à ton dernier coup sur elle
        (les monstres d'avant et les adds ne comptent pas), avec tes dégâts seconde par seconde ; le combat entier en rappel."""
        f, self.fight = self.fight, None
        if not f or len(f["hits"]) < FIGHT_MIN_HITS or f["last"] - f["first"] < FIGHT_MIN_SECONDS:
            return
        total = sum(h[3] for h in f["hits"])
        target = main_target(f["targets"], LIVE.maxhp)
        main = (target, f["targets"][target]) if target is not None else (0, 0)
        kind = self.kind(f, main, total)
        if kind == "other" and not self.all_kinds:
            log("combat non gardé (ni boss, ni mannequin, ni JcJ) :", f"{f['last'] - f['first']:.0f} s", total, "dégâts")
            return
        hits = f["hits"] if kind == "other" else [h for h in f["hits"] if h[1] == main[0]]
        first, last = (f["first"], f["last"]) if kind == "other" else (hits[0][0], hits[-1][0])
        duration = max(1.0, last - first)
        skills = {}
        for t, _, code, damage, crit, angle, summon in hits:
            s = skills.setdefault(code, {"hits": 0, "damage": 0, "crits": 0, "back": 0, "front": 0, "positioned": 0, "max": 0, "summon": summon})
            s["hits"] += 1
            s["damage"] += damage
            s["crits"] += crit
            s["max"] = max(s["max"], damage)
            if angle in (0, 1, 2):
                s["positioned"] += 1
                s["back"] += angle == 1
                s["front"] += angle == 2
        casts = [c for c in f["casts"] if first - 2.0 <= c[0] <= last + 0.5]
        taken = [d for t, d in f["taken"] if first <= t <= last]
        heals = [d for t, d in f["heals"] if first <= t <= last]
        timeline = [0] * (int(last - first) + 1)
        for h in hits:
            timeline[int(h[0] - first)] += h[3]
        damage = sum(h[3] for h in hits)
        fight = {
            "version": 3, "kind": kind, "meter": f"Pet Meter {VERSION}", "character": self.name, "entity": self.me,
            "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(first)),
            "startUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(first)),
            "duration": round(duration, 1), "damage": damage, "hits": len(hits),
            "target": {"entity": main[0], "code": f["codes"].get(main[0], 0), "damage": main[1]},
            "skills": [dict(code=c, **s) for c, s in sorted(skills.items(), key=lambda kv: -kv[1]["damage"])],
            "casts": [[round(t - first, 2), c, form] for t, c, form in casts],
            "taken": {"damage": sum(taken), "hits": len(taken), "max": max(taken, default=0)},
            "heals": {"amount": sum(heals), "hits": len(heals)}, "map": f.get("map", self.map),
            "timeline": timeline,
            "whole": {"duration": round(max(1.0, f["last"] - f["first"]), 1), "damage": total, "offset": round(first - f["first"], 1)},
        }
        try:
            os.makedirs(FIGHTS_DIR, exist_ok=True)
            name = "".join(ch for ch in self.name if ch.isalnum()) or "perso"
            path = os.path.join(FIGHTS_DIR, time.strftime("%Y%m%d-%H%M%S", time.localtime(first)) + f"-{name}.json")
            with open(path, "w", encoding="utf-8") as out:
                json.dump(fight, out, ensure_ascii=False)
            self.last_path = path
            for old in sorted(os.listdir(FIGHTS_DIR), reverse=True)[FIGHTS_KEEP:]:  # les plus anciens au-delà de 30
                if old.endswith(".json"):
                    os.remove(os.path.join(FIGHTS_DIR, old))
            log("combat :", kind, path, f"{duration:.0f} s", damage, "dégâts")
            self.emit(("fight", round(duration), round(damage / duration)))
        except OSError as e:
            log("combat non écrit :", e)


FIGHTS = Fights()

LIVE_BOSS_SPAN = 8.0      # vue Combat : ta cible principale frappée depuis 8 s (un monstre de farm meurt avant)
STALE_SECONDS = 10.0     # vue Combat : un joueur sans coup sur la cible depuis 10 s passe en gris (sorti de portée, mort…)
HISTORY_KEEP = 50        # combats gardés dans l'historique (fenêtre Historique, fichier combat_history.json)
SKILLS_FILE = os.path.join(RES_DIR, "data", "skills.json")  # noms, icônes des sorts et classe par préfixe (armurerie officielle)
SKILL_ICON_URL = "https://assets.playnccdn.com/static-aion2-gamedata/resources/{}.png"  # icônes officielles des sorts
# Réglages du tableau de combat (menu ⋯ → Combat), calqués sur ceux de NotMeter : colonnes affichées, formats, couleurs
# des barres par classe, lignes, largeur et hauteur, temps gardé après le combat (0 = jusqu'au prochain combat).
COMBAT_DEFAULTS = {"damage": False, "dps": True, "share": True, "dps_full": True, "damage_short": True, "class_colors": True,
                   "power": True, "share_basis": "group", "rows": 7, "width": 330, "row_h": 22, "keep": 30,
                   "colors": {"damage": "#FFE566", "dps": "#FFFFFF", "share": "#FFE566", "time": "#AAFAFF", "power": "#66E9FF"}}
COMBAT_LAYOUT = 3  # version des réglages du tableau : 2 (v0.8.3) plus compact, tailles reprises de NotMeter remplacées ;
                   # 3 (v0.8.9) base de la contribution (part du groupe ou % des PV du boss), reprise de NotMeter
# Couleur des barres par classe (2 premiers chiffres des sorts).
CLASS_COLORS = {11: "#D9A066", 12: "#7FB2E5", 13: "#B48CE0", 14: "#7CC48A", 15: "#E07A7A", 16: "#5FC9C2", 17: "#E6D27A", 18: "#E596C8"}
NOTMETER_SETTINGS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Not4You-Meter", "settings.json")


def notmeter_combat_settings():
    """Réglages du tableau de NotMeter (son settings.json, en clair, sur ce PC) traduits pour la vue Combat ; None s'il
    n'est pas installé. Seul ce qui existe ici est repris : colonnes, formats, couleurs, puissance de combat, lignes
    visibles, remise à zéro après le combat. Pas la largeur ni la hauteur des lignes : le tableau du Pet Meter est plus
    compact (police plus petite), les mêmes nombres le rendaient trop grand. La contribution en « % des PV du boss »
    (ContributionBasis = BossMaxHp) est reprise : PV max lus à l'apparition du boss."""
    try:
        m = json.load(open(NOTMETER_SETTINGS, encoding="utf-8-sig")).get("Meter", {})
    except Exception:
        return None
    clamp = lambda v, lo, hi, default: max(lo, min(hi, int(v))) if isinstance(v, (int, float)) else default
    hex_color = lambda v, default: v if isinstance(v, str) and len(v) == 7 and v.startswith("#") else default
    colors = COMBAT_DEFAULTS["colors"]
    return {
        "damage": bool(m.get("ShowTotalDamage", False)), "dps": bool(m.get("ShowDps", True)), "share": bool(m.get("ShowContribution", True)),
        "dps_full": m.get("DpsDisplayMode", "Full") == "Full", "damage_short": m.get("TotalDamageDisplayMode", "Short") == "Short",
        "class_colors": bool(m.get("UseJobClassColor", True)), "power": bool(m.get("ShowCombatPower", True)),
        "rows": clamp(m.get("FixedCombatRowsViewportRows"), 3, 12, 7) if m.get("FixedCombatRowsViewportEnabled") else 10,
        "share_basis": "boss" if m.get("ContributionBasis") == "BossMaxHp" else "group",
        "keep": clamp(m.get("CombatEndAutoResetSeconds"), 5, 600, 30) if m.get("CombatEndAutoResetEnabled", True) else 0,
        "colors": {"damage": hex_color(m.get("TotalDamageColor"), colors["damage"]), "dps": hex_color(m.get("DpsColor"), colors["dps"]),
                   "share": hex_color(m.get("ContributionColor"), colors["share"]), "time": hex_color(m.get("CombatTimeColor"), colors["time"]),
                   "power": hex_color(m.get("CombatPowerColor"), colors["power"])},
    }


def load_skills():
    """(classe par préfixe de sort, nom de sort par code, icône officielle par code) ; vides si le fichier manque."""
    try:
        d = json.load(open(SKILLS_FILE, encoding="utf-8"))
        ints = lambda m: {int(k): v for k, v in m.items()}
        return ints(d["classes"]), ints(d["skills"]), ints(d.get("icons", {}))
    except Exception as e:
        log("skills.json:", e)
        return {}, {}, {}


def load_history():
    """Combats gardés pour la fenêtre Historique (combat_history.json) ; les clés JSON des sorts redeviennent des nombres."""
    try:
        data = json.load(open(HISTORY_FILE, encoding="utf-8"))
    except Exception:
        return []
    out = []
    for snap in data[:HISTORY_KEEP]:
        try:
            players = [(who, {**s, "skills": {int(k): v for k, v in s["skills"].items()}}, tuple(number) if number else None, name, power)
                       for who, s, number, name, power in snap["players"]]
            out.append({**snap, "players": players})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_history(history):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)


class Tips:
    """Infobulles d'un canevas : un texte par élément, affiché après un court survol (DELAY), caché au départ de la souris
    ou au clic. Qui dessine l'élément le souligne de pointillés, pour qu'on sache qu'on peut le survoler. Les canevas
    redessinés vident leurs infobulles (clear) à chaque dessin."""
    DELAY = 450

    def __init__(self, canvas, colors, font, on_shown=None):
        self.c, self.colors, self.font, self.on_shown = canvas, colors, font, on_shown
        self.texts = {}
        self.win = self.pending = self.text = None
        canvas.bind("<Motion>", self.motion, add="+")
        canvas.bind("<Leave>", lambda e: self.hide(), add="+")
        canvas.bind("<ButtonPress>", lambda e: self.hide(), add="+")

    def clear(self):
        self.texts.clear()

    def add(self, items, text):
        key = f"tip_{len(self.texts)}"
        self.texts[key] = text
        for item in items if isinstance(items, (list, tuple)) else [items]:
            self.c.addtag_withtag(key, item)
        return key

    def motion(self, e):
        tags = [t for i in self.c.find_overlapping(e.x, e.y, e.x, e.y) for t in self.c.gettags(i)]
        text = next((self.texts[t] for t in reversed(tags) if t in self.texts), None)
        if text == self.text:
            return
        self.hide()
        self.text = text
        if text:
            self.pending = self.c.after(self.DELAY, lambda: self.show(e.x_root, e.y_root))

    def show(self, x, y):
        import tkinter as tk
        self.pending = None
        if not self.text:
            return
        bg, fg, rim = self.colors()
        w = tk.Toplevel(self.c)
        w.withdraw()  # préparée cachée : placée avant d'apparaître
        w.overrideredirect(True)
        w.transient(self.c.winfo_toplevel())  # rattachée à sa fenêtre : toujours au-dessus d'elle, même au premier plan
        w.attributes("-topmost", True)
        w.configure(bg=rim)
        tk.Label(w, text=self.text, bg=bg, fg=fg, font=self.font, justify="left", wraplength=340, padx=9, pady=6).pack(padx=1, pady=1)
        w.update_idletasks()
        w.geometry(f"+{max(0, min(x + 14, w.winfo_screenwidth() - w.winfo_reqwidth() - 8))}+{y + 18}")
        w.deiconify()
        w.lift()
        self.win = w
        if self.on_shown:
            self.on_shown()

    def hide(self):
        if self.pending:
            self.c.after_cancel(self.pending)
            self.pending = None
        if self.win is not None:
            self.win.destroy()
            self.win = None
        self.text = None


class SkillIcons:
    """Icônes officielles des sorts (CDN de l'armurerie) : téléchargées en arrière-plan à la première demande, gardées
    dans le dossier icons, puis réduites à la taille voulue. None tant qu'une icône n'est pas là."""

    def __init__(self, names):
        self.names = names     # code -> nom du fichier sur le CDN
        self.dir = os.path.join(DATA_DIR, "icons")
        self.photos = {}       # (code, taille) -> PhotoImage (gardée : Tk ne garde pas les images)
        self.pending = set()

    def get(self, code, size):
        key = (code, size)
        if key in self.photos:
            return self.photos[key]
        stem = self.names.get(code)
        if not stem:
            return None
        path = os.path.join(self.dir, stem + ".png")
        if not os.path.exists(path):
            if stem not in self.pending:
                self.pending.add(stem)
                threading.Thread(target=self._download, args=(stem, path), daemon=True).start()
            return None
        try:
            from PIL import Image, ImageTk
            self.photos[key] = ImageTk.PhotoImage(Image.open(path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS))
        except Exception as e:
            log("icône", stem, repr(e))
            self.names.pop(code, None)
            return None
        return self.photos[key]

    def _download(self, stem, path):
        import urllib.request
        try:
            os.makedirs(self.dir, exist_ok=True)
            with urllib.request.urlopen(SKILL_ICON_URL.format(stem), timeout=15) as r:
                data = r.read()
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
        except Exception as e:
            log("icône", stem, repr(e))


class Live:
    """Compteur de dégâts en direct, comme un DPS meter : dès que tu frappes, les dégâts de chaque joueur sur ta cible
    principale (la plus grosse de celles que tu frappes : le boss, pas ses adds), avec le détail de leurs sorts, depuis le
    premier coup reçu par cette cible (même avant le tien) jusqu'à sa mort, ou 15 s sans coup de ta part. Seuls comptent
    les joueurs à portée : le serveur n'envoie que ce qui est près de toi. Les invocations comptent pour leur maître (donné
    par leur apparition, sinon même classe et même échelle de puissance ; jamais un joueur). Noms et puissance de combat :
    apparitions des joueurs. En mémoire seulement : rien n'est écrit ni envoyé."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0      # change à chaque coup compté : l'overlay ne redessine que s'il a bougé
        self.casters = {}     # joueur qui lance des sorts -> (classe, échelle) : sert à rattacher les invocations
        self.numbers = {}     # joueur -> numéro dans sa classe (« Templier 2 »), quand son nom n'est pas connu
        self.owners = {}      # invocation -> son maître (lu à son apparition, ou reconnu à la signature de son échelle)
        self.names = {}       # joueur -> (nom, puissance de combat), lus à son apparition
        self.sigs = {}        # joueur connu (nommé, ou toi : « me ») -> signature de son échelle (voir scale_sig)
        self.maxhp = {}       # créature -> PV max (lus à son apparition) : contribution en % des PV du boss
        self.history = []     # combats finis, du plus récent au plus ancien (instantanés du tableau) : fenêtre Historique
        self.history_version = 0
        self.my_power = None  # ta puissance de combat
        self.keep = 30.0      # secondes où le tableau reste affiché après le combat (0 : jusqu'au prochain) ; réglage Combat
        self.pre = {}         # hors combat : cible -> {joueur: stats} des coups des autres, repris si tu rejoins leur combat
        self.pruned = 0.0     # dernier ménage de pre
        self.reset()

    def reset(self):
        self.start = self.last = 0.0
        self.ended = None     # fin du combat (ton dernier coup), None pendant le combat
        self.targets = {}     # cible -> {joueur: stats}
        self.mine = {}        # cible -> tes dégâts : la cible principale est celle que tu frappes le plus
        self.spans = {}       # cible -> [ton premier, ton dernier coup]
        self.codes = {}       # cible -> code de créature
        self.casts = {}       # joueur -> {sort: utilisations} pendant le combat

    def summon(self, entity, owner):
        with self.lock:
            if len(self.owners) > 5000:
                self.owners.clear()
            self.owners[entity] = owner

    def max_hp(self, entity, hp):
        with self.lock:
            if len(self.maxhp) > 3000:
                self.maxhp.clear()
            self.maxhp[entity] = hp

    def player(self, entity, name, power):
        with self.lock:
            if len(self.names) > 5000:
                self.names.clear()
            self.names[entity] = (name, power)

    def cast(self, actor, skill, me=None):
        if skill >= 10_000_000:
            with self.lock:
                if actor in self.owners:  # invocation : ses sorts ne font pas d'elle un joueur, son maître compte
                    return
                if actor not in self.casters:
                    self.casters[actor] = (skill // 1_000_000, None)
                    if len(self.casters) > 2000:
                        self.casters.clear()
                if self.start and self.ended is None:  # utilisations de chaque sort pendant le combat (détail d'un joueur)
                    used = self.casts.setdefault("me" if actor == me else actor, {})
                    used[skill_base(skill)] = used.get(skill_base(skill), 0) + 1

    def merge(self, summon, owner):
        """Une invocation reconnue après coup : ses lignes déjà comptées passent à son maître (sous verrou)."""
        self.numbers.pop(summon, None)
        for rows in self.targets.values():
            lost = rows.pop(summon, None)
            if lost is None:
                continue
            s = rows.get(owner)
            if s is None:
                rows[owner] = lost
                continue
            for key in ("damage", *HIT_COUNTS):
                s[key] += lost.get(key, 0)
            s["first"], s["last"] = min(s["first"], lost["first"]), max(s["last"], lost["last"])
            for code, k in lost["skills"].items():
                d = s["skills"].setdefault(code, {**dict.fromkeys(HIT_COUNTS, 0), "damage": 0, "max": 0})
                for key in ("damage", *HIT_COUNTS):
                    d[key] += k.get(key, 0)
                d["max"] = max(d["max"], k["max"])
        self.version += 1

    def guess_owner(self, target, cls, scale, mobs):
        """Maître d'une invocation que son apparition ne donne pas : le seul joueur de sa classe au combat (ou celui de même
        échelle de puissance s'ils sont plusieurs) ; None sinon. « me » pour toi."""
        classes = {who: s["cls"] for who, s in self.targets.get(target, {}).items() if who not in mobs}
        classes.update({c: cs[0] for c, cs in self.casters.items() if c not in mobs and c not in classes})
        same = [who for who, c in classes.items() if c == cls]
        if len(same) == 1:
            return same[0]
        return next((who for who in same if self.casters.get(who) == (cls, scale)), None)

    def hit(self, target, actor, skill, crit, scale, damage, now, own, mobs, me=None, angle=None, marks=0):
        """Un coup lu dans le trafic ; own : coup à toi (toi ou ton invocation). Une créature (apparue en 41 36) n'est jamais
        un joueur : ses coups vont à son maître (donné par son apparition, sinon deviné), ceux de ses sorts de créature
        compris ; sans maître trouvé, ils ne comptent pas."""
        if actor == target or damage <= 0:
            return
        cls = skill // 1_000_000 if skill >= 10_000_000 else None
        sig = scale_sig(scale)
        with self.lock:
            known = actor == me or actor in self.names
            if known and sig is not None and cls is not None:
                self.sigs["me" if actor == me else actor] = sig
            owner = self.owners.get(actor)
            if owner is None and not known and self.start and self.ended is None:
                # Invocation dont l'apparition ne donne pas le maître : une créature (apparue en 41 36, jamais un joueur
                # qu'on n'aurait pas vu apparaître) rattachée au joueur connu qui a la même signature d'échelle (ses 3
                # derniers chiffres), sinon au seul joueur de sa classe au combat.
                same = [who for who, g in self.sigs.items() if g == sig] if sig is not None else []
                if len(same) == 1 and actor in mobs:
                    owner = same[0]
                elif actor in mobs and cls is not None:
                    owner = self.guess_owner(target, cls, scale, mobs)
                if owner is not None:
                    self.owners[actor] = owner
                    self.merge(actor, "me" if owner in ("me", me) else owner)
            if owner is not None:  # invocation : ses coups vont à son maître, avec la classe du maître
                if owner in mobs:
                    return  # invocation d'un monstre
                own = own or owner in ("me", me)
                actor = owner
                row = self.targets.get(target, {}).get("me" if own else owner)
                cls = self.casters.get(owner, (None,))[0] or (row["cls"] if row else None) or cls or 0
            elif cls is None or actor in mobs:
                return  # coup de monstre, ou créature sans maître trouvé
            elif actor in self.casters:
                self.casters[actor] = (cls, scale)
            waiting = False
            if own:
                who = "me"
                if not self.start or self.ended is not None or now - self.last > FIGHT_GAP:
                    self.reset()
                    self.start = now
                    self.adopt(now, mobs)
                self.last = now
                self.mine[target] = self.mine.get(target, 0) + damage
                span = self.spans.setdefault(target, [now, now])
                span[1] = now
                if target not in self.codes and target in mobs:
                    self.codes[target] = mobs[target]
            else:
                who = actor if actor in self.casters else next(
                    (c for c, cs in self.casters.items() if cs == (cls, scale)), actor)
                # Pas de combat à toi en cours : les coups des autres seuls n'ouvrent rien et ne s'affichent pas ; ils sont
                # retenus cible par cible, pour compter leur combat depuis son début si tu le rejoins.
                waiting = not self.start or self.ended is not None
            if waiting and len(self.pre) >= PRE_TARGETS and target not in self.pre:
                return
            rows = (self.pre if waiting else self.targets).setdefault(target, {})
            s = rows.get(who)
            if s is None:
                s = rows[who] = {**dict.fromkeys(HIT_COUNTS, 0), "damage": 0, "cls": cls, "first": now, "last": now, "skills": {}}
                if who != "me" and who not in self.numbers:  # (classe, numéro dans la classe)
                    self.numbers[who] = (cls, 1 + sum(1 for c, _ in self.numbers.values() if c == cls))
            # coup, critique, de dos, parfait, puissant, multiple : comptés pour le joueur et pour le sort
            counts = dict(zip(HIT_COUNTS, (1, crit, angle == 1, bool(marks & HIT_PERFECT), bool(marks & HIT_POWERFUL), bool(marks & HIT_MULTI))))
            s["damage"] += damage
            for key, n in counts.items():
                s[key] += n
            s["last"] = now
            # sorts de créature des invocations : une seule ligne « Invocations » (code 0) dans le détail
            k = s["skills"].setdefault(skill_base(skill) if skill >= 10_000_000 else 0, {**dict.fromkeys(HIT_COUNTS, 0), "damage": 0, "max": 0})
            k["damage"] += damage
            for key, n in counts.items():
                k[key] += n
            k["max"] = max(k["max"], damage)
            if not waiting:
                self.version += 1

    def adopt(self, now, mobs):
        """Ton combat commence (sous verrou) : les coups des autres sur les cibles qu'ils frappaient juste avant (sans pause de
        plus de 15 s) sont repris, comme si tu étais là depuis leur premier coup. Pas sur un mannequin : chacun s'y entraîne
        seul, son combat n'est pas le tien."""
        for target, rows in self.pre.items():
            if rows and mobs.get(target) not in TRAINING_DUMMIES and now - max(s["last"] for s in rows.values()) <= FIGHT_GAP:
                self.targets[target] = rows
        self.pre = {}

    def finish(self, at):
        """Fin du combat (sous verrou) : gardé dans l'historique s'il méritait le tableau (boss, mannequin, ou à plusieurs)."""
        self.ended = at
        self.version += 1
        main = self.main()
        if main is not None and self.worth(main):
            snap = self._board(main)
            snap.update(t=self.ended, me=FIGHTS.name)
            self.history.insert(0, snap)
            del self.history[HISTORY_KEEP:]
            self.history_version += 1

    def died(self, entity, now):
        """Une mort (42 36) : celle de ta cible principale finit le combat tout de suite (le tableau se fige, les coups qui
        suivent n'y entrent plus) ; le suivant commence à ton prochain coup, même sur un boss voisin."""
        with self.lock:
            self.pre.pop(entity, None)
            if self.start and self.ended is None and entity == self.main():
                self.finish(now)

    def tick(self, now):
        with self.lock:
            if self.start and self.ended is None and now - self.last > FIGHT_GAP:
                self.finish(self.last)
            if self.pre and now - self.pruned > 1.0:  # hors combat : on oublie les cibles que plus personne ne frappe
                self.pruned = now
                self.pre = {t: rows for t, rows in self.pre.items() if now - max((s["last"] for s in rows.values()), default=0) <= FIGHT_GAP}

    def main(self):
        """Ta cible principale : la plus grosse de celles que tu frappes (voir main_target), sous verrou."""
        return main_target(self.mine, self.maxhp)

    def showing(self, now=None):
        """Le tableau mérite l'overlay : un boss ou le mannequin (cible frappée 8 s, ou à plusieurs), pendant le combat et
        le temps réglé après (menu Combat)."""
        now = time.time() if now is None else now
        with self.lock:
            main = self.main()
            if main is None or (self.ended is not None and self.keep and now - self.ended > self.keep):
                return False
            return self.worth(main)

    def worth(self, main):
        """Cible qui mérite le tableau : le mannequin, une cible frappée depuis 8 s, ou frappée à plusieurs (sous verrou)."""
        first, last = self.spans.get(main, (0, 0))
        return (self.codes.get(main) in TRAINING_DUMMIES or last - first >= LIVE_BOSS_SPAN
                or len(self.targets.get(main, {})) >= 2)

    def board(self):
        """Instantané pour l'affichage : cible, durée, total et joueurs triés (copies, hors verrou)."""
        with self.lock:
            main = self.main()
            return None if main is None else self._board(main)

    def _board(self, main):
        rows = self.targets.get(main, {})
        first = min((s["first"] for s in rows.values()), default=self.start)
        last = max((s["last"] for s in rows.values()), default=self.last)

        def skills(who, s):  # copie, avec les utilisations de chaque sort
            used = self.casts.get(who, {})
            return {c: {**v, "casts": used.get(c, 0)} for c, v in s["skills"].items()}
        # (joueur, stats, (classe, numéro), nom ou None, puissance de combat ou None)
        players = [(who, {**s, "skills": skills(who, s)}, self.numbers.get(who),
                    *((None, self.my_power) if who == "me" else self.names.get(who, (None, None))))
                   for who, s in sorted(rows.items(), key=lambda kv: -kv[1]["damage"])]
        return {"code": self.codes.get(main, 0), "duration": max(1.0, last - first), "ended": self.ended is not None,
                "total": sum(s["damage"] for s in rows.values()), "max_hp": self.maxhp.get(main), "players": players}


LIVE = Live()


class Decoder:
    """Байты -> события ("state", levels, points) / ("gain", id, amount)."""

    def __init__(self, emit):
        self.emit = emit
        FIGHTS.emit = emit
        self.streams = {}
        self.mobs = {}     # entité -> code de créature (apparitions récentes)
        self.deaths = []   # [(heure, code)] des dernières morts, pour les cristaux
        self.last_death = 0.0  # heure de la dernière mort d'un monstre (pour les Kinah)

    def segment(self, conn, seq, data):
        st = self.streams.setdefault(conn, Stream())
        for fr in st.add(seq, data):
            try:
                for op, p in unpack(fr):
                    RECORDER.frame(op, p)
                    self.handle(op, p)
            except Exception as e:
                log("frame:", e, fr[:32].hex())

    def handle(self, op, p, now=None):
        """Un message du serveur : âmes, collection complète, apparitions / morts, cristaux, combats."""
        FIGHTS.tick(time.time() if now is None else now)
        LIVE.tick(time.time() if now is None else now)
        if op == OP_HIT:
            FIGHTS.hit(p, self.mobs, now)
        elif op == OP_CAST:
            FIGHTS.cast(p, now)
        elif op == OP_STATS:
            FIGHTS.stats(p)
        elif op == OP_MAP and len(p) >= 8:
            FIGHTS.map = int.from_bytes(p[4:8], "little")
        elif op == OP_GAIN:
            kind, mid, amount = parse_gain(p)
            if kind != 1:
                log("gain kind", kind, mid, amount)
            self.emit(("gain", mid, amount))
        elif op == OP_SPAWN:
            r = parse_spawn(p)
            if r:
                self.mobs[r[0]] = r[1]
                if len(self.mobs) > 3000:  # on oublie les plus anciennes
                    for k in list(self.mobs)[:1000]:
                        del self.mobs[k]
            owned = parse_owner(p)
            if owned:
                LIVE.summon(*owned)
            top = parse_max_hp(p)
            if top and r:
                LIVE.max_hp(r[0], top)
        elif op == OP_PLAYER:
            r = parse_player(p)
            if r:
                LIVE.player(*r)
        elif op == OP_POWER and len(p) >= 8:
            power = int.from_bytes(p[:8], "little")
            if 1_000 <= power < 10_000_000:
                LIVE.my_power = power
        elif op == OP_DEATH:
            dead = parse_death(p)
            # La mort de ta cible principale (le boss) finit le combat tout de suite : tableau figé, combat écrit.
            LIVE.died(dead, time.time() if now is None else now)
            FIGHTS.died(dead)
            code = self.mobs.pop(dead, None)
            if code is not None:
                now = time.time()
                self.last_death = now
                self.deaths = [d for d in self.deaths if now - d[0] < CRYSTAL_WINDOW] + [(now, code)]
        elif op == OP_CRYSTAL:
            r = parse_crystal(p)
            if r:
                now = time.time()
                recent = [d for d in self.deaths if now - d[0] < CRYSTAL_WINDOW]
                codes = [c for t, c in reversed(recent)]
                self.deaths = recent[:-1]  # une mort ne sert qu'une fois (la plus récente)
                self.emit(("crystal", r[0], r[1], codes))
        elif op == OP_ITEM:
            bal = parse_kina(p)
            if bal is not None:
                self.emit(("kina", bal, time.time() - self.last_death < KINA_WINDOW))
        elif op == OP_SELF:
            name, server = parse_self(p)
            if name:
                FIGHTS.set_me(read_varint(p, 0)[0], name)
                self.emit(("char", name, server))
        elif op == OP_GATHER:
            r = parse_gather(p)
            if r:
                self.emit(("gather",) + r)
        elif op in (OP_GATHER_LOGIN, OP_GATHER_TALLY) and len(p) >= 8:
            self.emit(("gather_count", int.from_bytes(p[:8], "little"), op == OP_GATHER_TALLY))
        elif op == OP_STATE:
            try:
                self.emit(("state",) + parse_state(p))
            except Exception as e:
                log("state parse:", e, p[:64].hex())


# ---------------------------------------------------------------- данные

NAME_CONFIRM = 2    # сколько раз имя должно совпасть в чате, чтобы считаться точным
PHRASE_CONFIRM = 3  # сколько убийств подряд должна встретиться строка, чтобы признать её системной
# Известные тексты строки «Имя: <текст> xN». Для других языков клиента текст
# выучивается сам: строка вида «A: B xN», появляющаяся при каждом убийстве.
SEED_PHRASES = {"получены очки духа": PHRASE_CONFIRM}


class Tracker:
    """Уровень и очки на текущем уровне, как их хранит сервер: при достижении
    порога уровень растёт, счётчик обнуляется; на макс. уровне сервер не считает,
    поэтому души сверх максимума копим отдельно в extra."""

    def __init__(self):
        self.levels, self.points, self.extra = {}, {}, {}
        # Имена зависят от языка клиента игры, поэтому хранятся по языкам:
        # {язык: {id: имя}} и {язык: {id: {имя: сколько раз прочитано в чате}}}
        self.names_by_lang, self.votes_by_lang = {}, {}
        self.phrases = dict(SEED_PHRASES)  # текст системной строки -> сколько раз встречен
        self.ocr_lang = None   # язык распознавания, на котором нашлась строка ("ru", "en-US")
        self.game_lang = None  # язык клиента игры, выбранный вручную; None - определить по чату
        self.ui_lang = None    # язык интерфейса: "en", "ru" или None - как в системе (иначе английский)
        self.recent, self.hidden = [], set()
        self.synced = None  # время последней синхронизации с сервером
        self.sync_key = None  # clé personnelle Aion 2 Todo (onglet Familiers > Pet Meter)
        self.theme = None     # thème de l'overlay (voir THEMES)
        self.layout = None    # affichage : "rings" (anneaux), "bar" (barre), "mini" (une ligne)
        self.pos = None
        # Kinah de monstres du jour, par perso : {"day": "AAAA-MM-JJ", "chars": {nom: kinah}}
        self.kina = {"day": game_day(), "chars": {}}
        self.kina_cap = KINA_CAP
        self.show_kina = False  # module Kinah : masqué par défaut (menu Kinah du jour → Afficher)
        self.kina_show_chosen = False
        self.auto_hide = True   # l'overlay se cache sans butin de familier…
        self.hide_after = 5     # … au bout de ces secondes ; il revient au survol ou au prochain butin
        self.record_fights = True  # combats écrits pour Aion 2 Builder
        self.record_all_fights = False  # aussi les combats ordinaires (pas seulement boss, mannequin, JcJ)
        self.char = None          # perso en jeu (nom), connu à l'entrée dans une zone
        self.kina_balance = None  # dernier solde vu pendant cette session
        # Récoltes du jour, par serveur (« #n° » : la limite est commune aux persos d'un serveur) ; tant que le serveur n'est
        # pas lu, par perso (« @nom ») : {"day": "AAAA-MM-JJ", "by": {clé: {"count", "failed", "items": {objet: qté}, "chars": []}}}
        # count = le compteur du serveur (récoltes commencées, ratées comprises), lu à l'entrée en jeu et à chaque récolte.
        self.gather = {"day": game_day(), "by": {}}
        self.gather_pending = None  # compteur de l'entrée en jeu, en attente du perso (qui arrive juste après)
        self.servers = {}         # perso -> n° de serveur, lu à l'entrée dans une zone
        self.width = 330          # largeur des vues Familiers et Récolte (le tableau de combat a la sienne)
        self.show_list = True     # liste des derniers monstres sous le dernier tué
        self.tips_seen = False    # une infobulle a déjà été vue : l'astuce du pied n'est plus utile
        self.detail_full = False  # détail d'un joueur avec toutes les colonnes (lien « + colonnes »)
        self.gather_toast = False  # récolte : petit message en bas à droite au lieu de l'overlay (menu Affichage), coupé par défaut
        self.view_mode = "auto"   # vue choisie (pas « view » : c'est la méthode qui lit un familier) : "auto", ou verrouillée sur "pets" / "gather"
        self.auto_view = "pets"   # vue en mode auto : la dernière activité (monstre ou récolte)
        self.gather_times = []    # heures des récoltes de la session, pour le rythme
        self.combat = {**COMBAT_DEFAULTS, "colors": dict(COMBAT_DEFAULTS["colors"])}  # tableau de combat (menu ⋯ → Combat)
        self.combat_imported = False  # réglages de NotMeter déjà repris (une fois, au premier lancement)
        self.shared = load_shared_names()  # наша база data/names.json
        self.npc_pets = load_npc_pets()
        self.load()
        for lang, shared in self.shared.items():  # свои имена (переименованные, выученные) важнее базы
            self.names_by_lang[lang] = {**shared, **self.names_by_lang.get(lang, {})}

    @property
    def lang(self):
        """Язык клиента: выбранный вручную, иначе тот, на котором читается чат, иначе язык системы."""
        if self.game_lang:
            return self.game_lang
        if self.ocr_lang:
            return self.ocr_lang.split("-")[0]
        return ui_language()

    @property
    def names(self):
        return self.names_by_lang.setdefault(self.lang, {})

    @property
    def votes(self):
        return self.votes_by_lang.setdefault(self.lang, {})

    def load(self):
        try:
            d = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            return
        ints = lambda m: {int(k): v for k, v in m.items()}
        self.levels, self.points = ints(d.get("levels", {})), ints(d.get("points", {}))
        self.extra = ints(d.get("extra", {}))
        self.phrases.update(d.get("phrases", {}))
        self.ocr_lang = d.get("ocr_lang")
        self.game_lang = d.get("game_lang")
        names, votes = d.get("names", {}), d.get("votes", {})
        if names and all(k.isdigit() for k in names):  # старый формат без языков
            names, votes = {self.lang: names}, {self.lang: votes}
        self.names_by_lang = {lang: ints(m) for lang, m in names.items()}
        self.votes_by_lang = {lang: ints(m) for lang, m in votes.items()}
        self.ui_lang = d.get("ui_lang")
        self.recent = d.get("recent", [])
        self.hidden = set(d.get("hidden", []))
        self.synced = d.get("synced")
        self.sync_key = d.get("sync_key")
        self.theme = d.get("theme")
        self.layout = d.get("layout")
        self.pos = d.get("pos")
        self.kina = d.get("kina") or self.kina
        # la v0.5.0 enregistrait 2 M par défaut : on ne garde le plafond que s'il a été choisi dans le menu
        self.kina_cap = d.get("kina_cap") if d.get("kina_cap_chosen") else KINA_CAP
        # avant la v0.7.5, « Afficher » était enregistré à vrai sans avoir été choisi : on ne le garde que s'il a été choisi
        self.kina_show_chosen = d.get("kina_show_chosen", False)
        self.show_kina = d.get("show_kina", False) if self.kina_show_chosen else False
        self.auto_hide = d.get("auto_hide", True)
        self.hide_after = d.get("hide_after", 5)
        self.record_fights = d.get("record_fights", True)
        self.record_all_fights = d.get("record_all_fights", False)
        self.char = d.get("char")
        self.gather = d.get("gather") or self.gather
        self.servers = d.get("servers", {})
        self.width = d.get("width", 330)
        self.show_list = d.get("show_list", True)
        self.tips_seen = d.get("tips_seen", False)
        self.detail_full = d.get("detail_full", False)
        self.gather_toast = d.get("gather_toast", False)
        self.view_mode = d.get("view", "auto")
        saved = d.get("combat") or {}
        self.combat = {**self.combat, **saved, "colors": {**self.combat["colors"], **saved.get("colors", {})}}
        self.combat_imported = d.get("combat_imported", False)
        if d.get("combat_layout", 1) < 2:  # tableau plus compact depuis la v0.8.3 : tailles par défaut
            self.combat.update(width=COMBAT_DEFAULTS["width"], row_h=COMBAT_DEFAULTS["row_h"])
        if d.get("combat_layout", 1) < 3 and self.combat_imported:  # v0.8.9 : base de la contribution, reprise de NotMeter
            found = notmeter_combat_settings()
            if found:
                self.combat["share_basis"] = found["share_basis"]
        for mid in list(self.points):  # файлы старой версии копили очки без повышения уровня
            self.add_points(mid, 0)

    def save(self):
        d = {"levels": self.levels, "points": self.points, "extra": self.extra,
             "names": self.own_names(), "votes": self.votes_by_lang,
             "phrases": self.phrases, "ocr_lang": self.ocr_lang,
             "game_lang": self.game_lang, "ui_lang": self.ui_lang,
             "recent": self.recent, "hidden": sorted(self.hidden),
             "synced": self.synced, "pos": self.pos, "sync_key": self.sync_key, "theme": self.theme, "layout": self.layout,
             "kina": self.kina, "kina_cap": self.kina_cap, "kina_cap_chosen": self.kina_cap != KINA_CAP, "show_kina": self.show_kina, "kina_show_chosen": self.kina_show_chosen,
             "auto_hide": self.auto_hide, "hide_after": self.hide_after, "char": self.char, "gather": self.gather, "view": self.view_mode,
             "servers": self.servers, "width": self.width, "show_list": self.show_list,
             "tips_seen": self.tips_seen, "detail_full": self.detail_full, "gather_toast": self.gather_toast,
             "combat": self.combat, "combat_imported": self.combat_imported, "combat_layout": COMBAT_LAYOUT,
             "record_fights": self.record_fights, "record_all_fights": self.record_all_fights}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)

    def on_event(self, ev):
        if ev[0] == "state":
            self.levels, self.points = ev[1], ev[2]
            self.synced = time.strftime("%d.%m %H:%M")
        elif ev[0] == "gather":
            # ("gather", objet, quantité, ratée, supérieure) : le résultat d'une récolte (le compteur, lui, vient du serveur)
            _, item, qty, failed = ev[:4]
            entry = self.gather_slot()
            if failed:
                entry["failed"] += 1
            else:
                entry["items"][item] = entry["items"].get(item, 0) + qty
            if not self.gather_toast:  # en mode discret, la récolte ne change pas la vue
                self.auto_view = "gather"
        elif ev[0] == "gather_count":
            # ("gather_count", récoltes du jour, en jeu) : le compteur de la limite tenu par le serveur, récoltes faites
            # sans le Pet Meter comprises. À l'entrée en jeu il arrive avant le perso : gardé jusqu'à ce qu'on le connaisse.
            _, n, live = ev
            if n > 2 * GATHER_CAP:
                return
            if not live:
                self.gather_pending = n
                return
            entry = self.gather_slot()
            if n > entry["count"]:
                self.gather_times = [t for t in self.gather_times if time.time() - t < 1800] + [time.time()]
                if not self.gather_toast:
                    self.auto_view = "gather"
            entry["count"] = n
        elif ev[0] == "gain":
            _, mid, amount = ev
            self.auto_view = "pets"
            self.add_points(mid, amount)
            self.hidden.discard(mid)
            if mid in self.recent:
                self.recent.remove(mid)
            self.recent.insert(0, mid)
            del self.recent[30:]
        elif ev[0] == "site_state":
            if not self.synced or self.synced.startswith("site"):  # jamais par-dessus la collection du jeu
                self.levels, self.points = ev[1], ev[2]
                self.synced = "site " + time.strftime("%d.%m %H:%M")
        elif ev[0] == "crystal":
            mid = self.maxed_pet(ev[1], ev[3])
            if mid is None:
                log("cristaux sans monstre connu:", ev[1:])
                return
            self.auto_view = "pets"
            self.levels[mid] = MAX_LEVEL  # des cristaux = familier forcément au max
            self.points[mid] = 0
            self.extra[mid] = self.extra.get(mid, 0) + ev[2]
            self.hidden.discard(mid)
            if mid in self.recent:
                self.recent.remove(mid)
            self.recent.insert(0, mid)
            del self.recent[30:]
        elif ev[0] == "char":
            if ev[1] != self.char:
                self.kina_balance = None  # autre perso : autre solde
            self.char = ev[1]
            if len(ev) > 2 and ev[2]:
                self.servers[ev[1]] = ev[2]
                # récoltes comptées avant de connaître le serveur : rangées avec celles du serveur
                by = self.gather_today()
                early = by.pop(f"@{ev[1]}", None)
                if early:
                    entry = self.gather_slot()
                    entry["count"] = max(entry["count"], early["count"])  # compteurs du serveur : le plus récent est le plus grand
                    entry["failed"] += early["failed"]
                    for item, qty in early["items"].items():
                        entry["items"][item] = entry["items"].get(item, 0) + qty
            if self.gather_pending is not None:  # compteur reçu à l'entrée en jeu, juste avant le perso
                self.gather_slot()["count"] = self.gather_pending
                self.gather_pending = None
        elif ev[0] == "kina":
            _, bal, after_kill = ev
            gain = bal - self.kina_balance if self.kina_balance is not None else 0
            self.kina_balance = bal
            if gain > 0 and after_kill:
                day = self.kina_today()
                key = self.char or "?"
                day[key] = day.get(key, 0) + gain
        elif ev[0] == "vote":
            self.vote(*ev[1:])
        elif ev[0] == "phrase":
            self.phrases[ev[1]] = self.phrases.get(ev[1], 0) + 1
        elif ev[0] == "ocr_lang":
            self.ocr_lang = ev[1]

    def kina_today(self):
        """Kinah de monstres du jour par perso (remis à zéro au reset quotidien)."""
        today = game_day()
        if self.kina.get("day") != today:
            self.kina = {"day": today, "chars": {}}
        return self.kina["chars"]

    def kina_earned(self):
        return self.kina_today().get(self.char or "?", 0)

    def daily_payload(self):
        """Pour les tâches du jour d'Aion 2 Todo : les récoltes du jour par serveur (compteur du jeu) et les Kinah de monstres
        du jour par perso, avec le jour de jeu (reset à 07:00 UTC) ; None s'il n'y a rien à envoyer."""
        gather = [{"server": int(k[1:]), "count": e["count"]} for k, e in self.gather_today().items()
                  if k.startswith("#") and k[1:].isdigit() and e.get("count")]
        kinah = [{"name": n, "server": self.servers.get(n), "amount": a} for n, a in self.kina_today().items() if n != "?" and a > 0]
        if not gather and not kinah:
            return None
        return {"day": game_day(), "gather": gather, "kinah": kinah, "client": f"pet-meter {VERSION}"}

    def gather_today(self):
        """Récoltes du jour, remises à zéro au reset quotidien (07:00 UTC, 08:00 à Paris), comme dans le jeu."""
        today = game_day()
        if self.gather.get("day") != today:
            self.gather = {"day": today, "by": {}}
        return self.gather["by"]

    def gather_key(self):
        server = self.servers.get(self.char)
        return f"#{server}" if server else f"@{self.char or '?'}"

    def gather_entry(self):
        return self.gather_today().get(self.gather_key(), {"count": 0, "failed": 0, "items": {}, "chars": []})

    def item_name(self, code):
        """Nom d'une ressource récoltée (code = les 4 octets de l'objet, en hexadécimal) dans la langue du jeu, anglais à
        défaut ; None si le numéro n'est pas dans GATHER_ITEMS."""
        names = GATHER_ITEMS.get(int.from_bytes(bytes.fromhex(code), "little"))
        return names and names[{"fr": 0, "ru": 2}.get(self.lang.split("-")[0], 1)]

    def gather_slot(self):
        """Récoltes du jour du serveur en jeu (créées au besoin), avec le perso en jeu dans la liste des persos."""
        entry = self.gather_today().setdefault(self.gather_key(), {"count": 0, "failed": 0, "items": {}, "chars": []})
        if self.char and self.char not in entry["chars"]:
            entry["chars"].append(self.char)
        return entry

    def current_view(self):
        return self.view_mode if self.view_mode != "auto" else self.auto_view

    def gather_rate(self):
        """Récoltes par heure sur la dernière demi-heure ; None s'il y en a trop peu pour le dire."""
        recent = [t for t in self.gather_times if time.time() - t < 1800]
        if len(recent) < 10 or recent[-1] - recent[0] < 120:
            return None
        return (len(recent) - 1) / (recent[-1] - recent[0]) * 3600

    def maxed_pet(self, genus, codes):
        """Familier du monstre tué juste avant des cristaux : d'abord un familier déjà connu au max,
        de la bonne espèce si on la connaît, en partant de la mort la plus récente."""
        want = CRYSTAL_GENUS.get(genus)
        candidates = [(pid, g) for code in codes for pid, g in self.npc_pets.get(code, [])]
        for ok in (lambda pid, g: self.levels.get(pid, 0) >= MAX_LEVEL and (want is None or g == want),
                   lambda pid, g: want is None or g == want):
            for pid, g in candidates:
                if ok(pid, g):
                    return pid
        return None

    def known_phrases(self):
        return [p for p, n in list(self.phrases.items()) if n >= PHRASE_CONFIRM]

    def add_points(self, mid, amount):
        lvl = self.levels.get(mid, 0)
        if lvl >= MAX_LEVEL:
            self.extra[mid] = self.extra.get(mid, 0) + amount
            return
        pts = self.points.get(mid, 0) + amount
        while lvl < MAX_LEVEL and pts >= REQ[lvl]:
            pts -= REQ[lvl]
            lvl += 1
        if lvl >= MAX_LEVEL and pts:
            self.extra[mid] = self.extra.get(mid, 0) + pts
            pts = 0
        self.levels[mid], self.points[mid] = lvl, pts

    def vote(self, mid, name, lang=None):
        """Имя прочитано в чате; lang - язык распознавания, т.е. язык клиента."""
        lang = (lang or self.lang).split("-")[0]
        names = self.names_by_lang.setdefault(lang, {})
        votes = self.votes_by_lang.setdefault(lang, {})
        if mid in names:
            return
        v = votes.setdefault(mid, {})
        v[name] = v.get(name, 0) + 1
        if v[name] >= NAME_CONFIRM and name not in names.values():
            names[mid] = name
            votes.pop(mid, None)

    def rename(self, mid, name):
        if name:
            self.names[mid] = name
        else:
            self.names.pop(mid, None)
        self.votes.pop(mid, None)

    def own_names(self):
        """Только имена, которых нет в базе: копию базы в прогресс не сохраняем,
        иначе обновлённая база перекрывалась бы старыми значениями."""
        return {lang: {mid: n for mid, n in names.items() if self.shared.get(lang, {}).get(mid) != n}
                for lang, names in self.names_by_lang.items()}

    def name(self, mid):
        """База (язык клиента) -> выученное из чата -> кандидат из чата "?" -> английское из базы -> ID."""
        if mid in self.names:
            return self.names[mid]
        v = self.votes.get(mid)
        if v:
            return max(v, key=v.get) + "?"
        return self.shared.get("en", {}).get(mid) or f"ID {mid}"

    def view(self, mid):
        """(уровень, очки, нужно или None если максимум, души сверх максимума)."""
        lvl = self.levels.get(mid, 0)
        pts = self.points.get(mid, 0)
        return lvl, pts, (REQ[lvl] if lvl < MAX_LEVEL else None), self.extra.get(mid, 0)


# ---------------------------------------------------------------- synchro Aion 2 Todo

class Syncer:
    """Envoie l'état complet (niveaux + âmes de l'étape en cours) à Aion 2 Todo.

    Les envois sont regroupés : après un kill on attend quelques secondes, pour n'envoyer
    qu'une requête par série de kills. Tout se passe dans un thread : l'overlay ne bloque jamais.
    """
    DELAY = 4.0
    DAILY_DELAY = 20.0  # récoltes et Kinah du jour : un envoi toutes les 20 s au plus pendant que tu joues

    def __init__(self, tracker, on_status):
        self.tr, self.on_status = tracker, on_status
        self.due = None
        self.daily_due = None
        self.lock = threading.Lock()
        self.status = ("idle", None)  # ("ok", heure) / ("error", message) / ("nokey", None)
        threading.Thread(target=self._loop, daemon=True).start()

    def kick(self, now=False):
        with self.lock:
            self.due = time.time() + (0 if now else self.DELAY)

    def kick_daily(self):
        """Récoltes ou Kinah du jour changés : envoi groupé dans les 20 s (le premier changement fixe l'heure d'envoi)."""
        with self.lock:
            if self.daily_due is None:
                self.daily_due = time.time() + self.DAILY_DELAY

    def _loop(self):
        while True:
            time.sleep(0.5)
            with self.lock:
                daily = self.daily_due is not None and time.time() >= self.daily_due
                if daily:
                    self.daily_due = None
                pets = self.due is not None and time.time() >= self.due
                if pets:
                    self.due = None
            if daily:
                self._send_daily()
            if pets:
                self._send()

    def _send(self):
        import urllib.request, urllib.error
        key = (self.tr.sync_key or "").strip()
        if not key:
            self._set(("nokey", None)); return
        if not self.tr.synced:  # ni collection du jeu ni collection du site : rien de fiable à envoyer
            self._set(("waiting", None))
            return
        body = json.dumps({
            "levels": {str(k): v for k, v in self.tr.levels.items()},
            "points": {str(k): v for k, v in self.tr.points.items()},
            "client": f"pet-meter {VERSION}",
        }).encode()
        req = urllib.request.Request(SYNC_URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}",
            "User-Agent": f"AION2-pet-meter/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.loads(r.read() or b"{}")
            log("sync ok:", res)
            self._set(("ok", time.strftime("%H:%M")))
        except urllib.error.HTTPError as e:
            log("sync http:", e.code)
            self._set(("error", "clé refusée" if e.code == 401 else f"erreur {e.code}"))
            if e.code >= 500 or e.code == 429:
                self.kick()  # on réessaiera plus tard
        except Exception as e:
            log("sync:", repr(e))
            self._set(("error", "hors ligne"))
            self.kick()

    def _send_daily(self):
        """Récoltes et Kinah du jour vers Aion 2 Todo (tâches « Récolte — limite du jour » et « Kinah de monstres »). Un échec
        ne touche pas l'état de la synchro des familiers affiché dans le pied : on réessaie au prochain changement."""
        import urllib.request, urllib.error
        key = (self.tr.sync_key or "").strip()
        payload = self.tr.daily_payload()
        if not key or not payload:
            return
        req = urllib.request.Request(DAILY_URL, data=json.dumps(payload).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": f"AION2-pet-meter/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                log("synchro du jour :", json.loads(r.read() or b"{}"))
        except urllib.error.HTTPError as e:
            log("synchro du jour http:", e.code)
        except Exception as e:
            log("synchro du jour :", repr(e))

    def _set(self, st):
        self.status = st
        self.on_status(st)

    def pull(self, on_done):
        """Relit la collection enregistrée sur Aion 2 Todo (réinstallation, lancement avant d'entrer en jeu).
        on_done(levels, points) est appelé dans ce thread ; la collection du jeu la remplacera à l'entrée en jeu."""
        def run():
            import urllib.request, urllib.error
            key = (self.tr.sync_key or "").strip()
            if not key:
                return
            req = urllib.request.Request(SYNC_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": f"AION2-pet-meter/{VERSION}"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    d = json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                log("pull http:", e.code)
                self._set(("error", "clé refusée" if e.code == 401 else f"erreur {e.code}"))
                return
            except Exception as e:
                log("pull:", repr(e))
                return
            levels, points = {}, {}
            for k, total in (d.get("souls") or {}).items():
                lvl, rest = 0, int(total)
                while lvl < MAX_LEVEL and rest >= REQ[lvl]:
                    rest -= REQ[lvl]; lvl += 1
                levels[int(k)], points[int(k)] = lvl, (0 if lvl >= MAX_LEVEL else rest)
            log("pull ok:", len(levels), "familiers")
            on_done(levels, points)
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- thèmes

# bg = fond, rim = bordures, accent = progression, gold = niveaux finis, rose = alerte,
# text = texte principal, mist = texte secondaire, done = familier au max (rien à gagner).
THEMES = {
    "abysse":    {"label": "Abysse (par défaut)", "bg": "#0F1A24", "rim": "#22374A", "accent": "#6FE0CF", "gold": "#E6C068",
                  "rose": "#E08A9A", "text": "#E8EEF2", "mist": "#7D8C99", "done": "#4CC38A"},
    "asmodien":  {"label": "Asmodien", "bg": "#1A0F1F", "rim": "#3B2240", "accent": "#C07AF0", "gold": "#E6A04A",
                  "rose": "#FF6B6B", "text": "#F0E6F2", "mist": "#8E7A93", "done": "#5BD68A"},
    "elyseen":   {"label": "Élyséen (clair)", "bg": "#F4EFE3", "rim": "#D8CBA8", "accent": "#1F7F92", "gold": "#A87508",
                  "rose": "#B53A52", "text": "#2A2418", "mist": "#6F6550", "done": "#1E8A4C"},
    "sobre":     {"label": "Sobre", "bg": "#18181B", "rim": "#303036", "accent": "#60A5FA", "gold": "#FBBF24",
                  "rose": "#F87171", "text": "#F4F4F5", "mist": "#8B8B93", "done": "#4ADE80"},
    "contraste": {"label": "Contraste élevé", "bg": "#000000", "rim": "#5A5A5A", "accent": "#00FFC8", "gold": "#FFD700",
                  "rose": "#FF4D6D", "text": "#FFFFFF", "mist": "#C8C8C8", "done": "#3CFF7A"},
}


# ---------------------------------------------------------------- mise à jour

def version_tuple(v):
    """"v0.2.1" -> (0, 2, 1) ; None si ce n'est pas une version publiée (ex. "dev-abc123")."""
    v = (v or "").strip().lstrip("vV")
    parts = v.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


class Updater:
    """Vérifie sur Aion 2 Todo s'il existe une version plus récente, la télécharge à côté de l'exe,
    puis, sur demande, remplace l'exe et relance le Pet Meter (un petit script attend sa fermeture)."""
    CHECK_EVERY = 6 * 3600

    def __init__(self, on_status):
        self.on_status = on_status
        self.ready = None  # (version, chemin du nouvel exe) une fois téléchargé
        try:  # reste de la mise à jour précédente
            if FROZEN and os.path.exists(sys.executable + ".old"):
                os.remove(sys.executable + ".old")
        except OSError:
            pass
        if FROZEN and version_tuple(VERSION):
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._check()
            except Exception as e:
                log("update:", repr(e))
            time.sleep(self.CHECK_EVERY)

    def check_now(self):
        """Vérification demandée depuis le menu : le résultat s'affiche quelques secondes en bas de l'overlay."""
        def run():
            if not version_tuple(VERSION):
                self.on_status(("info", "update_fail")); return
            self.on_status(("info", "update_dl"))
            try:
                found = self._check()
            except Exception as e:
                log("update:", repr(e))
                self.on_status(("info", "update_fail")); return
            if not found:
                self.on_status(("info", "up_to_date"))
        threading.Thread(target=run, daemon=True).start()

    def _check(self):
        """True si une nouvelle version est prête (téléchargée), False si on est à jour."""
        import urllib.request
        base = SYNC_URL.rsplit("/api/", 1)[0]
        with urllib.request.urlopen(base + "/api/downloads/pet-meter", timeout=15) as r:
            info = json.loads(r.read() or b"{}")
        latest = info.get("version")
        if not info.get("available") or not version_tuple(latest) or version_tuple(latest) <= version_tuple(VERSION):
            return False
        if self.ready and self.ready[0] == latest:
            self.on_status(("ready", latest))
            return True
        target = os.path.join(APP_DIR, "aion2-pet-meter.new.exe")
        log("update: téléchargement", latest)
        tmp = target + ".part"
        with urllib.request.urlopen(base + info["url"], timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
        if os.path.getsize(tmp) != info.get("size", os.path.getsize(tmp)):
            raise IOError("téléchargement incomplet")
        os.replace(tmp, target)
        self.ready = (latest, target)
        self.on_status(("ready", latest))
        return True

    def apply(self):
        """Remplace l'exe en cours par le nouveau et relance le Pet Meter.

        Windows interdit de supprimer un exe en cours d'exécution mais autorise à le renommer :
        l'ancien devient .old (supprimé au prochain lancement), le nouveau prend sa place."""
        if not self.ready:
            return False
        exe, new = sys.executable, self.ready[1]
        old = exe + ".old"
        try:
            if os.path.exists(old):
                os.remove(old)
            os.replace(exe, old)
            os.replace(new, exe)
        except OSError as e:
            log("update apply:", repr(e))
            if not os.path.exists(exe) and os.path.exists(old):
                os.replace(old, exe)  # on remet l'ancien en place
            return False
        launch_detached(exe)
        return True


def launch_detached(exe):
    """Lance un exe PyInstaller comme un programme à part entière. Sans ça, il hérite de l'environnement de l'exe en
    cours (_PYI_*) : il réutilise son dossier temporaire _MEI…, que l'ancien ne peut plus supprimer en se fermant
    (« Failed to remove temporary directory ») et que le nouveau perd en cours de route."""
    import ctypes, subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        ctypes.windll.kernel32.SetDllDirectoryW(None)  # le dossier des DLL de l'ancien ne doit pas passer au nouveau
    except Exception:
        pass
    subprocess.Popen([exe], env=env, close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


# ---------------------------------------------------------------- имена из чата

def find_game_window():
    """Прямоугольник (l, t, r, b) самого большого видимого окна AION2.exe."""
    import ctypes
    from ctypes import wintypes
    import psutil
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "aion2.exe"}
    user32 = ctypes.windll.user32
    best = [None, 0]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[1]:
                best[0], best[1] = (r.left, r.top, r.right, r.bottom), area
        return True

    user32.EnumWindows(cb, 0)
    return best[0]


def ocr_languages():
    """Языки распознавания, установленные в Windows (зависят от языковых пакетов)."""
    try:
        from winrt.windows.media.ocr import OcrEngine
        return [l.language_tag for l in OcrEngine.available_recognizer_languages]
    except Exception as e:
        log("ocr langs:", repr(e))
        return ["ru", "en-US"]


def ui_language():
    import ctypes, locale
    tag = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "en_US")
    return tag.split("_")[0]


def norm_phrase(s):
    return " ".join(s.lower().split()).strip(" .,:;-—")


def similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


class NameLearner:
    """После убийства монстра без имени читает чат игры (OCR Windows) и ищет
    самую нижнюю строку «Имя: <текст> xN», где N совпадает с полученными очками.

    Язык не важен: строка распознаётся всеми языками OCR, установленными в Windows,
    а <текст> сверяется с известными фразами. Если клиент на языке, фраза которого
    неизвестна, она выучивается: строка, найденная в PHRASE_CONFIRM убийствах,
    признаётся системной. Имя засчитывается, только если рядом не было других
    убийств, и считается точным после NAME_CONFIRM совпадений."""

    def __init__(self, emit, tracker):
        import re
        # «Имя: текст xN»; N распознаётся неустойчиво («xl», «х|»), поэтому необязателен
        self.line_re = re.compile(r"([^\[\]:;：]{2,40}?)\s*[:;：]\s*([^:;：]{3,60}?)(?:\s*[xхX×*]\s*(\S{1,4}))?\s*$")
        self.emit, self.tr = emit, tracker
        self.jobs = queue.Queue()
        self.gains = []  # (время, id) последних убийств
        self.langs = ocr_languages()
        threading.Thread(target=self.work, daemon=True).start()

    def on_gain(self, mid, amount):
        now = time.time()
        self.gains = [g for g in self.gains if now - g[0] < 5] + [(now, mid)]
        if mid not in self.tr.names:
            self.jobs.put((mid, amount, now))

    def ambiguous(self, mid, t):
        """Рядом были убийства других монстров - нельзя понять, чья строка в чате."""
        return any(abs(gt - t) < 1.5 and gm != mid for gt, gm in self.gains)

    def lang_order(self):
        ui = self.tr.game_lang or ui_language()
        first = [self.tr.ocr_lang] if self.tr.ocr_lang in self.langs and not self.tr.game_lang else []
        native = [l for l in self.langs if l.split("-")[0] == ui]
        return list(dict.fromkeys(first + native + self.langs))

    def work(self):
        while True:
            mid, amount, t = self.jobs.get()
            if mid in self.tr.names:
                continue  # имя выучено, пока задание ждало в очереди
            for attempt, delay in enumerate((0.6, 1.6)):
                time.sleep(max(0, t + delay - time.time()))
                if self.ambiguous(mid, t):
                    log(f"ocr {mid}: рядом убийства других монстров, пропуск")
                    break
                try:
                    found = self.read_chat(amount, debug=attempt == 1)
                except Exception as e:
                    log("ocr:", repr(e))
                    break
                if not found:
                    continue
                name, phrase, lang, known = found
                if known:
                    if lang != self.tr.ocr_lang:
                        self.emit(("ocr_lang", lang))
                    self.emit(("vote", mid, name, lang))
                else:
                    self.emit(("phrase", phrase))
                break

    def read_chat(self, amount, debug=False):
        """(имя, фраза, язык, фраза_известна) для самой свежей подходящей строки."""
        from PIL import ImageGrab
        import winocr
        rect = find_game_window()
        if not rect:
            if debug:
                log("ocr: окно игры не найдено (свёрнуто?)")
            return None
        img = ImageGrab.grab(bbox=rect, all_screens=True)
        known = self.tr.known_phrases()
        guess, seen = None, 0
        for lang in self.lang_order():
            found = []
            for line in winocr.recognize_pil_sync(img, lang).get("lines", []):
                m = self.line_re.search(line["text"])
                if not m:
                    continue
                seen += 1
                n = m.group(3)
                if n and n.isdigit() and int(n) != amount:
                    continue
                y = line["words"][0]["bounding_rect"]["y"] if line.get("words") else 0
                name = " ".join(m.group(1).split()).strip(" -—.,")
                if name:
                    found.append((y, name, norm_phrase(m.group(2)), bool(n and n.isdigit())))
            hits = [f for f in found if any(similar(f[2], p) for p in known)]
            if hits:
                _, name, phrase, _ = max(hits)  # самая нижняя строка чата - самая свежая
                return name, phrase, lang, True
            # незнакомую фразу учим только по строкам с распознанным количеством
            learn = [f for f in found if f[3]]
            if learn and guess is None:
                _, name, phrase, _ = max(learn)
                guess = (name, phrase, lang, False)
        if not guess and debug:
            log(f"ocr: строка «Имя: … очки духа» не найдена (строк вида «A: B» на экране: {seen}); "
                "чат открыт? снимок: aion2_pet_tracker_ocr_miss.jpg")
            try:
                img.convert("RGB").save(os.path.join(DATA_DIR, "aion2_pet_tracker_ocr_miss.jpg"), quality=80)
            except Exception:
                pass
        return guess


# ---------------------------------------------------------------- захват

def run_sniffer(on_event):
    import psutil
    from scapy.all import sniff, conf, IP, TCP, Raw

    game_ports = set()

    def refresh():
        while True:
            try:
                pids = {p.pid for p in psutil.process_iter(["name"])
                        if (p.info["name"] or "").lower() == "aion2.exe"}
                ports = set()
                for c in psutil.net_connections(kind="tcp"):
                    if (c.pid in pids and c.raddr and not c.raddr[0].startswith("127.")
                            and c.raddr[1] not in (80, 443)):
                        ports.add(c.laddr[1])
                game_ports.clear(); game_ports.update(ports)
                on_event(("conn", bool(ports)))
            except Exception as e:
                log("psutil:", e)
            time.sleep(2)

    threading.Thread(target=refresh, daemon=True).start()
    dec = Decoder(on_event)

    def on_packet(p):
        if Raw in p and TCP in p and p[TCP].dport in game_ports:
            dec.segment((p[IP].src, p[TCP].sport, p[TCP].dport), p[TCP].seq, bytes(p[Raw].load))

    ifaces = [i.network_name for i in conf.ifaces.values() if i.ip and not i.ip.startswith("127.")]
    sniff(iface=ifaces, filter="tcp and not port 443 and not port 80", prn=on_packet, store=False)


def replay_fights(path, me=None):
    """Diagnostic : les combats d'un enregistrement perception-*.jsonl.gz, écrits dans le dossier des combats.
    --me <entité> impose « toi » (sinon : les stats 4a 36 de l'enregistrement)."""
    import gzip
    dec = Decoder(lambda ev: print("combat :", ev[1], "s,", ev[2], "DPS") if ev[0] == "fight" else None)
    if me:
        FIGHTS.set_me(int(me))
        FIGHTS.pinned = True
    with gzip.open(path, "rt", encoding="utf-8") as f:
        head = json.loads(f.readline())
        base = time.mktime(time.strptime(head["start"], "%Y-%m-%d %H:%M:%S")) if "start" in head else time.time()
        last = base
        for line in f:
            r = json.loads(line)
            if "op" in r:
                last = base + r["t"]
                dec.handle(bytes.fromhex(r["op"]), bytes.fromhex(r["p"]), last)
    FIGHTS.tick(last + FIGHT_GAP + 1)
    print("toi :", FIGHTS.me, "| dossier :", FIGHTS_DIR, "| dernier :", FIGHTS.last_path)


def replay(path):
    t = Tracker.__new__(Tracker)
    t.levels, t.points, t.extra = {}, {}, {}
    t.shared = load_shared_names()
    t.names_by_lang, t.votes_by_lang = {l: dict(m) for l, m in t.shared.items()}, {}
    t.ocr_lang, t.game_lang = None, "ru"
    t.recent, t.hidden, t.synced = [], set(), None
    t.npc_pets = load_npc_pets()

    def emit(ev):
        if ev[0] == "state":
            print(f"STATE: уровней {len(ev[1])}, очков {len(ev[2])}")
        else:
            print("GAIN:", ev[1:])
        t.on_event(ev)

    dec = Decoder(emit)
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("d") == "S":
            dec.segment(r["conn"], r["seq"], bytes.fromhex(r["data"]))
    for mid in t.recent:
        lvl, pts, need, extra = t.view(mid)
        print(f"{t.name(mid)}: ур.{lvl} " + (f"{pts}/{need}" if need else f"МАКС +{extra}"))


# ---------------------------------------------------------------- оверлей

TEXT = {
    "fr": {
        "left_to": "encore {n} {to}",
        "to": ["pour le débloquer", "pour le niveau 2", "pour le niveau 3"],
        "max": "MAX", "all_open": "tous les niveaux débloqués",
        "over_max": "+{n} au-delà du max — inutile de le farmer",
        "no_data": "En attente du jeu", "no_data_hint": "Reconnecte ton perso (retour à la sélection) pour charger ta collection",
        "empty": "Tue un monstre", "empty_hint": "Sa progression s'affichera ici",
        "online": "en jeu", "offline": "jeu introuvable", "synced": "données {t}", "unsynced": "pas de données",
        "rename": "Renommer…", "rename_title": "Nom du familier", "hide": "Retirer de la liste",
        "clear": "Vider la liste", "opacity": "Opacité", "quit": "Quitter",
        "to_tray": "Réduire dans la barre des tâches", "show_overlay": "Afficher l'overlay", "hide_overlay": "Masquer l'overlay",
        "auto": "Comme le système", "detect": "Détecter dans le chat",
        "npcap_title": "Npcap est nécessaire",
        "npcap_hint": "Le Pet Meter lit le trafic du jeu grâce au pilote gratuit Npcap. Installe-le depuis le site officiel, puis relance le Pet Meter.",
        "download": "Télécharger",
        "sniff_title": "La lecture n'a pas démarré",
        "sniff_hint": "Lance le Pet Meter en administrateur. Sinon, réinstalle Npcap en décochant « Restrict Npcap driver's access to Administrators only ».",
        "sync_key": "Clé de synchro Aion 2 Todo…", "sync_now": "Synchroniser maintenant",
        "sync_key_title": "Clé de synchro",
        "sync_key_prompt": "Colle ta clé (site Aion 2 Todo > onglet Familiers > Pet Meter > Créer ma clé) :",
        "sync_ok": "synchro {t}", "sync_waiting": "entre en jeu pour synchroniser",
        "site_data": "collection du site ({t})", "sync_err": "synchro : {m}", "sync_nokey": "pas de clé (menu ⋯)",
        "open_site": "Ouvrir Aion 2 Todo",
        "theme": "Thème",
        "layout": "Affichage",
        "layout_rings": "Cercle", "layout_bar": "Barre (sans cercle)", "layout_mini": "Une ligne (minimal)",
        "layout_list": "Liste des derniers monstres", "layout_width": "Largeur {n} px",
        "layout_gather_toast": "Récolte : petit message en bas à droite (sans ouvrir l'overlay)",
        "toast_gather_ok": "✓ Récolte réussie · +{q}", "toast_gather_better": "✓ Récolte supérieure · +{q}", "toast_gather_fail": "✕ Récolte ratée", "toast_gather_count": "{n} / {cap} aujourd'hui",
        "toast_gather_item": "✓ {item} ×{q}", "toast_gather_up": "Rang supérieur · ",
        "gather_items": "RESSOURCES DU JOUR", "gather_more": "+ {n} autres ressources", "gather_more_1": "+ {n} autre ressource",
        "item_unknown": "objet {id}",
        "check_update": "Vérifier les mises à jour",
        "up_to_date": "à jour ({v})", "update_dl": "téléchargement de la mise à jour…", "update_fail": "mise à jour impossible",
        "lvl_short": "niv. {n}",
        "maxed_banner": "AU MAX — inutile de le farmer",
        "maxed_short": "AU MAX",
        "skip_hint": "inutile de le farmer",
        "over_max_short": "+{n} âmes perdues au-delà du max",
        "update_ready": "v{v} prête — clic pour mettre à jour",
        "update_menu": "Mettre à jour vers {v}",
        "update_none": "Version {v}",
        "kina": "Kinah du jour", "kina_full": "plafond atteint", "kina_left": "encore {n} possibles", "kina_menu": "Kinah du jour",
        "hide_menu": "Masquage auto", "hide_on": "Se cacher sans butin de familier (revient au survol)", "hide_after": "Après {n} s sans butin",
        "kina_show": "Afficher", "kina_cap": "Plafond : {n}", "kina_reset": "Remettre à zéro (aujourd'hui)",
        "tab_pets": "FAMILIERS", "tab_gather": "RÉCOLTE", "view_auto": "AUTO", "view_locked": "FIXÉE",
        "gather_title": "Récoltes du jour", "gather_full_title": "Plafond atteint", "gather_line": "Récolte du jour",
        "gather_left": "{n} restantes · remise à zéro à {t}", "gather_eta": " · au rythme actuel, plafond vers {t}",
        "gather_near": "Plus que {n} récoltes aujourd'hui (ratées comprises)",
        "gather_blocked": "Récolte bloquée jusqu'à {t} (dans {d}) : place aux familiers",
        "gather_failed": " · dont {n} ratées",
        "gather_servers": "PAR SERVEUR (LIMITE COMMUNE AUX PERSOS)", "gather_chars": "PAR PERSO", "pet_line": "Familier",
        "tab_combat": "COMBAT", "combat_boss": "Boss", "combat_dummy": "Mannequin", "combat_done": "Combat terminé",
        "combat_none": "Pas de combat en cours", "combat_none_hint": "Le tableau s'ouvre dès que tu frappes un boss ou le mannequin.",
        "combat_sub": "{total} · {dps}/s", "c_power": "Puissance de combat (après le nom)",
        "combat_gone": "Plus de combat en cours pour ce joueur.", "you": "toi", "player": "Joueur",
        "p_damage": "Dégâts", "p_dps": "DPS", "p_share": "Part", "p_hits": "Coups", "p_crit": "Critique", "p_time": "Durée", "p_power": "puissance",
        "p_skill": "Sort", "p_max": "Max", "p_unknown": "Sort inconnu", "p_back": "De dos", "p_casts": "Util.", "p_avg": "Moy.", "p_perfect": "Parfait", "p_powerful": "Puissant", "p_multi": "Multiples",
        "p_crit_s": "Crit.", "p_perfect_s": "Parf.", "p_powerful_s": "Puiss.", "p_multi_s": "Multi",
        "hist_title": "HISTORIQUE DES COMBATS", "hist_menu": "Historique des combats", "hist_back": "← tous les combats",
        "hist_empty": "Aucun combat gardé pour l'instant : chaque combat de boss, de mannequin ou de groupe s'ajoute ici quand il se termine.",
        "hist_players": "{n} joueurs", "hist_hint": "clic sur un combat : son tableau ; clic sur un joueur : son détail", "hist_icon": "≡ historique", "p_summons": "Invocations", "p_max_hit": "Coup max",
        "hist_players_1": "{n} joueur", "gather_failed_1": " · dont {n} ratée", "over_max_short_1": "+{n} âme perdue au-delà du max",
        "tips_hint": "Astuce : survole ce qui est souligné en pointillés pour une explication.",
        "tip_ring": "Niveau du familier (0 à 3). Les anneaux se remplissent avec ses âmes : 5 pour le niveau 1, 25 pour le 2, 75 pour le 3.",
        "tip_points": "Âmes du niveau en cours / âmes qu'il faut pour passer au suivant.",
        "tip_pips": "Les 3 niveaux du familier : pastille pleine = niveau atteint, cerclée = en cours. À droite : âmes du niveau en cours / âmes qu'il faut.",
        "tip_maxed": "Familier au niveau 3 : inutile de farmer ce monstre. Le jeu donne des cristaux d'espèce à la place des âmes ; +N = âmes reçues au-delà du max, perdues.",
        "tip_bar": "3 segments = les niveaux 1, 2 et 3 (5, 25 et 75 âmes).",
        "tip_kina": "Kinah gagnés sur les monstres depuis la remise à zéro (08:00, heure de Paris), face au plafond du jour. Ni les ventes ni les quêtes, et seulement pendant que le Pet Meter tourne. ⋯ → Kinah du jour pour le plafond.",
        "tip_gather_line": "Récoltes du jour sur ce serveur face à la limite de 1 000. Onglet Récolte pour le détail.",
        "tip_view_auto": "AUTO : la vue suit ce que tu fais (familiers, combat de boss, récolte). Clic sur un onglet : la vue reste fixée dessus.",
        "tip_view_locked": "FIXÉE : la vue ne change plus toute seule. Clic ici : retour en AUTO.",
        "tip_footer": "En jeu : le Pet Meter lit le trafic du jeu (« jeu introuvable » : lance le jeu ; vérifie Npcap). Synchro : dernier envoi de ta collection à Aion 2 Todo. « pas de clé » : ⋯ → Aion 2 Todo → Clé de synchro.",
        "tip_menu": "Menu : affichage, combat, langue, Aion 2 Todo, outils, mises à jour.",
        "tip_gather_count": "Compteur du jeu : récoltes commencées aujourd'hui sur ce serveur (tous tes persos), ratées comprises. Lu à l'entrée en jeu, puis à chaque récolte.",
        "tip_gather_sub": "Remise à zéro à 08:00 (heure de Paris). L'heure du plafond suppose ton rythme des 30 dernières minutes. Les ratées sont celles vues par le Pet Meter depuis son lancement.",
        "tip_hist_link": "Les 50 derniers combats finis (boss, mannequin, groupe), avec le détail de chaque joueur.",
        "tip_combat_total": "Dégâts de tout le groupe sur ta cible, puis le DPS du groupe.",
        "c_lbl_damage": "DÉGÂTS", "c_lbl_dps": "DPS", "c_lbl_share_boss": "% PV", "c_lbl_share_group": "PART", "c_lbl_player": "JOUEUR · PUISSANCE",
        "tip_col_damage": "Dégâts totaux de chaque joueur sur ta cible (ses invocations comprises).",
        "tip_col_dps": "DPS : dégâts ÷ durée du combat.",
        "tip_col_share_boss": "Part des PV max du boss ({hp} PV), comme NotMeter réglé sur « BossMaxHp ». ⋯ → Combat pour afficher la part du groupe à la place.",
        "tip_col_share_group": "Part des dégâts du groupe (100 % au total). ⋯ → Combat pour afficher le % des PV du boss à la place.",
        "tip_stale": "Plus de coup sur la cible depuis {n} s : sorti de portée, mort ou occupé ailleurs. Ses chiffres n'avancent plus.",
        "tip_col_player": "Les joueurs qui frappent ta cible, toi toujours visible. En cyan, leur puissance de combat, lue quand ils apparaissent près de toi. Clic sur une ligne : le détail de ses sorts.",
        "p_share_boss": "PV du boss", "tip_close": "Fermer (Échap)",
        "tip_rates": "De dos {back} · parfaits {perfect} · puissants {powerful} · multiples {multi}",
        "tip_k_damage": "Dégâts totaux sur la cible.", "tip_k_crit": "Part de ses coups critiques.", "tip_k_back": "Part de ses coups portés de dos.",
        "tip_k_perfect": "Part de ses coups marqués « coup parfait » par le jeu.", "tip_k_powerful": "Part de ses coups marqués « coup puissant » par le jeu.",
        "tip_k_multi": "Part de ses coups marqués « coups multiples » par le jeu.", "tip_k_max": "Son plus gros coup du combat.",
        "tip_k_time": "Durée du combat : du premier au dernier coup sur la cible.",
        "tip_c_hits": "Nombre de coups.", "tip_c_casts": "Utilisations : nombre de lancers du sort (un lancer peut porter plusieurs coups).",
        "tip_c_share": "Part de ses dégâts faite par ce sort.", "tip_c_avg": "Dégâts moyens d'un coup.", "tip_c_max": "Plus gros coup de ce sort.",
        "tip_c_skill": "Ses sorts, du plus au moins de dégâts. Survole un sort pour tout son détail.",
        "p_more": "+ colonnes", "p_less": "− colonnes", "tip_more": "Afficher ou cacher : utilisations, de dos, parfaits, puissants, multiples, moyenne.",
        "p_summons_tip": "Coups de ses invocations avec leurs propres sorts.",
        "tip_unknown": "Absent de la liste officielle des sorts (souvent un enchaînement) : son nom n'est pas connu.",
        "tip_skill": "{name}\n{hits} coups · {casts} utilisations · {damage} dégâts\ncritique {crit} · de dos {back} · parfaits {perfect} · puissants {powerful} · multiples {multi}\nmoyenne {avg} · max {max}",
        "hist_dps": "TON DPS", "tip_hist_dps": "Ton DPS sur ce combat (celui du 1er si tu n'y étais pas).",
        "menu_lang": "Langue / Language", "menu_lang_ui": "Interface", "menu_lang_game": "Langue du jeu (noms des familiers)",
        "menu_todo": "Aion 2 Todo", "menu_tools": "Outils", "menu_updates": "Mises à jour",
        "combat_menu": "Combat", "c_damage": "Dégâts totaux", "c_dps": "DPS", "c_share": "Contribution", "c_basis_group": "    en part des dégâts du groupe", "c_basis_boss": "    en % des PV max du boss (comme NotMeter)",
        "c_dps_full": "DPS en entier (27 272)", "c_damage_short": "Dégâts abrégés (1,92 M)", "c_class_colors": "Barres aux couleurs des classes",
        "c_rows": "{n} lignes", "c_width": "Largeur {n} px", "c_row_h": "Lignes de {n} px", "c_keep": "Garder le tableau {n} s après le combat",
        "c_keep_next": "Garder le tableau jusqu'au prochain combat", "c_import": "Reprendre les réglages de NotMeter",
        "combat_imported": "réglages de combat repris de NotMeter", "combat_no_notmeter": "NotMeter introuvable sur ce PC",
        "rec_start": "Enregistrer le trafic (5 min, diagnostic)", "rec_mark": "Repère n° {n}",
        "rec_stop": "Arrêter l'enregistrement", "rec_show": "Afficher l'enregistrement",
        "rec_on": "● enregistrement {t} · repères : {n}", "rec_saved": "enregistrement terminé",
        "fight_saved": "⚔ combat enregistré : {d} s · {dps} DPS", "fights_menu": "Mes combats (Aion 2 Builder)",
        "fights_on": "Enregistrer mes combats", "fights_open": "Ouvrir le dossier des combats",
        "fights_all": "Aussi les combats ordinaires (sinon : boss, mannequin, JcJ)",
    },
    "ru": {
        "left_to": "осталось {n} {to}",
        "to": ["до открытия", "до 2-го уровня", "до 3-го уровня"],
        "max": "МАКС", "all_open": "все уровни открыты",
        "over_max": "+{n} сверх максимума — можно не бить",
        "no_data": "Нет данных с сервера", "no_data_hint": "Перезайдите персонажем, чтобы загрузить прогресс",
        "empty": "Убейте монстра", "empty_hint": "Его прогресс появится здесь",
        "online": "в игре", "offline": "игра не найдена", "synced": "данные {t}", "unsynced": "без данных",
        "rename": "Переименовать…", "rename_title": "Имя питомца", "hide": "Убрать из списка",
        "clear": "Очистить список", "opacity": "Прозрачность", "quit": "Закрыть",
        "to_tray": "Свернуть в трей", "show_overlay": "Показать оверлей", "hide_overlay": "Скрыть оверлей",
        "auto": "Как в системе", "detect": "Определить по чату",
        "npcap_title": "Нужен Npcap",
        "npcap_hint": "Оверлей читает трафик игры через бесплатный драйвер Npcap. Установите его с официального сайта и перезапустите оверлей.",
        "download": "Скачать",
        "sniff_title": "Перехват не запустился",
        "sniff_hint": "Запустите оверлей от имени администратора. Если не поможет — переустановите Npcap без галочки «Restrict Npcap driver's access to Administrators only».",
        "kina": "Кина за день", "kina_full": "лимит достигнут", "kina_left": "ещё {n}", "kina_menu": "Кина за день",
        "hide_menu": "Автоскрытие", "hide_on": "Скрывать без добычи питомцев (появляется при наведении)", "hide_after": "Через {n} с без добычи",
        "kina_show": "Показывать", "kina_cap": "Лимит: {n}", "kina_reset": "Сбросить (сегодня)",
        "tab_pets": "ПИТОМЦЫ", "tab_gather": "СБОР", "view_auto": "АВТО", "view_locked": "ЗАКРЕПЛЕНО",
        "gather_title": "Сбор за день", "gather_full_title": "Лимит достигнут", "gather_line": "Сбор за день",
        "gather_left": "осталось {n} · сброс в {t}", "gather_eta": " · в этом темпе лимит к {t}",
        "gather_near": "Осталось {n} сборов на сегодня (неудачные тоже считаются)",
        "layout_rings": "Круг", "layout_bar": "Полоса (без круга)", "layout_mini": "Одна строка (минимум)",
        "layout_list": "Список последних монстров", "layout_width": "Ширина {n} px",
        "layout_gather_toast": "Сбор: маленькое уведомление внизу справа (без оверлея)",
        "toast_gather_ok": "✓ Сбор удался · +{q}", "toast_gather_better": "✓ Улучшенный сбор · +{q}", "toast_gather_fail": "✕ Сбор не удался", "toast_gather_count": "{n} / {cap} за сегодня",
        "toast_gather_item": "✓ {item} ×{q}", "toast_gather_up": "Улучшенный сбор · ",
        "gather_items": "РЕСУРСЫ ЗА ДЕНЬ", "gather_more": "+ ещё ресурсов: {n}", "gather_more_1": "+ ещё ресурсов: {n}",
        "item_unknown": "предмет {id}",
        "gather_blocked": "Сбор закрыт до {t} (через {d}) : время для питомцев",
        "gather_failed": " · из них неудачных: {n}",
        "gather_servers": "ПО СЕРВЕРАМ (ОБЩИЙ ЛИМИТ)", "gather_chars": "ПО ПЕРСОНАЖАМ", "pet_line": "Питомец",
        "tab_combat": "БОЙ", "combat_boss": "Босс", "combat_dummy": "Манекен", "combat_done": "Бой окончен",
        "combat_none": "Боя нет", "combat_none_hint": "Таблица откроется, как только ты ударишь босса или манекен.",
        "combat_sub": "{total} · {dps}/с", "c_power": "Боевая мощь (после имени)",
        "combat_gone": "У этого игрока больше нет боя.", "you": "ты", "player": "Игрок",
        "p_damage": "Урон", "p_dps": "DPS", "p_share": "Доля", "p_hits": "Удары", "p_crit": "Крит", "p_time": "Время", "p_power": "мощь",
        "p_skill": "Умение", "p_max": "Макс.", "p_unknown": "Неизвестное умение", "p_back": "Со спины", "p_casts": "Исп.", "p_avg": "Сред.", "p_perfect": "Идеальный", "p_powerful": "Мощный", "p_multi": "Мульти",
        "p_crit_s": "Крит", "p_perfect_s": "Идеал.", "p_powerful_s": "Мощн.", "p_multi_s": "Мульти",
        "hist_title": "ИСТОРИЯ БОЁВ", "hist_menu": "История боёв", "hist_back": "← все бои",
        "hist_empty": "Пока нет сохранённых боёв: бои с боссом, манекеном или в группе появятся здесь после окончания.",
        "hist_players": "игроков: {n}", "hist_hint": "клик по бою: его таблица; клик по игроку: подробности", "hist_icon": "≡ история", "p_summons": "Призывы", "p_max_hit": "Макс. удар",
        "hist_players_1": "игроков: {n}", "gather_failed_1": " · из них неудачных: {n}", "over_max_short_1": "+{n} душ сверх максимума потеряно", "over_max_short": "+{n} душ сверх максимума потеряно",
        "tips_hint": "Совет: наведи курсор на подчёркнутое пунктиром, чтобы увидеть пояснение.",
        "tip_ring": "Уровень питомца (0–3). Кольца заполняются душами: 5 для 1-го уровня, 25 для 2-го, 75 для 3-го.",
        "tip_points": "Души текущего уровня / сколько нужно для следующего.",
        "tip_pips": "3 уровня питомца: закрашенный кружок — достигнут, контур — в процессе. Справа: души текущего уровня / сколько нужно.",
        "tip_maxed": "Питомец на 3-м уровне: фармить этого монстра незачем. Игра даёт кристаллы вида вместо душ; +N — души сверх максимума, потеряны.",
        "tip_bar": "3 отрезка — уровни 1, 2 и 3 (5, 25 и 75 душ).",
        "tip_kina": "Кины с монстров с ежедневного сброса против дневного лимита. Без продаж и квестов, и только пока работает Pet Meter.",
        "tip_gather_line": "Сборы за день на этом сервере против лимита 1000. Подробно — во вкладке «Сбор».",
        "tip_view_auto": "АВТО: вид следует за тем, что ты делаешь (питомцы, бой с боссом, сбор). Клик по вкладке закрепляет вид.",
        "tip_view_locked": "ЗАКРЕПЛЕНО: вид больше не меняется сам. Клик здесь — снова АВТО.",
        "tip_footer": "В игре: Pet Meter читает трафик игры. Синхронизация: последняя отправка коллекции в Aion 2 Todo. «нет ключа»: ⋯ → Aion 2 Todo → ключ.",
        "tip_menu": "Меню: вид, бой, язык, Aion 2 Todo, инструменты, обновления.",
        "tip_gather_count": "Счётчик игры: сборы, начатые сегодня на этом сервере (все персонажи), включая неудачные. Читается при входе в игру и при каждом сборе.",
        "tip_gather_sub": "Сброс по времени игры. Время до лимита — по твоему темпу за последние 30 минут. Неудачные — замеченные Pet Meter с его запуска.",
        "tip_hist_link": "Последние 50 законченных боёв (босс, манекен, группа) с подробностями по каждому игроку.",
        "tip_combat_total": "Урон всей группы по цели, затем DPS группы.",
        "c_lbl_damage": "УРОН", "c_lbl_dps": "DPS", "c_lbl_share_boss": "% HP", "c_lbl_share_group": "ДОЛЯ", "c_lbl_player": "ИГРОК · МОЩЬ",
        "tip_col_damage": "Общий урон каждого игрока по цели (с призывами).",
        "tip_col_dps": "DPS: урон ÷ длительность боя.",
        "tip_col_share_boss": "Доля от макс. HP босса ({hp} HP), как в NotMeter с «BossMaxHp». ⋯ → Бой — показать долю группы.",
        "tip_col_share_group": "Доля урона группы (всего 100 %). ⋯ → Бой — показать % HP босса.",
        "tip_stale": "Нет ударов по цели {n} с: вне зоны видимости, погиб или занят другим. Его цифры больше не растут.",
        "tip_col_player": "Игроки, бьющие твою цель; ты всегда виден. Голубым — боевая мощь, прочитанная при их появлении рядом. Клик по строке — его умения.",
        "p_share_boss": "HP босса", "tip_close": "Закрыть (Esc)",
        "tip_rates": "Со спины {back} · идеальных {perfect} · мощных {powerful} · мульти {multi}",
        "tip_k_damage": "Общий урон по цели.", "tip_k_crit": "Доля критических ударов.", "tip_k_back": "Доля ударов со спины.",
        "tip_k_perfect": "Доля ударов, отмеченных игрой как «идеальный».", "tip_k_powerful": "Доля ударов, отмеченных игрой как «мощный».",
        "tip_k_multi": "Доля ударов, отмеченных игрой как «мульти».", "tip_k_max": "Самый сильный удар в бою.",
        "tip_k_time": "Длительность боя: от первого до последнего удара по цели.",
        "tip_c_hits": "Число ударов.", "tip_c_casts": "Использования: число применений умения (одно применение может дать несколько ударов).",
        "tip_c_share": "Доля его урона от этого умения.", "tip_c_avg": "Средний урон удара.", "tip_c_max": "Самый сильный удар умения.",
        "tip_c_skill": "Его умения по урону. Наведи на умение, чтобы увидеть всё.",
        "p_more": "+ столбцы", "p_less": "− столбцы", "tip_more": "Показать или скрыть: использования, со спины, идеальные, мощные, мульти, средний.",
        "p_summons_tip": "Удары его призывов их собственными умениями.",
        "tip_unknown": "Нет в официальном списке умений (часто цепочка): название неизвестно.",
        "tip_skill": "{name}\n{hits} ударов · {casts} использований · {damage} урона\nкрит {crit} · со спины {back} · идеальных {perfect} · мощных {powerful} · мульти {multi}\nсредний {avg} · макс. {max}",
        "hist_dps": "ТВОЙ DPS", "tip_hist_dps": "Твой DPS в этом бою (лидера, если тебя там не было).",
        "menu_lang": "Язык / Language", "menu_lang_ui": "Интерфейс", "menu_lang_game": "Язык игры (имена питомцев)",
        "menu_todo": "Aion 2 Todo", "menu_tools": "Инструменты", "menu_updates": "Обновления",
        "combat_menu": "Бой", "c_damage": "Общий урон", "c_dps": "DPS", "c_share": "Вклад", "c_basis_group": "    доля урона группы", "c_basis_boss": "    % от макс. HP босса (как NotMeter)",
        "c_dps_full": "DPS целиком (27 272)", "c_damage_short": "Урон сокращённо (1,92 M)", "c_class_colors": "Полосы цветом класса",
        "c_rows": "{n} строк", "c_width": "Ширина {n} px", "c_row_h": "Строки по {n} px", "c_keep": "Держать таблицу {n} с после боя",
        "c_keep_next": "Держать таблицу до следующего боя", "c_import": "Взять настройки NotMeter",
        "combat_imported": "настройки боя взяты из NotMeter", "combat_no_notmeter": "NotMeter не найден на этом ПК",
        "rec_start": "Запись для изучения вида (5 мин)", "rec_mark": "Метка № {n}",
        "rec_stop": "Остановить запись", "rec_show": "Показать запись",
        "rec_on": "● запись {t} · меток: {n}", "rec_saved": "запись сохранена",
        "fight_saved": "⚔ бой записан: {d} с · {dps} DPS", "fights_menu": "Мои бои (Aion 2 Builder)",
        "fights_on": "Записывать мои бои", "fights_open": "Открыть папку боёв",
        "fights_all": "Также обычные бои (иначе: боссы, манекен, PvP)",
    },
    "en": {
        "left_to": "{n} more {to}",
        "to": ["to unlock", "to level 2", "to level 3"],
        "max": "MAX", "all_open": "all levels unlocked",
        "over_max": "+{n} past max — safe to skip",
        "no_data": "No server data yet", "no_data_hint": "Re-enter the world with your character to load progress",
        "empty": "Kill a monster", "empty_hint": "Its progress will show up here",
        "online": "in game", "offline": "game not found", "synced": "data {t}", "unsynced": "no data",
        "rename": "Rename…", "rename_title": "Pet name", "hide": "Remove from list",
        "clear": "Clear list", "opacity": "Opacity", "quit": "Close",
        "to_tray": "Hide to tray", "show_overlay": "Show overlay", "hide_overlay": "Hide overlay",
        "auto": "Match system", "detect": "Detect from chat",
        "npcap_title": "Npcap is required",
        "npcap_hint": "The overlay reads game traffic through the free Npcap driver. Install it from the official site, then restart the overlay.",
        "download": "Download",
        "sniff_title": "Capture didn't start",
        "sniff_hint": "Run the overlay as administrator. If that doesn't help, reinstall Npcap with \"Restrict Npcap driver's access to Administrators only\" unchecked.",
        "kina": "Kinah today", "kina_full": "cap reached", "kina_left": "{n} left", "kina_menu": "Daily Kinah",
        "hide_menu": "Auto-hide", "hide_on": "Hide without pet loot (comes back on hover)", "hide_after": "After {n} s without loot",
        "kina_show": "Show", "kina_cap": "Cap: {n}", "kina_reset": "Reset (today)",
        "tab_pets": "PETS", "tab_gather": "GATHERING", "view_auto": "AUTO", "view_locked": "LOCKED",
        "gather_title": "Gathering today", "gather_full_title": "Cap reached", "gather_line": "Gathering today",
        "gather_left": "{n} left · resets at {t}", "gather_eta": " · at this pace, cap around {t}",
        "gather_near": "Only {n} gathers left today (failures count)",
        "layout_rings": "Circle", "layout_bar": "Bar (no circle)", "layout_mini": "One line (minimal)",
        "layout_list": "List of recent monsters", "layout_width": "Width {n} px",
        "layout_gather_toast": "Gathering: small toast at bottom right (overlay stays put)",
        "toast_gather_ok": "✓ Gathered · +{q}", "toast_gather_better": "✓ Higher-grade gather · +{q}", "toast_gather_fail": "✕ Gathering failed", "toast_gather_count": "{n} / {cap} today",
        "toast_gather_item": "✓ {item} ×{q}", "toast_gather_up": "Higher grade · ",
        "gather_items": "RESOURCES TODAY", "gather_more": "+ {n} more resources", "gather_more_1": "+ {n} more resource",
        "item_unknown": "item {id}",
        "gather_blocked": "Gathering blocked until {t} (in {d}): time for pets",
        "gather_failed": " · {n} of them failed",
        "gather_servers": "PER SERVER (SHARED BY CHARACTERS)", "gather_chars": "PER CHARACTER", "pet_line": "Pet",
        "tab_combat": "COMBAT", "combat_boss": "Boss", "combat_dummy": "Training dummy", "combat_done": "Fight over",
        "combat_none": "No fight in progress", "combat_none_hint": "The board opens as soon as you hit a boss or the dummy.",
        "combat_sub": "{total} · {dps}/s", "c_power": "Combat power (after the name)",
        "combat_gone": "No fight in progress for this player.", "you": "you", "player": "Player",
        "p_damage": "Damage", "p_dps": "DPS", "p_share": "Share", "p_hits": "Hits", "p_crit": "Crit", "p_time": "Time", "p_power": "power",
        "p_skill": "Skill", "p_max": "Max", "p_unknown": "Unknown skill", "p_back": "Back", "p_casts": "Uses", "p_avg": "Avg", "p_perfect": "Perfect", "p_powerful": "Powerful", "p_multi": "Multi",
        "p_crit_s": "Crit", "p_perfect_s": "Perf.", "p_powerful_s": "Pow.", "p_multi_s": "Multi",
        "hist_title": "FIGHT HISTORY", "hist_menu": "Fight history", "hist_back": "← all fights",
        "hist_empty": "No fight kept yet: each boss, dummy or group fight is added here when it ends.",
        "hist_players": "{n} players", "hist_hint": "click a fight: its board; click a player: their details", "hist_icon": "≡ history", "p_summons": "Summons", "p_max_hit": "Max hit",
        "hist_players_1": "{n} player", "gather_failed_1": " · {n} of them failed", "over_max_short_1": "+{n} soul lost beyond max", "over_max_short": "+{n} souls lost beyond max",
        "tips_hint": "Tip: hover what is underlined with dots for an explanation.",
        "tip_ring": "Pet level (0 to 3). The rings fill with its souls: 5 for level 1, 25 for level 2, 75 for level 3.",
        "tip_points": "Souls of the current level / souls needed for the next one.",
        "tip_pips": "The pet's 3 levels: filled dot = reached, ring = in progress. Right: souls of the current level / souls needed.",
        "tip_maxed": "Pet at level 3: no need to farm this monster. The game gives genus crystals instead of souls; +N = souls received beyond max, lost.",
        "tip_bar": "3 segments = levels 1, 2 and 3 (5, 25 and 75 souls).",
        "tip_kina": "Kinah earned from monsters since the daily reset, against the daily cap. Not sales or quests, and only while the Pet Meter runs. ⋯ → Daily Kinah for the cap.",
        "tip_gather_line": "Today's gathers on this server against the 1,000 limit. Gathering tab for details.",
        "tip_view_auto": "AUTO: the view follows what you do (pets, boss fight, gathering). Click a tab to keep the view on it.",
        "tip_view_locked": "LOCKED: the view no longer changes by itself. Click here to go back to AUTO.",
        "tip_footer": "In game: the Pet Meter reads the game traffic (\"game not found\": start the game; check Npcap). Sync: last upload of your collection to Aion 2 Todo. \"no key\": ⋯ → Aion 2 Todo → Sync key.",
        "tip_menu": "Menu: display, combat, language, Aion 2 Todo, tools, updates.",
        "tip_gather_count": "The game's counter: gathers started today on this server (all your characters), failures included. Read when you enter the game, then at each gather.",
        "tip_gather_sub": "Daily reset at the game's reset time. The cap time assumes your pace over the last 30 minutes. Failures are those seen by the Pet Meter since it started.",
        "tip_hist_link": "The last 50 finished fights (boss, dummy, group), with each player's details.",
        "tip_combat_total": "Damage of the whole group on your target, then the group's DPS.",
        "c_lbl_damage": "DAMAGE", "c_lbl_dps": "DPS", "c_lbl_share_boss": "% HP", "c_lbl_share_group": "SHARE", "c_lbl_player": "PLAYER · POWER",
        "tip_col_damage": "Each player's total damage on your target (summons included).",
        "tip_col_dps": "DPS: damage ÷ fight duration.",
        "tip_col_share_boss": "Share of the boss max HP ({hp} HP), like NotMeter set to \"BossMaxHp\". ⋯ → Combat to show the group share instead.",
        "tip_col_share_group": "Share of the group's damage (100% in total). ⋯ → Combat to show % of the boss HP instead.",
        "tip_stale": "No hit on the target for {n} s: out of range, dead or busy elsewhere. Their numbers have stopped.",
        "tip_col_player": "Players hitting your target, you always shown. In cyan, their combat power, read when they appear near you. Click a row: their skills.",
        "p_share_boss": "Boss HP", "tip_close": "Close (Esc)",
        "tip_rates": "Back {back} · perfect {perfect} · powerful {powerful} · multi {multi}",
        "tip_k_damage": "Total damage on the target.", "tip_k_crit": "Share of critical hits.", "tip_k_back": "Share of hits from behind.",
        "tip_k_perfect": "Share of hits the game marks as \"perfect\".", "tip_k_powerful": "Share of hits the game marks as \"powerful\".",
        "tip_k_multi": "Share of hits the game marks as \"multi-hit\".", "tip_k_max": "Their biggest hit of the fight.",
        "tip_k_time": "Fight duration: from the first to the last hit on the target.",
        "tip_c_hits": "Number of hits.", "tip_c_casts": "Uses: number of casts (one cast can land several hits).",
        "tip_c_share": "Share of their damage done by this skill.", "tip_c_avg": "Average damage per hit.", "tip_c_max": "Biggest hit of this skill.",
        "tip_c_skill": "Their skills, most damage first. Hover a skill for all its details.",
        "p_more": "+ columns", "p_less": "− columns", "tip_more": "Show or hide: uses, back, perfect, powerful, multi, average.",
        "p_summons_tip": "Hits of their summons with their own skills.",
        "tip_unknown": "Not in the official skill list (often a chain skill): its name is unknown.",
        "tip_skill": "{name}\n{hits} hits · {casts} uses · {damage} damage\ncrit {crit} · back {back} · perfect {perfect} · powerful {powerful} · multi {multi}\naverage {avg} · max {max}",
        "hist_dps": "YOUR DPS", "tip_hist_dps": "Your DPS in this fight (the top player's if you were not in it).",
        "menu_lang": "Language / Langue", "menu_lang_ui": "Interface", "menu_lang_game": "Game language (pet names)",
        "menu_todo": "Aion 2 Todo", "menu_tools": "Tools", "menu_updates": "Updates",
        "combat_menu": "Combat", "c_damage": "Total damage", "c_dps": "DPS", "c_share": "Contribution", "c_basis_group": "    as share of group damage", "c_basis_boss": "    as % of boss max HP (like NotMeter)",
        "c_dps_full": "Full DPS (27,272)", "c_damage_short": "Short damage (1.92 M)", "c_class_colors": "Bars in class colors",
        "c_rows": "{n} rows", "c_width": "Width {n} px", "c_row_h": "{n} px rows", "c_keep": "Keep the board {n} s after the fight",
        "c_keep_next": "Keep the board until the next fight", "c_import": "Take NotMeter's settings",
        "combat_imported": "combat settings taken from NotMeter", "combat_no_notmeter": "NotMeter not found on this PC",
        "rec_start": "Record traffic (5 min, diagnostics)", "rec_mark": "Mark #{n}",
        "rec_stop": "Stop recording", "rec_show": "Show recording",
        "rec_on": "● recording {t} · marks: {n}", "rec_saved": "recording saved",
        "fight_saved": "⚔ fight saved: {d} s · {dps} DPS", "fights_menu": "My fights (Aion 2 Builder)",
        "fights_on": "Record my fights", "fights_open": "Open the fights folder",
        "fights_all": "Ordinary fights too (otherwise: bosses, dummy, PvP)",
    },
}


def run_overlay():
    import ctypes
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import simpledialog

    try:  # реальные пиксели экрана - нужны для снимка окна игры
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    tr = Tracker()
    FIGHTS.enabled = tr.record_fights
    FIGHTS.all_kinds = tr.record_all_fights
    threading.Thread(target=tidy, args=(tr.record_all_fights,), daemon=True).start()  # ménage du dossier, sans retarder l'ouverture
    FIGHTS.name = tr.char or ""

    def lang_table():
        code = tr.ui_lang or ui_language()
        return {**TEXT["fr"], **TEXT.get(code, TEXT["fr"])}  # sans traduction : français (textes manquants aussi)

    L = lang_table()

    def plural(n, key):
        """Texte accordé : la clé key_1 pour 0 ou 1, key au-delà ; {n} remplacé (plus de « joueur(s) »)."""
        return L[key + "_1" if n <= 1 else key].format(n=kfmt(int(n)))

    # Палитра: глубина Бездны, эфир, золото даэвов; розовый - души, ушедшие впустую.
    def palette(name):
        t = THEMES.get(name or "abysse", THEMES["abysse"])
        return t["bg"], t["rim"], t["accent"], t["gold"], t["rose"], t["text"], t["mist"], t["done"]

    ABYSS, RIM, AETHER, GOLD, ROSE, PEARL, MIST, DONE = palette(tr.theme)

    def mix(c1, c2, t):
        """Couleur entre c1 et c2 (t = part de c2), pour les fonds teintés."""
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))
    # Premier lancement : les réglages du tableau de combat de NotMeter, s'il est installé sur ce PC (une seule fois ;
    # menu ⋯ → Combat pour les reprendre plus tard).
    notmeter_imported = False
    if not tr.combat_imported:
        found = notmeter_combat_settings()
        if found:
            tr.combat.update(found)
            notmeter_imported = True
        tr.combat_imported = True
        tr.save()
    LIVE.keep = tr.combat["keep"]
    LIVE.history = load_history()  # combats gardés des sessions précédentes (fenêtre Historique)
    hist_saved = {"v": LIVE.history_version}
    # Largeur : celle des vues Familiers et Récolte (menu Affichage), ou celle du tableau de combat (menu Combat).
    W, PAD, HERO_H, ROW_H, FOOT_H, ROWS = tr.width, 14, 80, 24, 24, 5
    RADII = (18, 25, 32)  # кольца уровней 1-3: чем больше порог, тем больше кольцо
    RING_W = 4

    events = queue.Queue()
    online = {"v": False}

    root = tk.Tk()
    root.title(APP_NAME)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.94)
    root.configure(bg=ABYSS)

    fams = set(tkfont.families())
    pick = lambda *names: next((n for n in names if n in fams), "Segoe UI")
    SERIF = pick("Sitka Subheading Semibold", "Sitka Text Semibold", "Georgia")
    SERIF_S = pick("Sitka Small", "Sitka Text", "Georgia")
    NUM = pick("Bahnschrift SemiBold Condensed", "Bahnschrift", "Segoe UI Semibold")
    NUM_M = pick("Bahnschrift SemiCondensed", "Bahnschrift", "Segoe UI")
    CAP = pick("Bahnschrift Light SemiCondensed", "Bahnschrift Light", "Segoe UI")
    f_hero_name = tkfont.Font(family=SERIF, size=14)
    f_row_name = tkfont.Font(family=SERIF_S, size=10)
    f_big = (NUM, 18)
    f_ring = (NUM, 15)  # chiffre au centre du cercle
    f_mid = (NUM_M, 12)
    f_num = (NUM, 11)
    f_cap = (CAP, 8)
    f_tab = (NUM, 9)  # onglets Familiers / Récolte
    f_cap_font = tkfont.Font(family=CAP, size=8)  # pour mesurer le texte du pied
    f_mid_font = tkfont.Font(family=NUM_M, size=12)  # pour raccourcir le nom de la ressource du petit message

    cv = tk.Canvas(root, width=W, height=200, bg=ABYSS, highlightthickness=0, bd=0)
    cv.pack()

    # Infobulles : survol d'un élément souligné de pointillés (curseur « ? »). L'astuce du pied le dit tant qu'aucune
    # infobulle n'a encore été vue.
    f_tip = ("Segoe UI", 9)
    tip_colors = lambda: (mix(ABYSS, RIM, 0.35), PEARL, RIM)

    def tips_seen():
        if not tr.tips_seen:
            tr.tips_seen = True
            dirty["v"] = True
            root.after(50, draw)
    tips = Tips(cv, tip_colors, f_tip, on_shown=tips_seen)

    def tipped(c, t, item, text, underline=True, ask=True):
        """Un élément (ou une liste) du canevas c avec une infobulle (t) : souligné de pointillés et curseur « ? », pour
        qu'on sache qu'on peut le survoler. underline=False pour une image (cercle, pastilles) ; ask=False pour un
        élément déjà cliquable (une ligne de joueur), qui garde la main."""
        items = list(item) if isinstance(item, (list, tuple)) else [item]
        if underline:
            x0, _, x1, y1 = c.bbox(items[0])
            items.append(c.create_line(x0, y1 - 1, x1, y1 - 1, fill=MIST, dash=(1, 2)))
        if ask:
            for i in items:
                c.addtag_withtag("tipq", i)
        t.add(items, text)
        return items[0]

    try:  # скруглённые углы Windows 11
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
    except Exception:
        pass
    pos = tr.pos or [24, 180]  # по умолчанию - под игровыми индикаторами слева
    root.geometry(f"+{pos[0]}+{pos[1]}")

    hits = []        # (y0, y1, mid) - для меню по строкам
    problem = {"v": None}  # (ключ заголовка, ключ пояснения, ссылка или None)
    anim = {"mid": None, "p": None, "from": 0.0, "to": 0.0, "t0": 0.0}
    ANIM_SEC = 0.45

    def progress(mid):
        """Прогресс одним числом 0..3: уровень + доля текущего кольца."""
        lvl, pts, need, _ = tr.view(mid)
        return float(lvl) if need is None else lvl + pts / need

    def fit(text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    # --- Rendu lissé : Tk ne lisse pas les cercles sous Windows (bords pixelisés). On dessine avec
    # Pillow à 4x puis on réduit (antialiasing), et on pose l'image sur le canvas.
    from PIL import Image, ImageDraw, ImageTk
    SS = 4
    img_cache = {}

    def rgba(c, a=255):
        return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), a

    def smooth(key, w, h, paint):
        if key in img_cache:
            return img_cache[key]
        if len(img_cache) > 400:
            img_cache.clear()
        im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
        paint(ImageDraw.Draw(im))
        ph = ImageTk.PhotoImage(im.resize((w, h), Image.Resampling.LANCZOS))
        img_cache[key] = ph
        return ph

    def draw_sigil(cx, cy, p, dim=False, maxed=False):
        size = 2 * (RADII[-1] + RING_W) + 4
        c = size * SS / 2

        def paint(d):
            for k, r in enumerate(RADII):
                o = (r + RING_W / 2) * SS
                box = (c - o, c - o, c + o, c + o)
                d.ellipse(box, outline=rgba(RIM), width=RING_W * SS)
                fill = 0.0 if dim else min(max(p - k, 0.0), 1.0)
                if fill >= 1.0:
                    d.ellipse(box, outline=rgba(DONE if maxed else GOLD), width=RING_W * SS)
                elif fill > 0:
                    d.arc(box, -90, -90 + 360 * fill, fill=rgba(AETHER), width=RING_W * SS)
        img = smooth(("sigil", round(p, 3), dim, maxed, RIM, GOLD, AETHER, DONE), size, size, paint)
        return cv.create_image(cx, cy, image=img)

    def draw_pips(x, cy, lvl, pts, maxed=False):
        # trois pastilles de taille croissante = les paliers 5 / 25 / 75
        def paint(d):
            for k, dd in enumerate((5, 7, 9)):
                x0 = (k * 13) * SS
                box = (x0, (10 - dd) / 2 * SS, x0 + dd * SS, (10 + dd) / 2 * SS)
                if maxed:
                    d.ellipse(box, fill=rgba(DONE))
                elif k < lvl:
                    d.ellipse(box, fill=rgba(GOLD))
                elif k == lvl and pts:
                    d.ellipse(box, outline=rgba(AETHER), width=int(1.5 * SS))
                else:
                    d.ellipse(box, outline=rgba(RIM), width=int(1.5 * SS))
        img = smooth(("pips", lvl, bool(pts), maxed, GOLD, AETHER, RIM, DONE), 36, 10, paint)
        return cv.create_image(x, cy, image=img, anchor="w")

    def draw_bar(x, y, w, h, p, maxed=False):
        """Barre en 3 segments (un par niveau), bouts arrondis."""
        gap = 4

        def paint(d):
            seg = (w - 2 * gap) / 3
            for k in range(3):
                x0 = k * (seg + gap)
                box = (x0 * SS, 0, (x0 + seg) * SS, h * SS)
                d.rounded_rectangle(box, radius=h * SS / 2, fill=rgba(RIM))
                fill = 1.0 if maxed else min(max(p - k, 0.0), 1.0)
                if fill > 0:
                    color = DONE if maxed else GOLD if fill >= 1 else AETHER
                    fw = max(seg * fill, h)
                    d.rounded_rectangle((x0 * SS, 0, (x0 + fw) * SS, h * SS), radius=h * SS / 2, fill=rgba(color))
        img = smooth(("bar", w, h, round(p, 3), maxed, RIM, GOLD, AETHER, DONE), w, h, paint)
        return cv.create_image(x, y, image=img, anchor="nw")

    def draw():
        nonlocal W
        cv.delete("all")
        tips.clear()
        hits.clear()
        layout = tr.layout or "rings"
        shown = [m for m in tr.recent if m not in tr.hidden][:((3 if layout == "mini" else ROWS) if tr.show_list else 0) + 1]
        hero, rest = (shown[0] if shown else None), shown[1:]
        hero_bottom = PAD + HERO_H
        W = tr.combat["width"] if view_now() == "combat" and not problem["v"] else tr.width
        cv.config(width=W)
        if view_now() == "combat" and not problem["v"]:
            draw_combat(hero)
            return
        if GATHER_READY and view_now() == "gather" and not problem["v"]:
            draw_gather(hero)
            return

        if layout != "rings" and not problem["v"]:
            hero_bottom = draw_compact_hero(hero, layout)
            draw_rest(rest, hero_bottom)
            return
        # --- главный монстр (или ошибка, которую нужно исправить)
        cx, cy = PAD + RADII[-1] + 2, PAD + HERO_H / 2 - 2
        tx, tw = PAD + 2 * RADII[-1] + 22, W - PAD - (PAD + 2 * RADII[-1] + 22)
        if problem["v"]:
            title, hint, url = problem["v"]
            draw_sigil(cx, cy, 0, dim=True)
            cv.create_text(cx, cy, text="!", fill=ROSE, font=f_ring)
            cv.create_text(tx, PAD + 14, text=L[title], fill=PEARL, font=f_hero_name, anchor="w")
            hint_id = cv.create_text(tx, PAD + 30, text=L[hint], fill=MIST, font=f_cap, anchor="nw", width=tw)
            bottom = cv.bbox(hint_id)[3]
            if url:
                link = cv.create_text(tx, bottom + 6, text=f"{L['download']} → {url.split('//')[1].split('/')[0]}",
                                      fill=AETHER, font=f_mid, anchor="nw", tags=("link",))
                bottom = cv.bbox(link)[3]
            hero_bottom = max(hero_bottom, bottom + 12)
            rest = shown
        elif hero is None:
            draw_sigil(cx, cy, 0, dim=True)
            cv.create_text(cx, cy, text="—", fill=MIST, font=f_ring)
            title, hint = (L["empty"], L["empty_hint"]) if tr.synced else (L["no_data"], L["no_data_hint"])
            cv.create_text(tx, cy - 14, text=fit(title, f_hero_name, tw), fill=PEARL, font=f_hero_name, anchor="w")
            cv.create_text(tx, cy + 6, text=hint, fill=MIST, font=f_cap, anchor="nw", width=tw)
        else:
            lvl, pts, need, extra = tr.view(hero)
            p = anim["p"] if anim["mid"] == hero and anim["p"] is not None else progress(hero)
            if need is None:
                # Familier au max : fond teinté sur tout le bloc, pour le voir d'un coup d'œil.
                cv.create_rectangle(1, 1, W - 2, PAD + HERO_H, fill=mix(ABYSS, DONE, 0.18), outline="")
            ring = draw_sigil(cx, cy, p, maxed=need is None)
            # Au centre, le niveau (le reste à faire est dit juste à côté) ; au max, ✓ ou les âmes perdues au-delà.
            if need is None:
                center = cv.create_text(cx, cy, text=(f"+{extra}" if extra else "✓"), fill=DONE, font=f_ring)
            else:
                center = cv.create_text(cx, cy, text=str(lvl), fill=PEARL, font=f_ring)
            tipped(cv, tips, [ring, center], L["tip_maxed"] if need is None else L["tip_ring"], underline=False)
            cv.create_text(tx, cy - 20, text=fit(tr.name(hero), f_hero_name, tw),
                           fill=MIST if need is None else PEARL, font=f_hero_name, anchor="w")
            if need is None:
                badge = cv.create_text(tx + 7, cy + 4, text=L["maxed_short"], fill=ABYSS, font=f_num, anchor="w")
                x0, y0, x1, y1 = cv.bbox(badge)
                pill = cv.create_rectangle(x0 - 7, y0 - 2, x1 + 7, y1 + 2, fill=DONE, outline="")
                cv.tag_lower(pill, badge)
                tipped(cv, tips, [badge, pill], L["tip_maxed"], underline=False)
                cv.create_text(tx, cy + 21, text=L["skip_hint"], fill=DONE, font=f_cap, anchor="w")
                if extra:
                    cv.create_text(tx, cy + 33, text=plural(extra, "over_max_short"), fill=MIST, font=f_cap, anchor="w")
            else:
                tipped(cv, tips, cv.create_text(tx, cy + 4, text=f"{pts} / {need}", fill=AETHER, font=f_mid, anchor="w"), L["tip_points"])
                cv.create_text(tx, cy + 22, text=L["left_to"].format(n=need - pts, to=L["to"][lvl]), fill=MIST, font=f_cap, anchor="w")
            hits.append((0, PAD + HERO_H, hero))

        draw_rest(rest, hero_bottom)

    def draw_rest(rest, hero_bottom):
        """Kills récents + pied (commun à tous les affichages)."""
        y = hero_bottom
        if rest:
            cv.create_line(PAD, y + 4, W - PAD, y + 4, fill=RIM)
            y += 10
            for mid in rest:
                lvl, pts, need, extra = tr.view(mid)
                cy_r = y + ROW_H / 2
                cv.create_text(PAD, cy_r, text=fit(tr.name(mid), f_row_name, 150),
                               fill=MIST if need is None else PEARL, font=f_row_name, anchor="w")
                pips = draw_pips(W - PAD - 132, cy_r, lvl, pts, maxed=need is None)
                if need is None:
                    txt, col = (f"{L['maxed_short']} +{extra}", DONE) if extra else (L["maxed_short"], DONE)
                else:
                    txt, col = f"{pts} / {need}", PEARL
                value = cv.create_text(W - PAD, cy_r, text=txt, fill=col, font=f_num, anchor="e")
                tipped(cv, tips, [pips, value], L["tip_maxed"] if need is None else L["tip_pips"], underline=False)
                hits.append((y, y + ROW_H, mid))
                y += ROW_H

        # L'autre vue en une ligne : les récoltes du jour, s'il y en a eu.
        count = tr.gather_entry()["count"]
        if GATHER_READY and count:
            y = draw_side_line(y, L["gather_line"], f"{kfmt(int(count))} / {kfmt(GATHER_CAP)}", min(count / GATHER_CAP, 1.0),
                               ROSE if count >= GATHER_CAP * GATHER_NEAR else GOLD, tip=L["tip_gather_line"])
        draw_footer(y)

    def draw_footer(y, alert=False):
        """Module Kinah, messages, pied, onglets et cadre (communs aux vues Familiers et Récolte)."""
        if tr.show_kina:
            y = draw_kina(y)

        # --- подвал
        if info_state["v"] and time.time() < info_state["v"][1] and not update_state["v"]:
            cv.create_text(PAD, y + 12, text=L[info_state["v"][0]].format(v=VERSION, **(info_state["v"][2] if len(info_state["v"]) > 2 else {})),
                           fill=AETHER, font=f_cap, anchor="w")
            y += 20
        if not tr.tips_seen:  # tant qu'aucune infobulle n'a été vue : dire qu'il y en a
            cv.create_text(PAD, y + 12, text=L["tips_hint"], fill=AETHER, font=f_cap, anchor="w")
            y += 20
        if update_state["v"]:  # mise à jour téléchargée : ligne cliquable au-dessus du pied
            cv.create_text(PAD, y + 12, text="⬆ " + L["update_ready"].format(v=update_state["v"]), fill=AETHER,
                           font=f_cap, anchor="w", tags=("update",))
            y += 20
        h = y + FOOT_H + 6
        fy = h - FOOT_H / 2 - 4
        cv.create_oval(PAD, fy - 3, PAD + 6, fy + 3, fill=AETHER if online["v"] else MIST, outline="")
        state = L["online"] if online["v"] else L["offline"]
        data = L["synced"].format(t=tr.synced) if tr.synced else L["unsynced"]
        kind, val = sync_state["v"]
        sync_txt = {"ok": L["sync_ok"].format(t=val), "error": L["sync_err"].format(m=val), "nokey": L["sync_nokey"],
                    "waiting": L["sync_waiting"]}.get(kind)
        if not sync_txt and tr.synced and tr.synced.startswith("site"):
            sync_txt = L["site_data"].format(t=tr.synced[5:])
        foot = f"{state} · " + (sync_txt if sync_txt else data)  # la synchro (heure) suffit quand elle marche
        if RECORDER.active:
            left = RECORDER.remaining()
            foot = L["rec_on"].format(t=f"{left // 60}:{left % 60:02d}", n=RECORDER.marks)
        status = cv.create_text(PAD + 12, fy, text=fit(foot, f_cap_font, W - 2 * PAD - 34),
                                fill=ROSE if RECORDER.active or kind in ("error", "nokey") else MIST, font=f_cap, anchor="w")
        tipped(cv, tips, status, L["tip_footer"])
        tipped(cv, tips, cv.create_text(W - PAD, fy, text="⋯", fill=MIST, font=f_mid, anchor="e", tags="menu"), L["tip_menu"], underline=False, ask=False)

        if tabs_shown():  # tout descend sous les onglets
            cv.move("all", 0, TABS_H)
            hits[:] = [(y0 + TABS_H, y1 + TABS_H, mid) for y0, y1, mid in hits]
            draw_tabs()
            h += TABS_H
        cv.create_rectangle(0, 0, W - 1, h - 1, outline=ROSE if alert else RIM)

        cv.config(height=h)
        root.geometry(f"{W}x{h}")

    def tabs_shown():
        return (tr.layout or "rings") != "mini"

    def draw_tabs():
        """Onglets Familiers / Récolte (clic : vue verrouillée) ; à droite AUTO, ou FIXÉE (clic : retour en auto)."""
        cur = view_now()
        x = PAD
        tabs = [("pets", L["tab_pets"]), ("combat", L["tab_combat"])] + ([("gather", L["tab_gather"])] if GATHER_READY else [])
        for key, label in tabs:
            item = cv.create_text(x, TABS_H / 2, text=label, fill=PEARL if cur == key else MIST, font=f_tab, anchor="w", tags=("tab", f"tab_{key}"))
            x0, _, x1, _ = cv.bbox(item)
            if cur == key:
                cv.create_line(x0, TABS_H - 2, x1, TABS_H - 2, fill={"gather": GOLD, "combat": ROSE}.get(key, AETHER), width=2)
            x = x1 + 14
        locked = tr.view_mode != "auto"
        mode = cv.create_text(W - PAD, TABS_H / 2, text=L["view_locked"] if locked else L["view_auto"], fill=MIST, font=f_tab,
                              anchor="e", tags=("tab", "tab_auto"))
        x0 = cv.bbox(mode)[0]
        dot = cv.create_oval(x0 - 10, TABS_H / 2 - 3, x0 - 4, TABS_H / 2 + 3, fill=MIST if locked else AETHER, outline="", tags=("tab", "tab_auto"))
        tipped(cv, tips, [mode, dot], L["tip_view_locked"] if locked else L["tip_view_auto"], underline=False, ask=False)
        cv.create_line(0, TABS_H, W, TABS_H, fill=RIM)

    def draw_side_line(y, label, value, ratio, color, tip=None):
        """L'autre vue en une ligne : libellé (avec son infobulle), barre fine, valeur."""
        cv.create_line(PAD, y + 4, W - PAD, y + 4, fill=RIM)
        cy = y + 18
        label_id = cv.create_text(PAD, cy, text=label, fill=PEARL, font=f_cap, anchor="w")
        if tip:
            tipped(cv, tips, label_id, tip)
        value_id = cv.create_text(W - PAD, cy, text=value, fill=color, font=f_num, anchor="e")
        if ratio is not None:
            x0 = PAD + f_cap_font.measure(label) + 10
            x1 = cv.bbox(value_id)[0] - 10
            if x1 - x0 > 20:
                draw_line_bar(x0, cy - 2, x1 - x0, 4, ratio, color)
        return cy + 12

    def draw_line_bar(x, y, w, h, p, color):
        def paint(d):
            d.rounded_rectangle((0, 0, w * SS, h * SS), radius=h * SS / 2, fill=rgba(RIM))
            if p > 0:
                d.rounded_rectangle((0, 0, max(w * p, h) * SS, h * SS), radius=h * SS / 2, fill=rgba(color))
        img = smooth(("lbar", int(w), h, round(p, 3), color, RIM), int(w), h, paint)
        cv.create_image(x, y, image=img, anchor="nw")

    def next_reset():
        """Prochain reset quotidien (07:00 UTC), en secondes depuis l'époque."""
        return (int((time.time() - DAILY_RESET_H * 3600) // 86400) + 1) * 86400 + DAILY_RESET_H * 3600

    # ---- vue Combat (compteur de dégâts en direct, comme un DPS meter)
    classes, skill_names, icon_names = load_skills()
    icons = SkillIcons(icon_names)
    decimal = "," if (tr.ui_lang or ui_language()) in ("fr", "ru") else "."
    PW = 470  # largeur de la fenêtre de détail d'un joueur, colonnes compactes (PW_FULL : toutes)

    def view_now():
        """Vue affichée : celle verrouillée d'un clic, sinon Combat pendant un combat de boss (un familier gagné pendant le
        combat est compté sans prendre la place), sinon la dernière activité (monstres ou récolte)."""
        ready = lambda v: v if v != "gather" or GATHER_READY else "pets"
        if tr.view_mode != "auto":
            return ready(tr.view_mode)
        if LIVE.showing():
            return "combat"
        return ready(tr.auto_view)

    def short(n):
        """1 922 678 → « 1,92 M », 27 272 → « 27,3 k »."""
        if n >= 1e6:
            return f"{n / 1e6:.2f} M".replace(".", decimal)
        if n >= 1e4:
            return f"{n / 1e3:.1f} k".replace(".", decimal)
        return kfmt(int(n))

    def player_label(who, number, name=None):
        if who == "me":
            return tr.char or FIGHTS.name or L["you"]
        if name:
            return name
        cls, n = number or (0, 0)
        return f"{classes.get(cls, L['player'])} {n}"

    f_cb_name = tkfont.Font(family=NUM_M, size=10)  # tableau de combat : police compacte, comme NotMeter
    f_cb_val = (NUM, 10)

    def draw_combat(hero):
        """Vue Combat, compacte comme NotMeter : une ligne d'en-tête (cible, durée, total et DPS du groupe, ≡ historique),
        les noms des colonnes (survol : ce qu'elles comptent), puis une ligne par joueur (barre à la couleur de sa classe,
        nom, puissance de combat, colonnes choisies), toi toujours visible ; clic sur une ligne : ses sorts et ses dégâts.
        En bas, le dernier familier en une ligne."""
        b = LIVE.board()
        y = PAD - 4
        hist_id = cv.create_text(W - PAD, y + 9, text=L["hist_icon"], fill=AETHER, font=f_cap, anchor="e", tags=("hist",))
        tipped(cv, tips, hist_id, L["tip_hist_link"], underline=False, ask=False)
        if not b or not b["players"]:
            cv.create_text(PAD, y + 12, text=L["combat_none"], fill=PEARL, font=f_row_name, anchor="w")
            hint = cv.create_text(PAD, y + 24, text=L["combat_none_hint"], fill=MIST, font=f_cap, anchor="nw", width=W - 2 * PAD)
            y = cv.bbox(hint)[3] + 8
        else:
            colors = tr.combat["colors"]
            total, dur = b["total"], b["duration"]
            title = L["combat_done"] if b["ended"] else L["combat_dummy"] if b["code"] in TRAINING_DUMMIES else L["combat_boss"]
            head = cv.create_text(PAD, y + 9, text=title.upper(), fill=MIST if b["ended"] else PEARL, font=f_tab, anchor="w")
            cv.create_text(cv.bbox(head)[2] + 8, y + 9, text=f"{int(dur) // 60}:{int(dur) % 60:02d}",
                           fill=MIST if b["ended"] else colors["time"], font=f_tab, anchor="w")
            tipped(cv, tips, cv.create_text(cv.bbox(hist_id)[0] - 10, y + 9, text=L["combat_sub"].format(total=short(total), dps=short(total / dur)),
                                            fill=MIST, font=f_cap, anchor="e"), L["tip_combat_total"])
            y = board_rows(cv, W, y + 20, b, tr.combat["rows"], "prow", t=tips) + 2
        if hero is not None:  # le familier gagné pendant le combat est compté, sans prendre la place du tableau
            lvl, pts, need, extra = tr.view(hero)
            y = draw_side_line(y, fit(f"{L['pet_line']} · {tr.name(hero)}", f_cap_font, 210),
                               L["maxed_short"] if need is None else f"{pts} / {need}", None, DONE if need is None else AETHER)
        draw_footer(y)

    def contribution(b):
        """Contribution d'un tableau : (libellé court, libellé du détail, texte de l'infobulle, fonction dégâts -> « 5,4 % »).
        Part des dégâts du groupe, ou, comme NotMeter réglé sur « BossMaxHp », % des PV max du boss (lus à son
        apparition ; sans eux, ou sur un mannequin qui se régénère, la part du groupe). La même partout : tableau, détail."""
        boss_hp = b.get("max_hp") if tr.combat["share_basis"] == "boss" and b["code"] not in TRAINING_DUMMIES else None
        if boss_hp:
            return (L["c_lbl_share_boss"], L["p_share_boss"], L["tip_col_share_boss"].format(hp=short(boss_hp)),
                    lambda d: f"{100 * d / boss_hp:.1f} %".replace(".", decimal))
        return (L["c_lbl_share_group"], L["p_share"], L["tip_col_share_group"], lambda d: f"{round(100 * d / max(b['total'], 1))} %")

    def board_rows(c, width, y, b, rows, prefix, me_label=None, t=None):
        """Les noms des colonnes (avec leur infobulle, sur les Tips t), puis les lignes des joueurs d'un tableau (combat en
        cours, ou de l'historique) sur le canevas c, de la largeur width ; renvoie le bas. Étiquettes prefix et
        prefix_<joueur> : le clic ouvre le détail."""
        cfg, colors = tr.combat, tr.combat["colors"]
        players, dur = b["players"], b["duration"]
        ranks = {who: i + 1 for i, (who, *_) in enumerate(players)}
        row_h = cfg["row_h"]
        shown = players[:rows]
        if all(who != "me" for who, *_ in shown):  # toi toujours visible, à ton rang
            me = next((p for p in players if p[0] == "me"), None)
            if me:
                shown = players[:rows - 1] + [me]
        top = max(players[0][1]["damage"], 1)
        share_lbl, _, share_tip, share = contribution(b)
        # Colonnes choisies (comme NotMeter : dégâts totaux, DPS, contribution), de gauche à droite, calées à droite.
        columns = [(key, w) for key, w in (("damage", 50), ("dps", 50), ("share", 40 if share_lbl == L["c_lbl_share_boss"] else 32)) if cfg[key]]
        labels = {"damage": (L["c_lbl_damage"], L["tip_col_damage"]), "dps": (L["c_lbl_dps"], L["tip_col_dps"]), "share": (share_lbl, share_tip)}
        x = width - PAD - 4
        for key, w in reversed(columns):
            item = c.create_text(x, y + 6, text=labels[key][0], fill=MIST, font=f_cap, anchor="e")
            if t:
                tipped(c, t, item, labels[key][1])
            x -= w
        item = c.create_text(PAD + 20, y + 6, text=L["c_lbl_player"], fill=MIST, font=f_cap, anchor="w")
        if t:
            tipped(c, t, item, L["tip_col_player"])
        y += 14
        now = time.time()
        for who, s, number, name, power in shown:
            tag = (prefix, f"{prefix}_{who}")
            mine = who == "me"
            # Combat en cours : un joueur qui ne frappe plus la cible depuis 10 s (sorti de portée, mort, occupé ailleurs)
            # passe en gris ; ses chiffres n'avancent plus, le serveur n'envoie que ce qui est près de toi.
            idle = now - s["last"]
            stale = not b.get("ended", True) and not mine and idle > STALE_SECONDS
            base = CLASS_COLORS.get(s["cls"], MIST) if cfg["class_colors"] else (AETHER if mine else MIST)
            if stale:
                base = mix(base, ABYSS, 0.5)
            # Barre sombre (les chiffres restent lisibles dessus) et liseré vif à gauche, à la couleur de la classe.
            c.create_rectangle(PAD, y + 1, PAD + max(3, (width - 2 * PAD) * s["damage"] / top), y + row_h - 1,
                               fill=mix(ABYSS, base, 0.3 if mine else 0.2), outline="", tags=tag)
            c.create_rectangle(PAD, y + 1, PAD + 3, y + row_h - 1, fill=base, outline="", tags=tag)
            cy = y + row_h / 2
            c.create_text(PAD + 7, cy, text=str(ranks[who]), fill=AETHER if mine else MIST, font=f_cap, anchor="w", tags=tag)
            values = {"damage": (short(s["damage"]) if cfg["damage_short"] else kfmt(int(s["damage"])), colors["damage"]),
                      "dps": (kfmt(int(s["damage"] / dur)) if cfg["dps_full"] else short(s["damage"] / dur), colors["dps"]),
                      "share": (share(s["damage"]), colors["share"])}
            x = width - PAD - 4
            for key, w in reversed(columns):
                text, color = values[key]
                c.create_text(x, cy, text=text, fill=MIST if stale else color, font=f_cb_val, anchor="e", tags=tag)
                x -= w
            power_txt = kfmt(power) if cfg["power"] and power else ""
            room = x - (PAD + 20) - 6 - (f_cap_font.measure(power_txt) + 6 if power_txt else 0)
            text = me_label if mine and me_label else player_label(who, number, name)
            label = c.create_text(PAD + 20, cy, text=fit(text, f_cb_name, max(40, room)), fill=MIST if stale else PEARL, font=f_cb_name, anchor="w", tags=tag)
            if stale and t:
                tipped(c, t, label, L["tip_stale"].format(n=int(idle)))
            if power_txt:
                c.create_text(c.bbox(label)[2] + 6, cy + 1, text=power_txt, fill=MIST if stale else colors["power"], font=f_cap, anchor="w", tags=tag)
            y += row_h
        return y

    popups = {}
    f_pop_title = tkfont.Font(family=NUM, size=13)
    ICON = 18  # icônes des sorts dans le détail d'un joueur
    PW_FULL = 680  # largeur du détail avec toutes les colonnes (PW : sans)

    def pct(n, d):
        return f"{round(100 * n / max(d, 1))} %"

    def open_player(who, snap=None, at=None):
        """Détail d'un joueur, comme dans un DPS meter : ses chiffres clés et ses sorts sur le boss (icône officielle), mis à
        jour chaque seconde ; snap : un combat de l'historique au lieu du combat en cours. Compact par défaut (dégâts, DPS,
        contribution, critique, coup max, durée ; par sort : coups, dégâts, %, critique, max) ; « + colonnes » ajoute
        utilisations, de dos, parfaits, puissants, multiples et moyenne (choix gardé). Le survol d'un sort donne tout son
        détail. Fenêtre sans cadre Windows, à l'image de l'overlay : on la déplace par n'importe où, × ou Échap la ferme."""
        key = (who, id(snap))
        old = popups.get(key)
        if old is not None and old.winfo_exists():
            old.lift()
            return
        win = tk.Toplevel(root)
        win.title(APP_NAME)
        win.overrideredirect(True)
        win.configure(bg=ABYSS)
        win.attributes("-topmost", True)
        win.attributes("-alpha", opacity["v"])
        x0, y0 = at or (root.winfo_x() + W + 8, root.winfo_y())
        win.geometry(f"+{max(0, min(x0, win.winfo_screenwidth() - PW_FULL - 8))}+{y0}")  # toujours dans l'écran
        pc = tk.Canvas(win, width=PW, height=120, bg=ABYSS, highlightthickness=0)
        pc.pack()
        pt = Tips(pc, tip_colors, f_tip, on_shown=tips_seen)
        pc.tag_bind("tipq", "<Enter>", lambda e: pc.config(cursor="question_arrow"))
        pc.tag_bind("tipq", "<Leave>", lambda e: pc.config(cursor=""))
        pc.tag_bind("more", "<Enter>", lambda e: pc.config(cursor="hand2"))
        pc.tag_bind("more", "<Leave>", lambda e: pc.config(cursor=""))
        popups[key] = win
        grab = {}

        def press(e):
            grab.update(x=e.x_root - win.winfo_x(), y=e.y_root - win.winfo_y(), moved=False)

        def move(e):
            grab["moved"] = True
            win.geometry(f"+{e.x_root - grab['x']}+{e.y_root - grab['y']}")

        def release(e):
            if grab.get("moved"):
                return
            tags = {t for i in pc.find_overlapping(e.x - 6, e.y - 6, e.x + 6, e.y + 6) for t in pc.gettags(i)}
            if "close" in tags:
                win.destroy()
            elif "more" in tags:  # + colonnes / − colonnes
                tr.detail_full = not tr.detail_full
                dirty["v"] = True
                render()
        pc.bind("<ButtonPress-1>", press)
        pc.bind("<B1-Motion>", move)
        pc.bind("<ButtonRelease-1>", release)
        win.bind("<Escape>", lambda e: win.destroy())
        win.after(60, win.focus_force)  # sans cadre, la fenêtre ne prend pas le clavier d'elle-même (Échap)

        def render():
            if not win.winfo_exists():
                return
            full = tr.detail_full
            pw = PW_FULL if full else PW
            b = snap or LIVE.board()
            row = next((p for p in b["players"] if p[0] == who), None) if b else None
            pc.delete("all")
            pt.clear()
            pc.config(width=pw)
            tipped(pc, pt, pc.create_text(pw - 12, 14, text="×", fill=MIST, font=f_mid, tags=("close",)), L["tip_close"], underline=False, ask=False)
            y = 12
            if row is None:
                pc.create_text(14, y + 8, text=L["combat_gone"], fill=MIST, font=f_cap, anchor="w")
                y += 30
            else:
                _, s, number, name, power = row
                dur, colors = b["duration"], tr.combat["colors"]
                _, share_lbl, share_tip, share = contribution(b)
                tint = CLASS_COLORS.get(s["cls"], AETHER)
                pc.create_rectangle(0, 0, pw, 3, fill=tint, outline="")  # la couleur de sa classe, en haut
                label = (snap.get("me") or player_label(who, number, name)) if snap and who == "me" else player_label(who, number, name)
                pc.create_text(14, y + 9, text=label, fill=PEARL, font=f_pop_title, anchor="w")
                sub = classes.get(s["cls"], "") + (f" · {L['p_power']} {kfmt(power)}" if power else "")
                pc.create_text(14, y + 27, text=sub, fill=MIST, font=f_cap, anchor="w")
                y += 42
                hits_n = s["hits"]
                rates = L["tip_rates"].format(back=pct(s.get("back", 0), hits_n), perfect=pct(s.get("perfect", 0), hits_n),
                                              powerful=pct(s.get("powerful", 0), hits_n), multi=pct(s.get("multi", 0), hits_n))
                # Les chiffres clés sur une ligne : libellé (souligné : son infobulle) au-dessus, valeur en dessous.
                kpis = [(L["p_damage"], short(s["damage"]), PEARL, L["tip_k_damage"]),
                        (L["p_dps"], short(s["damage"] / dur), colors["dps"], L["tip_col_dps"]),
                        (share_lbl, share(s["damage"]), colors["share"], share_tip),
                        (L["p_crit"], pct(s["crits"], hits_n), PEARL, L["tip_k_crit"] + ("" if full else "\n" + rates))]
                if full:
                    kpis += [(L["p_back"], pct(s.get("back", 0), hits_n), PEARL, L["tip_k_back"]),
                             (L["p_perfect"], pct(s.get("perfect", 0), hits_n), PEARL, L["tip_k_perfect"]),
                             (L["p_powerful"], pct(s.get("powerful", 0), hits_n), PEARL, L["tip_k_powerful"]),
                             (L["p_multi"], pct(s.get("multi", 0), hits_n), PEARL, L["tip_k_multi"])]
                kpis += [(L["p_max_hit"], short(max((k["max"] for k in s["skills"].values()), default=0)), PEARL, L["tip_k_max"]),
                         (L["p_time"], f"{int(dur) // 60}:{int(dur) % 60:02d}", colors["time"], L["tip_k_time"])]
                col = (pw - 28) / len(kpis)
                for i, (k, v, c, tip) in enumerate(kpis):
                    x = 14 + i * col
                    tipped(pc, pt, pc.create_text(x, y, text=k.upper(), fill=MIST, font=f_cap, anchor="nw"), tip)
                    pc.create_text(x, y + 13, text=v, fill=c, font=f_cb_val, anchor="nw")
                y += 38
                pc.create_line(14, y, pw - 14, y, fill=RIM)
                y += 10
                # Colonnes : compactes, ou toutes (+ colonnes). (libellé, clé, x depuis la droite, infobulle)
                if full:
                    spec = [(L["p_hits"], "hits", 444, L["tip_c_hits"]), (L["p_casts"], "casts", 404, L["tip_c_casts"]),
                            (L["p_damage"], "damage", 350, L["tip_k_damage"]), ("%", "share", 310, L["tip_c_share"]),
                            (L["p_crit_s"], "crits", 266, L["tip_k_crit"]), (L["p_back"], "back", 224, L["tip_k_back"]),
                            (L["p_perfect_s"], "perfect", 180, L["tip_k_perfect"]), (L["p_powerful_s"], "powerful", 136, L["tip_k_powerful"]),
                            (L["p_multi_s"], "multi", 98, L["tip_k_multi"]), (L["p_avg"], "avg", 58, L["tip_c_avg"]),
                            (L["p_max"], "max", 14, L["tip_c_max"])]
                else:
                    spec = [(L["p_hits"], "hits", 214, L["tip_c_hits"]), (L["p_damage"], "damage", 152, L["tip_k_damage"]),
                            ("%", "share", 110, L["tip_c_share"]), (L["p_crit_s"], "crits", 60, L["tip_k_crit"]),
                            (L["p_max"], "max", 14, L["tip_c_max"])]
                head = pc.create_text(14 + ICON + 6, y, text=L["p_skill"].upper(), fill=MIST, font=f_cap, anchor="w")
                tipped(pc, pt, head, L["tip_c_skill"])
                for label_txt, _, dx, tip in spec:
                    tipped(pc, pt, pc.create_text(pw - dx, y, text=label_txt.upper(), fill=MIST, font=f_cap, anchor="e"), tip)
                link = pc.create_text(pc.bbox(head)[2] + 12, y, text=L["p_less"] if full else L["p_more"], fill=AETHER, font=f_cap,
                                      anchor="w", tags=("more",))
                tipped(pc, pt, link, L["tip_more"], underline=False, ask=False)
                y += 10
                skills = sorted(s["skills"].items(), key=lambda kv: -kv[1]["damage"])[:16]
                best = max((k["damage"] for _, k in skills), default=1) or 1
                name_w = pw - spec[0][2] - 36 - (14 + ICON + 6)
                for code, k in skills:
                    cy = y + 12
                    img = icons.get(code, ICON)
                    if img:
                        pc.create_image(14, cy, image=img, anchor="w")
                    else:  # pas d'icône (enchaînement absent de l'armurerie, invocations, ou en cours de téléchargement)
                        pc.create_rectangle(14, cy - ICON / 2, 14 + ICON, cy + ICON / 2, fill=mix(ABYSS, tint, 0.35), outline="")
                    known = code in skill_names or code == 0
                    label = L["p_summons"] if code == 0 else skill_names[code] if known else f"{L['p_unknown']} · {code}"
                    x0 = 14 + ICON + 6
                    name_id = pc.create_text(x0, cy - 1, text=fit(label, f_cb_name, name_w), fill=PEARL if known else MIST, font=f_cb_name, anchor="w")
                    # part de ses dégâts : une barre fine sous le nom, à la couleur de sa classe
                    pc.create_rectangle(x0, cy + 8, x0 + max(2, name_w * k["damage"] / best), cy + 10, fill=tint, outline="")
                    n = k["hits"]
                    values = {"hits": kfmt(n), "casts": kfmt(k["casts"]) if k.get("casts") else "—", "damage": short(k["damage"]),
                              "share": pct(k["damage"], s["damage"]), "crits": pct(k["crits"], n), "back": pct(k.get("back", 0), n),
                              "perfect": pct(k.get("perfect", 0), n), "powerful": pct(k.get("powerful", 0), n), "multi": pct(k.get("multi", 0), n),
                              "avg": short(k["damage"] / max(n, 1)), "max": short(k["max"])}
                    cells = [pc.create_text(pw - dx, cy, text=values[key], fill=colors["share"] if key == "share" else MIST if key == "casts" else PEARL,
                                            font=f_cb_val, anchor="e") for _, key, dx, _ in spec]
                    # Survol d'un sort : tout son détail, même en vue compacte (et ce qu'est un sort inconnu ou les invocations).
                    about = L["p_summons_tip"] + "\n" if code == 0 else "" if known else L["tip_unknown"] + "\n"
                    detail = L["tip_skill"].format(name=label, hits=kfmt(n), casts=kfmt(k.get("casts", 0)), damage=short(k["damage"]),
                                                   crit=values["crits"], back=values["back"], perfect=values["perfect"],
                                                   powerful=values["powerful"], multi=values["multi"], avg=values["avg"], max=values["max"])
                    row_bg = pc.create_rectangle(10, cy - 11, pw - 10, cy + 12, fill="", outline="")
                    tipped(pc, pt, [row_bg, name_id, *cells], about + detail, underline=False)
                    y += 24
            y += 8
            pc.create_rectangle(0, 0, pw - 1, y - 1, outline=RIM)
            pc.config(height=y)

        def tick():
            if win.winfo_exists():
                render()
                if snap is None:
                    win.after(1000, tick)
        tick()

    HW, HIST_ROWS = 440, 12  # fenêtre Historique : largeur, combats visibles à la fois (molette pour les autres)
    hist = {"win": None}

    def open_history():
        """Historique des combats, comme le livre de NotMeter : les combats finis (boss, mannequin, groupe), du plus récent
        au plus ancien ; clic sur un combat : son tableau ; clic sur un joueur : son détail. Sans cadre, comme l'overlay ;
        on la déplace par n'importe où, × ou Échap la ferme."""
        if hist["win"] is not None and hist["win"].winfo_exists():
            hist["win"].lift()
            return
        win = tk.Toplevel(root)
        win.title(APP_NAME)
        win.overrideredirect(True)
        win.configure(bg=ABYSS)
        win.attributes("-topmost", True)
        win.attributes("-alpha", opacity["v"])
        win.geometry(f"+{max(0, min(root.winfo_x() + W + 8, win.winfo_screenwidth() - HW - 8))}+{root.winfo_y()}")
        hc = tk.Canvas(win, width=HW, height=120, bg=ABYSS, highlightthickness=0)
        hc.pack()
        ht = Tips(hc, tip_colors, f_tip, on_shown=tips_seen)
        hc.tag_bind("tipq", "<Enter>", lambda e: hc.config(cursor="question_arrow"))
        hc.tag_bind("tipq", "<Leave>", lambda e: hc.config(cursor=""))
        win.after(60, win.focus_force)  # sans cadre, la fenêtre ne prend pas le clavier d'elle-même (Échap)
        hist["win"] = win
        view = {"fight": None, "top": 0}
        grab = {}

        def fights():
            with LIVE.lock:
                return list(LIVE.history)

        def press(e):
            grab.update(x=e.x_root - win.winfo_x(), y=e.y_root - win.winfo_y(), moved=False)

        def move(e):
            grab["moved"] = True
            win.geometry(f"+{e.x_root - grab['x']}+{e.y_root - grab['y']}")

        def release(e):
            if grab.get("moved"):
                return
            tags = {t for i in hc.find_overlapping(e.x, e.y, e.x, e.y) for t in hc.gettags(i)}
            tags |= {t for i in hc.find_overlapping(e.x - 6, e.y - 6, e.x + 6, e.y + 6) for t in hc.gettags(i) if t in ("close", "back")}
            if "close" in tags:
                win.destroy()
            elif "back" in tags:
                view["fight"] = None
                render()
            elif (pick := next((t for t in tags if t.startswith("hfight_")), None)) is not None:
                view["fight"] = fights()[int(pick[7:])]
                render()
            elif view["fight"] is not None and (row := next((t for t in tags if t.startswith("hrow_")), None)) is not None:
                open_player("me" if row[5:] == "me" else int(row[5:]), view["fight"], at=(win.winfo_x() + HW + 8, win.winfo_y()))

        def wheel(e):
            if view["fight"] is None:
                view["top"] = max(0, min(len(fights()) - HIST_ROWS, view["top"] - (1 if e.delta > 0 else -1)))
                render()
        hc.bind("<ButtonPress-1>", press)
        hc.bind("<B1-Motion>", move)
        hc.bind("<ButtonRelease-1>", release)
        hc.bind("<MouseWheel>", wheel)
        win.bind("<Escape>", lambda e: win.destroy())
        for t in ("hfight", "hrow", "back"):
            hc.tag_bind(t, "<Enter>", lambda e: hc.config(cursor="hand2"))
            hc.tag_bind(t, "<Leave>", lambda e: hc.config(cursor=""))

        def kind(snap):
            return L["combat_dummy"] if snap["code"] in TRAINING_DUMMIES else L["combat_boss"]

        def clock(seconds):
            return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"

        def render():
            if not win.winfo_exists():
                return
            hc.delete("all")
            ht.clear()
            tipped(hc, ht, hc.create_text(HW - 12, 14, text="×", fill=MIST, font=f_mid, tags=("close",)), L["tip_close"], underline=False, ask=False)
            y = 12
            snap = view["fight"]
            if snap is None:
                hc.create_text(14, y + 8, text=L["hist_title"], fill=PEARL, font=f_tab, anchor="w")
                hc.create_text(14, y + 24, text=L["hist_hint"], fill=MIST, font=f_cap, anchor="w")
                tipped(hc, ht, hc.create_text(HW - 16, y + 24, text=L["hist_dps"], fill=MIST, font=f_cap, anchor="e"), L["tip_hist_dps"])
                y += 38
                all_fights = fights()
                if not all_fights:
                    note = hc.create_text(14, y + 4, text=L["hist_empty"], fill=MIST, font=f_cap, anchor="nw", width=HW - 28)
                    y = hc.bbox(note)[3] + 10
                for i, f in list(enumerate(all_fights))[view["top"]:view["top"] + HIST_ROWS]:
                    tag = ("hfight", f"hfight_{i}")
                    me = next((p for p in f["players"] if p[0] == "me"), None)
                    dps = (me or f["players"][0])[1]["damage"] / f["duration"]
                    hc.create_rectangle(10, y, HW - 10, y + 26, fill=mix(ABYSS, RIM, 0.5 if i % 2 else 0.25), outline="", tags=tag)
                    hc.create_text(16, y + 13, text=time.strftime("%d/%m %H:%M", time.localtime(f["t"])), fill=MIST, font=f_cap, anchor="w", tags=tag)
                    hc.create_text(92, y + 13, text=f"{kind(f)} · {clock(f['duration'])}", fill=PEARL, font=f_cb_name, anchor="w", tags=tag)
                    hc.create_text(HW - 120, y + 13, text=plural(len(f["players"]), "hist_players"), fill=MIST, font=f_cap, anchor="e", tags=tag)
                    hc.create_text(HW - 16, y + 13, text=f"{kfmt(int(dps))}/s", fill=tr.combat["colors"]["dps"], font=f_cb_val, anchor="e", tags=tag)
                    y += 28
            else:
                hc.create_text(14, y + 8, text=L["hist_back"], fill=AETHER, font=f_cap, anchor="w", tags=("back",))
                y += 24
                head = hc.create_text(14, y + 8, text=f"{kind(snap).upper()}  {clock(snap['duration'])}", fill=PEARL, font=f_tab, anchor="w")
                hc.create_text(hc.bbox(head)[2] + 10, y + 8, text=time.strftime("%d/%m %H:%M", time.localtime(snap["t"])), fill=MIST, font=f_cap, anchor="w")
                hc.create_text(HW - PAD, y + 8, text=L["combat_sub"].format(total=short(snap["total"]), dps=short(snap["total"] / snap["duration"])),
                               fill=MIST, font=f_cap, anchor="e")
                y = board_rows(hc, HW, y + 20, snap, 12, "hrow", me_label=snap.get("me"), t=ht) + 6
            y += 6
            hc.create_rectangle(0, 0, HW - 1, y - 1, outline=RIM)
            hc.config(height=y)

        def tick():
            if win.winfo_exists():
                render()
                win.after(2000, tick)  # un combat qui se termine s'ajoute à la liste
        tick()

    live_seen = {"v": -1, "show": False}

    def live_tick():
        """Deux fois par seconde : la vue Combat suit les coups comptés ; l'overlay reste visible pendant un combat."""
        LIVE.tick(time.time())
        showing = LIVE.showing()
        if showing:
            hide["last"] = time.time()
            set_hidden(False)
        if LIVE.version != live_seen["v"] or showing != live_seen["show"]:
            switched = showing != live_seen["show"]
            live_seen.update(v=LIVE.version, show=showing)
            if switched or view_now() == "combat":
                draw()
        root.after(500, live_tick)

    def draw_gather(hero):
        """Vue Récolte : récoltes du jour (compteur du jeu, ratées comprises) sur le plafond (alerte dès 90 %), ressources
        récoltées (nom d'après GATHER_ITEMS), par serveur ; en bas, le dernier familier en une ligne."""
        entry, cap = tr.gather_entry(), GATHER_CAP
        count = entry["count"]
        full, near = count >= cap, count >= cap * GATHER_NEAR
        color = ROSE if near else GOLD
        reset = next_reset()
        reset_at = time.strftime("%H:%M", time.localtime(reset))
        y = PAD
        cv.create_text(PAD, y + 10, text=L["gather_full_title"] if full else L["gather_title"], fill=PEARL, font=f_hero_name, anchor="w")
        cap_id = cv.create_text(W - PAD, y + 12, text=f" / {kfmt(cap)}", fill=MIST, font=f_num, anchor="e")
        tipped(cv, tips, cv.create_text(cv.bbox(cap_id)[0], y + 10, text=kfmt(int(count)), fill=color, font=f_big, anchor="e"), L["tip_gather_count"])
        draw_line_bar(PAD, y + 28, W - 2 * PAD, 6, min(count / cap, 1.0), color)
        if full:
            left = int(reset - time.time())
            sub = L["gather_blocked"].format(t=reset_at, d=f"{left // 3600} h {left % 3600 // 60:02d}")
        elif near:
            sub = L["gather_near"].format(n=cap - count)
        else:
            sub = L["gather_left"].format(n=kfmt(cap - count), t=reset_at)
            rate = tr.gather_rate()
            if rate and time.time() + (cap - count) / rate * 3600 < reset:
                sub += L["gather_eta"].format(t=time.strftime("%H:%M", time.localtime(time.time() + (cap - count) / rate * 3600)))
        if entry["failed"]:
            sub += plural(entry["failed"], "gather_failed")
        sub_id = cv.create_text(PAD, y + 42, text=sub, fill=ROSE if near and not full else MIST, font=f_cap, anchor="nw", width=W - 2 * PAD)
        tipped(cv, tips, sub_id, L["tip_gather_sub"])
        y = cv.bbox(sub_id)[3] + 6

        # Ressources du jour sur ce serveur, de la plus récoltée à la moins : les 5 premières, puis « + n autres ».
        items = sorted(entry["items"].items(), key=lambda kv: -kv[1])
        if items and tr.show_list:
            cv.create_line(PAD, y, W - PAD, y, fill=RIM)
            cv.create_text(PAD, y + 12, text=L["gather_items"], fill=MIST, font=f_cap, anchor="w")
            y += 22
            for code, qty in items[:5]:
                cy = y + 11
                name = tr.item_name(code) or L["item_unknown"].format(id=int.from_bytes(bytes.fromhex(code), "little"))
                cv.create_text(PAD, cy, text=fit(name, f_cap_font, 200), fill=PEARL, font=f_cap, anchor="w")
                cv.create_text(W - PAD, cy, text=f"×{kfmt(qty)}", fill=GOLD, font=f_num, anchor="e")
                y += 22
            if len(items) > 5:
                cv.create_text(PAD, y + 9, text=plural(len(items) - 5, "gather_more"), fill=MIST, font=f_cap, anchor="w")
                y += 20
            y += 4

        # Par serveur (ou par perso tant que le serveur n'est pas lu), dès qu'il y en a plusieurs aujourd'hui.
        by = tr.gather_today()
        if len(by) > 1 and tr.show_list:
            cv.create_line(PAD, y, W - PAD, y, fill=RIM)
            per_server = not any(k.startswith("@") for k in by)
            cv.create_text(PAD, y + 12, text=L["gather_servers"] if per_server else L["gather_chars"], fill=MIST, font=f_cap, anchor="w")
            y += 22
            for key, e in sorted(by.items(), key=lambda kv: -kv[1]["count"]):
                cy = y + 11
                # le jeu donne un n° de serveur, pas son nom : on l'appelle par ses persos
                who = ", ".join(e["chars"]) if e["chars"] else key.lstrip("@#")
                cv.create_text(PAD, cy, text=fit(who, f_cap_font, 200), fill=PEARL, font=f_cap, anchor="w")
                cv.create_text(W - PAD, cy, text=f"{kfmt(int(e['count']))} / {kfmt(cap)}",
                               fill=(ROSE if e["count"] >= cap * GATHER_NEAR else GOLD) if key == tr.gather_key() else MIST, font=f_num, anchor="e")
                y += 22
            y += 4

        # L'autre vue en une ligne : le dernier familier.
        if hero is not None:
            lvl, pts, need, extra = tr.view(hero)
            y = draw_side_line(y, fit(f"{L['pet_line']} · {tr.name(hero)}", f_cap_font, 210),
                               L["maxed_short"] if need is None else f"{pts} / {need}", None, DONE if need is None else AETHER)
        draw_footer(y, alert=near)

    def kfmt(n):
        return f"{n:,}".replace(",", "\u202f")

    def draw_kina(y):
        """Kinah de monstres du jour du perso en jeu, sur le plafond quotidien."""
        earned, cap = tr.kina_earned(), tr.kina_cap
        full = earned >= cap
        cv.create_line(PAD, y + 4, W - PAD, y + 4, fill=RIM)
        cy_k = y + 18
        cap_txt = f"{cap / 1e6:g}\u202fM" if cap % 100_000 == 0 else kfmt(cap)
        tipped(cv, tips, cv.create_text(PAD, cy_k, text=L["kina"], fill=PEARL, font=f_row_name, anchor="w"), L["tip_kina"])
        cv.create_text(W - PAD, cy_k, text=f"{kfmt(earned)} / {cap_txt}", fill=DONE if full else GOLD, font=f_num, anchor="e")
        draw_kina_bar(PAD, cy_k + 11, W - 2 * PAD, 4, min(earned / cap, 1.0) if cap else 0, full)
        sub = L["kina_full"] if full else L["kina_left"].format(n=kfmt(cap - earned))
        if tr.char:
            sub = f"{tr.char} · {sub}"
        cv.create_text(PAD, cy_k + 25, text=fit(sub, f_cap_font, W - 2 * PAD), fill=DONE if full else MIST, font=f_cap, anchor="w")
        return cy_k + 33

    def draw_kina_bar(x, y, w, h, p, full):
        def paint(d):
            d.rounded_rectangle((0, 0, w * SS, h * SS), radius=h * SS / 2, fill=rgba(RIM))
            if p > 0:
                d.rounded_rectangle((0, 0, max(w * p, h) * SS, h * SS), radius=h * SS / 2,
                                    fill=rgba(DONE if full else GOLD))
        img = smooth(("kbar", w, h, round(p, 3), full, RIM, GOLD, DONE), w, h, paint)
        cv.create_image(x, y, image=img, anchor="nw")

    def draw_compact_hero(hero, layout):
        """Affichages « Barre » et « Minimal » : renvoie le bas du bloc."""
        if hero is None:
            title = L["empty"] if tr.synced else L["no_data"]
            cv.create_text(PAD, PAD + 10, text=title, fill=PEARL, font=f_row_name, anchor="w")
            if layout == "bar":
                cv.create_text(PAD, PAD + 28, text=L["empty_hint"] if tr.synced else L["no_data_hint"],
                               fill=MIST, font=f_cap, anchor="w", width=W - 2 * PAD)
                return PAD + 44
            return PAD + 22
        lvl, pts, need, extra = tr.view(hero)
        p = anim["p"] if anim["mid"] == hero and anim["p"] is not None else progress(hero)
        maxed = need is None
        bottom = PAD + (60 if layout == "bar" else 22)
        if maxed:
            cv.create_rectangle(1, 1, W - 2, bottom, fill=mix(ABYSS, DONE, 0.18), outline="")
        if maxed:
            badge_txt = L["maxed_short"] + (f" +{extra}" if extra else "")
        else:
            badge_txt = f"{pts} / {need}"
        # valeur à droite (pastille verte si au max)
        rid = cv.create_text(W - PAD, PAD + 10, text=badge_txt, fill=ABYSS if maxed else AETHER, font=f_num, anchor="e")
        if maxed:
            x0, y0, x1, y1 = cv.bbox(rid)
            pill = cv.create_rectangle(x0 - 7, y0 - 2, x1 + 7, y1 + 2, fill=DONE, outline="")
            cv.tag_lower(pill, rid)
            tipped(cv, tips, [rid, pill], L["tip_maxed"], underline=False)
            right = x0 - 14
        else:
            tipped(cv, tips, rid, L["tip_points"])
            right = cv.bbox(rid)[0] - 10
        if layout == "mini":
            if not maxed:
                tipped(cv, tips, draw_pips(right - 36, PAD + 10, lvl, pts), L["tip_pips"], underline=False)
                right -= 44
            cv.create_text(PAD, PAD + 10, text=fit(tr.name(hero), f_row_name, right - PAD),
                           fill=PEARL, font=f_row_name, anchor="w")
        else:
            cv.create_text(PAD, PAD + 10, text=fit(tr.name(hero), f_hero_name, right - PAD),
                           fill=PEARL, font=f_hero_name, anchor="w")
            tipped(cv, tips, draw_bar(PAD, PAD + 26, W - 2 * PAD, 8, p, maxed), L["tip_bar"], underline=False)
            sub = (L["maxed_banner"] if maxed else L["left_to"].format(n=need - pts, to=L["to"][lvl]))
            cv.create_text(PAD, PAD + 48, text=sub, fill=DONE if maxed else MIST, font=f_cap, anchor="w")
        hits.append((0, bottom, hero))
        return bottom

    def tick_anim():
        if anim["p"] is None:
            return
        k = min((time.time() - anim["t0"]) / ANIM_SEC, 1.0)
        ease = 1 - (1 - k) ** 3
        anim["p"] = anim["from"] + (anim["to"] - anim["from"]) * ease
        if k >= 1.0:
            anim["p"] = None
        draw()
        if anim["p"] is not None:
            root.after(16, tick_anim)

    dirty = {"v": False}
    sync_state = {"v": ("idle", None)}
    update_state = {"v": None}  # version téléchargée, prête à installer
    info_state = {"v": None}    # (clé de texte, fin d'affichage) : message temporaire en bas
    if notmeter_imported:  # un message de quelques secondes : les réglages de combat viennent de NotMeter
        info_state["v"] = ("combat_imported", time.time() + 12)
        root.after(12200, lambda: draw())

    toast = {"win": None, "canvas": None, "job": None}

    def gather_toast(failed, qty, better=False, item=None):
        """Récolte en mode discret (menu Affichage) : un petit message en bas à droite de la fenêtre du jeu (de l'écran à
        défaut) : la ressource et sa quantité (ou « ratée »), puis « rang supérieur » et le compteur du jour ; il s'efface
        seul au bout de 3 s (ou au clic) et l'overlay ne bouge pas. Il s'élargit au nom de la ressource (jusqu'à 380 px)."""
        n = tr.gather_entry()["count"]
        name = None if failed or item is None else tr.item_name(item)
        if failed:
            line = L["toast_gather_fail"]
        elif name:  # « ✓ Minerai d'orichalque ×3 »
            line = L["toast_gather_item"].format(item=name, q=kfmt(qty))
        else:
            line = L["toast_gather_better" if better else "toast_gather_ok"].format(q=kfmt(qty))
        sub = ((L["toast_gather_up"] if better and name else "") +
               (L["toast_gather_count"].format(n=kfmt(n), cap=kfmt(GATHER_CAP)) if n else "")).strip(" ·")
        w, h = min(max(250, f_mid_font.measure(line) + 28), 380), 44 if sub else 30
        if f_mid_font.measure(line) + 28 > w:  # nom trop long : raccourci, la quantité reste visible
            rest = L["toast_gather_item"].format(item="", q=kfmt(qty))
            line = L["toast_gather_item"].format(item=fit(name, f_mid_font, w - 28 - f_mid_font.measure(rest)), q=kfmt(qty))
        if toast["win"] is None or not toast["win"].winfo_exists():
            win = toast["win"] = tk.Toplevel(root)
            win.withdraw()  # placé avant d'être montré (sinon il apparaît un instant en haut à gauche)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.94)
            c = toast["canvas"] = tk.Canvas(win, highlightthickness=0, bd=0)
            c.pack(fill="both", expand=True)
            c.bind("<Button-1>", lambda e: win.withdraw())
        win, c = toast["win"], toast["canvas"]
        c.delete("all")
        c.configure(width=w, height=h, bg=ABYSS)
        c.create_rectangle(0, 0, w - 1, h - 1, outline=RIM)
        c.create_rectangle(0, 0, 3, h, fill=ROSE if failed else DONE, outline="")
        c.create_text(14, 15, text=line, fill=ROSE if failed else PEARL, font=f_mid, anchor="w")
        if sub:
            c.create_text(14, 32, text=sub, fill=GOLD if n >= GATHER_NEAR * GATHER_CAP or (better and name) else MIST, font=f_cap, anchor="w")
        try:
            game = find_game_window()
        except Exception:
            game = None
        right, bottom = (game[2], game[3]) if game else (root.winfo_screenwidth(), root.winfo_screenheight() - 40)
        win.geometry(f"{w}x{h}+{right - w - 18}+{bottom - h - 18}")
        win.deiconify()
        win.lift()
        if toast["job"]:
            root.after_cancel(toast["job"])
        toast["job"] = root.after(3000, win.withdraw)

    def pump():
        changed = False
        while True:
            try:
                ev = events.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "conn":
                if online["v"] != ev[1]:
                    online["v"] = ev[1]; changed = True
                continue
            if ev[0] == "problem":
                problem["v"] = ev[1]; changed = True
                continue
            if ev[0] == "sync":
                sync_state["v"] = ev[1]; changed = True
                continue
            if ev[0] == "update":
                if ev[1][0] == "ready":
                    update_state["v"] = ev[1][1]
                else:  # message temporaire : à jour / téléchargement / échec
                    info_state["v"] = (ev[1][1], time.time() + 6)
                    root.after(6200, draw)
                changed = True
                continue
            if ev[0] == "fight":  # combat écrit pour Aion 2 Builder : un message de quelques secondes
                info_state["v"] = ("fight_saved", time.time() + 8, {"d": ev[1], "dps": f"{ev[2]:,}".replace(",", " ")})
                root.after(8200, draw)
                changed = True
                continue
            if ev[0] == "tray":  # команды из значка в трее (приходят из его потока)
                toggle_visible() if ev[1] == "toggle" else quit_()
                continue
            if (ev[0] == "gather" or (ev[0] == "gather_count" and ev[2])) and not tr.gather_toast:
                hide["last"] = time.time()  # récolte : l'overlay revient (masquage auto), sur la vue Récolte
                set_hidden(False)
            if ev[0] == "gain":
                mid = ev[1]
                hide["last"] = time.time()  # butin de familier : l'overlay revient (masquage auto)
                set_hidden(False)
                start = anim["p"] if anim["mid"] == mid and anim["p"] is not None else progress(mid)
                tr.on_event(ev)
                anim.update(mid=mid, p=start, to=progress(mid), t0=time.time())
                anim["from"] = start
                root.after(16, tick_anim)
            else:
                tr.on_event(ev)
            if ev[0] == "gather" and tr.gather_toast:  # mode discret : le message en bas à droite, compteur à jour
                gather_toast(ev[3], ev[2], len(ev) > 4 and ev[4], ev[1])
            if ev[0] in ("gather", "gather_count", "kina", "char"):  # récoltes et Kinah du jour : tâches d'Aion 2 Todo
                syncer.kick_daily()
            if ev[0] == "kina" and not tr.show_kina:  # (le perso, lui, peut changer le compteur de récolte affiché)
                dirty["v"] = True
                continue
            if ev[0] in ("state", "gain", "crystal"):
                syncer.kick()
            changed = True; dirty["v"] = True
        if changed:
            draw()
        root.after(100, pump)

    def autosave():
        if dirty["v"]:
            dirty["v"] = False
            tr.save()
        if LIVE.history_version != hist_saved["v"]:  # un combat fini s'est ajouté à l'historique
            hist_saved["v"] = LIVE.history_version
            with LIVE.lock:
                kept = list(LIVE.history)
            try:
                save_history(kept)
            except Exception as e:
                log("historique :", repr(e))
        if visible["v"]:
            root.lift()
            root.attributes("-topmost", True)
            if tips.win is not None:  # une infobulle ouverte reste au-dessus de l'overlay
                tips.win.lift()
        root.after(3000, autosave)

    # перетаскивание за любое место
    drag = {}

    def press(e):
        drag["x"], drag["y"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()
        drag["moved"] = False

    def move(e):
        drag["moved"] = True
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    def release(e):
        if drag.get("moved"):
            tr.pos = [root.winfo_x(), root.winfo_y()]
            dirty["v"] = True
        else:
            tags = {t for i in cv.find_overlapping(e.x - 8, e.y - 8, e.x + 8, e.y + 8) for t in cv.gettags(i)}
            if "link" in tags and problem["v"] and problem["v"][2]:
                webbrowser.open(problem["v"][2])
            elif "menu" in tags:
                main_menu(e)
            elif "hist" in tags:  # ≡ historique, en tête de la vue Combat
                open_history()
            elif "update" in tags:
                apply_update()
            elif tags & {"tab_pets", "tab_gather", "tab_combat"}:  # onglet : vue verrouillée
                tr.view_mode = next(t[4:] for t in ("tab_pets", "tab_gather", "tab_combat") if t in tags)
                dirty["v"] = True
                draw()
            elif "tab_auto" in tags and tr.view_mode != "auto":  # FIXÉE : retour en auto
                tr.view_mode = "auto"
                dirty["v"] = True
                draw()
            else:  # une ligne du tableau de combat (au point près : les lignes se touchent) : le détail du joueur
                at = {t for i in cv.find_overlapping(e.x, e.y, e.x, e.y) for t in cv.gettags(i)}
                row = next((t[5:] for t in at if t.startswith("prow_")), None)
                if row:
                    open_player("me" if row == "me" else int(row))

    cv.tag_bind("update", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("update", "<Leave>", lambda e: cv.config(cursor=""))
    cv.tag_bind("tab", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("tab", "<Leave>", lambda e: cv.config(cursor=""))
    cv.tag_bind("tipq", "<Enter>", lambda e: cv.config(cursor="question_arrow"))  # survol : une explication
    cv.tag_bind("tipq", "<Leave>", lambda e: cv.config(cursor=""))
    cv.tag_bind("hist", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("hist", "<Leave>", lambda e: cv.config(cursor=""))
    cv.tag_bind("prow", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("prow", "<Leave>", lambda e: cv.config(cursor=""))
    cv.tag_bind("link", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("link", "<Leave>", lambda e: cv.config(cursor=""))
    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", release)

    def quit_():
        RECORDER.stop()
        if visible["v"]:
            tr.pos = [root.winfo_x(), root.winfo_y()]
        tr.save()
        if tray["icon"]:
            tray["icon"].stop()
        root.destroy()

    visible = {"v": True}
    tray = {"icon": None}
    opacity = {"v": 0.94}
    hide = {"hidden": False, "last": time.time()}

    def set_hidden(hidden):
        if hide["hidden"] != hidden:
            hide["hidden"] = hidden
            # Presque invisible plutôt qu'invisible : la fenêtre reçoit encore le survol qui la rappelle.
            root.attributes("-alpha", HIDDEN_ALPHA if hidden else opacity["v"])

    def hide_tick():
        """Masquage auto : sans butin de familier depuis tr.hide_after secondes (souris hors de l'overlay, pas de menu
        ouvert), l'overlay se cache ; le survol ou le prochain butin le rappelle. Un problème affiché (Npcap, lecture)
        le garde visible : sinon on le croirait en marche."""
        now = time.time()
        try:
            px, py = root.winfo_pointerxy()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            over = x <= px < x + root.winfo_width() and y <= py < y + root.winfo_height()
            if over or root.grab_current() is not None or not tr.auto_hide or problem["v"] or LIVE.showing():
                hide["last"] = now
                set_hidden(False)
            elif now - hide["last"] >= tr.hide_after:
                set_hidden(True)
        except tk.TclError:
            pass
        root.after(250, hide_tick)

    def toggle_visible():
        if visible["v"]:
            tr.pos = [root.winfo_x(), root.winfo_y()]
            root.withdraw()
        else:
            root.deiconify()
            root.geometry(f"+{tr.pos[0]}+{tr.pos[1]}" if tr.pos else "")
            root.attributes("-topmost", True)
            hide["last"] = time.time()  # réaffiché : il reste visible le temps du délai
            set_hidden(False)
        visible["v"] = not visible["v"]
        if tray["icon"]:
            tray["icon"].update_menu()

    def start_tray():
        """Значок в трее: окно без рамки не видно ни в панели задач, ни в Alt+Tab."""
        try:
            import pystray
            from PIL import Image
            img = Image.open(os.path.join(RES_DIR, "assets", "icon.ico"))
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: L["hide_overlay"] if visible["v"] else L["show_overlay"],
                                 lambda: events.put(("tray", "toggle")), default=True),
                pystray.MenuItem(lambda item: L["quit"], lambda: events.put(("tray", "quit"))))
            tray["icon"] = pystray.Icon("AION2-pet-meter", img, APP_NAME, menu)
            tray["icon"].run_detached()
        except Exception as e:
            log("tray:", repr(e))

    def menu_at(e, items):
        """Menu contextuel : (libellé, action), (libellé, [sous-menu]) à toute profondeur, None = séparateur."""
        def build(parent, entries):
            m = tk.Menu(parent, tearoff=0, bg=ABYSS, fg=PEARL, activebackground=RIM, activeforeground=PEARL, bd=0, font=(CAP, 9))
            for it in entries:
                if it is None:
                    m.add_separator()
                elif isinstance(it[1], list):
                    m.add_cascade(label=it[0], menu=build(m, it[1]))
                else:
                    m.add_command(label=it[0], command=it[1])
            return m
        build(root, items).tk_popup(e.x_root, e.y_root)
        hide["last"] = time.time()  # menu fermé : pas de masquage immédiat si la souris est restée sur le menu

    def main_menu(e):
        def clear():
            tr.recent.clear(); dirty["v"] = True; draw()
        def set_opacity(a):
            opacity["v"] = a
            if not hide["hidden"]:
                root.attributes("-alpha", a)
        alpha = [(f"{int(a * 100)}%", lambda a=a: set_opacity(a)) for a in (1.0, 0.94, 0.8, 0.6)]
        # Regroupé (7 entrées au lieu de 20) : l'affichage, le combat, le Kinah, la langue, Aion 2 Todo, les outils,
        # puis mise à jour et sortie. Pendant un enregistrement, ses repères restent en tête, à portée de clic.
        display = [*layout_items(), None, (L["theme"], theme_items()), (L["opacity"], alpha), (L["hide_menu"], hide_items())]
        combat = [*combat_items(), None, (L["hist_menu"], open_history)]
        lang = [(L["menu_lang_ui"], lang_items()), (L["menu_lang_game"], game_lang_items())]
        todo = [(L["sync_key"], ask_key), (L["sync_now"], lambda: syncer.kick(now=True)),
                (L["open_site"], lambda: webbrowser.open(SYNC_URL.rsplit("/api/", 1)[0]))]
        tools = [*rec_items(), (L["fights_menu"], fight_items()), None, (L["clear"], clear)]
        update = ((L["update_menu"].format(v=update_state["v"]), apply_update) if update_state["v"]
                  else (L["check_update"], lambda: updater.check_now()))
        menu_at(e, [*(rec_items() + [None] if RECORDER.active else []),
                    (L["layout"], display), (L["combat_menu"], combat), (L["kina_menu"], kina_items()),
                    (L["menu_lang"], lang), (L["menu_todo"], todo), (L["menu_tools"], tools), None,
                    update, *([(L["to_tray"], toggle_visible)] if tray["icon"] else []), (L["quit"], quit_)])

    def rec_items():
        if RECORDER.active:
            return [(L["rec_mark"].format(n=RECORDER.marks + 1), rec_mark), (L["rec_stop"], rec_stop)]
        return [(L["rec_start"], rec_start)] + ([(L["rec_show"], rec_show)] if RECORDER.path else [])

    def fight_items():
        def toggle():
            FIGHTS.enabled = tr.record_fights = not tr.record_fights
            dirty["v"] = True
            draw()

        def open_dir():
            import subprocess
            os.makedirs(FIGHTS_DIR, exist_ok=True)
            subprocess.Popen(f'explorer "{FIGHTS_DIR}"')
        def toggle_all():
            FIGHTS.all_kinds = tr.record_all_fights = not tr.record_all_fights
            dirty["v"] = True
            draw()
        return [(("✓ " if tr.record_fights else "") + L["fights_on"], toggle),
                (("✓ " if tr.record_all_fights else "") + L["fights_all"], toggle_all), (L["fights_open"], open_dir)]

    def rec_start():
        RECORDER.start()
        draw()
        root.after(1000, rec_tick)

    def rec_tick():
        """Compte à rebours en bas de l'overlay ; l'enregistrement s'arrête seul au bout de 5 min."""
        if not RECORDER.active:
            return
        if RECORDER.remaining() == 0:
            return rec_stop()
        draw()
        root.after(1000, rec_tick)

    def rec_mark():
        RECORDER.mark()
        draw()

    def rec_stop():
        RECORDER.stop()
        info_state["v"] = ("rec_saved", time.time() + 6)
        root.after(6200, draw)
        draw()

    def rec_show():
        import subprocess
        subprocess.Popen(f'explorer /select,"{RECORDER.path}"')

    def combat_items():
        """Réglages du tableau de combat, comme dans NotMeter : colonnes, formats, couleurs, lignes, taille, temps gardé
        après le combat, et la reprise des réglages de NotMeter."""
        cfg = tr.combat

        def flip(key):
            def do():
                cfg[key] = not cfg[key]; dirty["v"] = True; draw()
            return do

        def put(key, value):
            def do():
                cfg[key] = value
                LIVE.keep = cfg["keep"]
                dirty["v"] = True; draw()
            return do

        def from_notmeter():
            found = notmeter_combat_settings()
            if found:
                cfg.update(found)
                LIVE.keep = cfg["keep"]
            info_state["v"] = ("combat_imported" if found else "combat_no_notmeter", time.time() + 8)
            root.after(8200, draw)
            dirty["v"] = True; draw()

        mark = lambda on: "✓ " if on else "    "
        return [(mark(cfg["damage"]) + L["c_damage"], flip("damage")), (mark(cfg["dps"]) + L["c_dps"], flip("dps")),
                (mark(cfg["share"]) + L["c_share"], flip("share")),
                (mark(cfg["share_basis"] == "group") + L["c_basis_group"], put("share_basis", "group")),
                (mark(cfg["share_basis"] == "boss") + L["c_basis_boss"], put("share_basis", "boss")), None,
                (mark(cfg["dps_full"]) + L["c_dps_full"], flip("dps_full")), (mark(cfg["damage_short"]) + L["c_damage_short"], flip("damage_short")),
                (mark(cfg["class_colors"]) + L["c_class_colors"], flip("class_colors")),
                (mark(cfg["power"]) + L["c_power"], flip("power")), None,
                *[(mark(cfg["rows"] == n) + L["c_rows"].format(n=n), put("rows", n)) for n in (5, 7, 10)], None,
                *[(mark(cfg["width"] == n) + L["c_width"].format(n=n), put("width", n)) for n in (300, 330, 400)],
                *[(mark(cfg["row_h"] == n) + L["c_row_h"].format(n=n), put("row_h", n)) for n in (20, 22, 26)], None,
                *[(mark(cfg["keep"] == n) + (L["c_keep"].format(n=n) if n else L["c_keep_next"]), put("keep", n)) for n in (10, 30, 60, 0)],
                None, (L["c_import"], from_notmeter)]

    def kina_items():
        def toggle():
            tr.show_kina = not tr.show_kina; tr.kina_show_chosen = True; dirty["v"] = True; draw()

        def cap(n):
            tr.kina_cap = n; dirty["v"] = True; draw()

        def reset():
            tr.kina_today()[tr.char or "?"] = 0; dirty["v"] = True; draw()
        caps = [(("✓ " if tr.kina_cap == n else "    ") + L["kina_cap"].format(n=kfmt(n)), lambda n=n: cap(n))
                for n in (1_000_000, 2_000_000, 3_000_000, 5_000_000)]
        return [(("✓ " if tr.show_kina else "    ") + L["kina_show"], toggle), None, *caps, None, (L["kina_reset"], reset)]

    def hide_items():
        def toggle():
            tr.auto_hide = not tr.auto_hide; dirty["v"] = True

        def delay(n):
            tr.hide_after = n; tr.auto_hide = True; dirty["v"] = True
        delays = [(("✓ " if tr.auto_hide and tr.hide_after == n else "    ") + L["hide_after"].format(n=n), lambda n=n: delay(n)) for n in HIDE_DELAYS]
        return [(("✓ " if tr.auto_hide else "    ") + L["hide_on"], toggle), None, *delays]

    def layout_items():
        """Le dernier monstre (cercle, barre ou une ligne), la liste des précédents, la largeur des vues Familiers et Récolte."""
        cur = tr.layout or "rings"
        mark = lambda on: "✓ " if on else "    "

        def flip_list():
            tr.show_list = not tr.show_list; dirty["v"] = True; draw()

        def width(n):
            tr.width = n; dirty["v"] = True; draw()

        def flip_toast():
            tr.gather_toast = not tr.gather_toast; dirty["v"] = True; draw()
        return [*[(mark(cur == k) + L["layout_" + k], lambda k=k: set_layout(k)) for k in ("rings", "bar", "mini")], None,
                (mark(tr.show_list) + L["layout_list"], flip_list), None,
                *[(mark(tr.width == n) + L["layout_width"].format(n=n), lambda n=n: width(n)) for n in (270, 300, 330)], None,
                (mark(tr.gather_toast) + L["layout_gather_toast"], flip_toast)]

    def set_layout(name):
        tr.layout = name
        dirty["v"] = True
        draw()

    def theme_items():
        cur = tr.theme or "abysse"
        return [(("✓ " if cur == k else "    ") + t["label"], lambda k=k: set_theme(k)) for k, t in THEMES.items()]

    def set_theme(name):
        nonlocal ABYSS, RIM, AETHER, GOLD, ROSE, PEARL, MIST, DONE
        tr.theme = name
        ABYSS, RIM, AETHER, GOLD, ROSE, PEARL, MIST, DONE = palette(name)
        root.configure(bg=ABYSS)
        cv.configure(bg=ABYSS)
        dirty["v"] = True
        draw()

    def apply_update():
        if updater.apply():
            quit_()

    def game_lang_items():
        # от языка клиента зависят имена питомцев; обычно он определяется по чату сам
        mark = lambda code: ("✓ " if tr.game_lang == code else "    ")
        detected = tr.ocr_lang.split("-")[0] if tr.ocr_lang else None
        auto = L["detect"] + (f" ({GAME_LANGS.get(detected, detected)})" if detected else "")
        items = [(mark(None) + auto, lambda: set_game_lang(None)), None]
        return items + [(mark(c) + n, lambda c=c: set_game_lang(c)) for c, n in GAME_LANGS.items()]

    def set_game_lang(code):
        tr.game_lang = code
        dirty["v"] = True
        draw()

    def lang_items():
        mark = lambda code: ("✓ " if tr.ui_lang == code else "    ")
        return [(mark(None) + L["auto"], lambda: set_lang(None)), None,
                (mark("fr") + "Français", lambda: set_lang("fr")),
                (mark("en") + "English", lambda: set_lang("en")),
                (mark("ru") + "Русский", lambda: set_lang("ru"))]

    def set_lang(code):
        nonlocal L
        tr.ui_lang = code
        L = lang_table()
        dirty["v"] = True
        draw()

    def context(e):
        mid = next((m for y0, y1, m in hits if y0 <= e.y < y1), None)
        if mid is None:
            return main_menu(e)

        def rename():
            root.attributes("-topmost", False)
            s = simpledialog.askstring(L["rename_title"], f"ID {mid}:", initialvalue=tr.names.get(mid, ""), parent=root)
            root.attributes("-topmost", True)
            if s is not None:
                tr.rename(mid, s.strip()); dirty["v"] = True; draw()

        def hide():
            tr.hidden.add(mid); dirty["v"] = True; draw()

        menu_at(e, [(L["rename"], rename), (L["hide"], hide), None,
                    (L["clear"], lambda: (tr.recent.clear(), draw())), (L["quit"], quit_)])

    cv.bind("<Button-3>", context)

    learner = None
    try:
        learner = NameLearner(events.put, tr)
    except Exception as e:
        log("name learner:", repr(e))

    def emit(ev):
        events.put(ev)
        if ev[0] == "gain" and learner:
            learner.on_gain(ev[1], ev[2])

    syncer = Syncer(tr, lambda st: events.put(("sync", st)))

    def ask_key():
        root.attributes("-topmost", False)
        s = simpledialog.askstring(L["sync_key_title"], L["sync_key_prompt"], initialvalue=tr.sync_key or "", parent=root)
        root.attributes("-topmost", True)
        if s is not None:
            tr.sync_key = s.strip() or None
            tr.save()
            if tr.synced and not tr.synced.startswith("site"):
                syncer.kick(now=True)
            else:
                syncer.pull(lambda lv, pt: events.put(("site_state", lv, pt)))

    updater = Updater(lambda st: events.put(("update", st)))

    if not tr.sync_key:
        events.put(("sync", ("nokey", None)))
    elif tr.synced and not tr.synced.startswith("site"):
        syncer.kick(now=True)  # renvoie la dernière collection connue dès le lancement
    else:
        syncer.pull(lambda lv, pt: events.put(("site_state", lv, pt)))

    def sniffer():
        if not npcap_installed():
            log("npcap не найден")
            events.put(("problem", ("npcap_title", "npcap_hint", NPCAP_URL)))
            return
        try:
            run_sniffer(emit)
        except Exception as e:
            log("sniffer:", repr(e))
            events.put(("conn", False))
            events.put(("problem", ("sniff_title", "sniff_hint", None)))

    threading.Thread(target=sniffer, daemon=True).start()
    start_tray()
    draw()
    root.after(100, pump)
    root.after(250, hide_tick)
    root.after(500, live_tick)
    root.after(3000, autosave)
    root.protocol("WM_DELETE_WINDOW", quit_)
    root.mainloop()


def selftest():
    """Проверка окружения; результат - в aion2_pet_tracker.log рядом с программой."""
    log("selftest: npcap", npcap_installed(), "| data", DATA_DIR, "| names", {k: len(v) for k, v in load_shared_names().items()})
    try:
        from scapy.all import conf
        log("selftest: interfaces", [i.name for i in conf.ifaces.values() if i.ip])
    except Exception as e:
        log("selftest: scapy FAIL", repr(e))
    try:
        from PIL import Image, ImageDraw, ImageFont
        import winocr
        img = Image.new("RGB", (900, 80), "black")
        font = ImageFont.truetype("segoeui.ttf", 36)
        ImageDraw.Draw(img).text((10, 15), "Спарки: получены очки духа x1", fill="white", font=font)
        for lang in ocr_languages():
            text = " | ".join(l["text"] for l in winocr.recognize_pil_sync(img, lang).get("lines", []))
            log(f"selftest: ocr {lang}: {text}")
    except Exception as e:
        log("selftest: ocr FAIL", repr(e))
    log("selftest: game window", find_game_window())


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--replay":
        replay(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--fights":
        FIGHTS.all_kinds = "--all" in sys.argv  # diagnostic : écrire aussi les combats ordinaires, pour voir leur type
        replay_fights(sys.argv[2], sys.argv[sys.argv.index("--me") + 1] if "--me" in sys.argv else None)
    elif "--selftest" in sys.argv:
        selftest()
    else:
        run_overlay()
