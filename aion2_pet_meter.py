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
    """(objet, quantité, ratée) d'une de tes récoltes, ou None."""
    if len(p) < 12:
        return None
    _, i = read_varint(p, 2)
    if i + 8 > len(p):
        return None
    qty = int.from_bytes(p[i + 4:i + 8], "little")
    return (p[i:i + 4].hex(), qty, p[0] != 0) if qty < 10_000 else None


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
FIGHT_GAP = 15.0        # secondes sans coup donné : le combat est fini
FIGHT_MIN_HITS = 10     # en dessous : pas un combat (un monstre achevé en passant)
FIGHT_MIN_SECONDS = 10
FIGHTS_KEEP = 30        # combats gardés dans le dossier (Aion 2 Builder en garde 20 par perso)
TRAINING_DUMMIES = {2400032}  # mannequins d'entraînement (code de créature)
BOSS_MIN_SPAN = 25.0    # boss : la cible principale encaisse tes coups pendant au moins 25 s…
BOSS_MIN_SHARE = 0.5    # … et prend au moins la moitié de tes dégâts (un pack de monstres meurt plus vite et se partage)
FIGHTS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "AION2-pet-meter", "fights")


def parse_hit(p):
    """Un coup : (cible, attaquant, sort, critique, angle, échelle, dégâts) ; angle 1 = de dos, 2 = de face, None = inconnu.
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
        if low & 2:  # drapeaux, PV rendus (varint), angle : la longueur du varint décale la suite
            j = i + 1
            _, j = read_varint(p, j)
            angle = p[j]
            skip += j - i - 2
        i += skip
        scale, i = read_varint(p, i)
        damage, i = read_varint(p, i)
        if scale == 0:  # bourrage : l'échelle et les dégâts viennent après
            scale, damage = damage, read_varint(p, i)[0]
        return target, actor, skill, kind == 3, angle, scale, damage
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


def skill_base(code):
    """Sort sans sa variante : les 4 derniers chiffres (spécialisations) ne changent pas le sort."""
    return code // 10000 * 10000 if code >= 10_000_000 else code


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

    def mine(self, actor, skill, scale):
        """Coup à toi : le tien, ou celui d'une invocation (même échelle de puissance, sort de ta classe)."""
        if actor == self.me:
            return True
        return (self.scale is not None and scale == self.scale and self.prefix is not None
                and skill >= 10_000_000 and skill // 1_000_000 == self.prefix)

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
            LIVE.cast(r[0], r[1])
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
        target, actor, skill, crit, angle, scale, damage = h
        now = time.time() if now is None else now
        self.seen(actor, skill)
        # Compteur de dégâts en direct : tous les joueurs, même si l'écriture des combats est coupée.
        LIVE.hit(target, actor, skill, crit, scale, damage, now, actor != target and self.mine(actor, skill, scale), mobs)
        if not self.enabled:
            return
        with self.lock:
            if actor == self.me and 1_000 <= scale <= 200_000:
                self.scale = scale
            if actor == self.me and skill >= 10_000_000:
                self.prefix = skill // 1_000_000
            f = self.fight
            if actor != target and self.mine(actor, skill, scale):
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
        main = max(f["targets"].items(), key=lambda kv: kv[1]) if f["targets"] else (0, 0)
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
SKILLS_FILE = os.path.join(RES_DIR, "data", "skills.json")  # noms des sorts et classe par préfixe (données d'Aion 2 Builder)
# Réglages du tableau de combat (menu ⋯ → Combat), calqués sur ceux de NotMeter : colonnes affichées, formats, couleurs
# des barres par classe, lignes, largeur et hauteur, temps gardé après le combat (0 = jusqu'au prochain combat).
COMBAT_DEFAULTS = {"damage": False, "dps": True, "share": True, "dps_full": True, "damage_short": True, "class_colors": True,
                   "power": True, "rows": 7, "width": 330, "row_h": 22, "keep": 30,
                   "colors": {"damage": "#FFE566", "dps": "#FFFFFF", "share": "#FFE566", "time": "#AAFAFF", "power": "#66E9FF"}}
COMBAT_LAYOUT = 2  # version de la taille du tableau : la 2 (v0.8.3) est plus compacte, les tailles reprises de NotMeter avant sont remplacées
# Couleur des barres par classe (2 premiers chiffres des sorts).
CLASS_COLORS = {11: "#D9A066", 12: "#7FB2E5", 13: "#B48CE0", 14: "#7CC48A", 15: "#E07A7A", 16: "#5FC9C2", 17: "#E6D27A", 18: "#E596C8"}
NOTMETER_SETTINGS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Not4You-Meter", "settings.json")


def notmeter_combat_settings():
    """Réglages du tableau de NotMeter (son settings.json, en clair, sur ce PC) traduits pour la vue Combat ; None s'il
    n'est pas installé. Seul ce qui existe ici est repris : colonnes, formats, couleurs, puissance de combat, lignes
    visibles, remise à zéro après le combat. Pas la largeur ni la hauteur des lignes : le tableau du Pet Meter est plus
    compact (police plus petite), les mêmes nombres le rendaient trop grand. La contribution « % des PV du boss » n'est
    pas reprise : les PV du boss ne sont pas lus, la contribution reste la part des dégâts du groupe."""
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
        "keep": clamp(m.get("CombatEndAutoResetSeconds"), 5, 600, 30) if m.get("CombatEndAutoResetEnabled", True) else 0,
        "colors": {"damage": hex_color(m.get("TotalDamageColor"), colors["damage"]), "dps": hex_color(m.get("DpsColor"), colors["dps"]),
                   "share": hex_color(m.get("ContributionColor"), colors["share"]), "time": hex_color(m.get("CombatTimeColor"), colors["time"]),
                   "power": hex_color(m.get("CombatPowerColor"), colors["power"])},
    }


def load_skills():
    """(classe par préfixe de sort, nom de sort par code) ; vides si le fichier manque."""
    try:
        d = json.load(open(SKILLS_FILE, encoding="utf-8"))
        return {int(k): v for k, v in d["classes"].items()}, {int(k): v for k, v in d["skills"].items()}
    except Exception as e:
        log("skills.json:", e)
        return {}, {}


class Live:
    """Compteur de dégâts en direct, comme un DPS meter : dès que tu frappes, les dégâts de chaque joueur sur la cible que
    tu frappes le plus (le boss), avec le détail de leurs sorts. Les invocations comptent pour leur maître (donné par leur
    apparition, sinon même classe et même échelle de puissance). Noms et puissance de combat : apparitions des joueurs.
    En mémoire seulement : rien n'est écrit ni envoyé."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0      # change à chaque coup compté : l'overlay ne redessine que s'il a bougé
        self.casters = {}     # joueur qui lance des sorts -> (classe, échelle) : sert à rattacher les invocations
        self.numbers = {}     # joueur -> numéro dans sa classe (« Templier 2 »), quand son nom n'est pas connu
        self.owners = {}      # invocation -> son maître (lu à son apparition)
        self.names = {}       # joueur -> (nom, puissance de combat), lus à son apparition
        self.my_power = None  # ta puissance de combat
        self.keep = 30.0      # secondes où le tableau reste affiché après le combat (0 : jusqu'au prochain) ; réglage Combat
        self.reset()

    def reset(self):
        self.start = self.last = 0.0
        self.ended = None     # fin du combat (ton dernier coup), None pendant le combat
        self.targets = {}     # cible -> {joueur: stats}
        self.mine = {}        # cible -> tes dégâts : la cible principale est celle que tu frappes le plus
        self.spans = {}       # cible -> [ton premier, ton dernier coup]
        self.codes = {}       # cible -> code de créature

    def summon(self, entity, owner):
        with self.lock:
            if len(self.owners) > 5000:
                self.owners.clear()
            self.owners[entity] = owner

    def player(self, entity, name, power):
        with self.lock:
            if len(self.names) > 5000:
                self.names.clear()
            self.names[entity] = (name, power)

    def cast(self, actor, skill):
        if skill >= 10_000_000:
            with self.lock:
                if actor in self.owners:  # invocation : ses sorts ne font pas d'elle un joueur, son maître compte
                    return
                if actor not in self.casters:
                    self.casters[actor] = (skill // 1_000_000, None)
                    if len(self.casters) > 2000:
                        self.casters.clear()

    def hit(self, target, actor, skill, crit, scale, damage, now, own, mobs):
        """Un coup lu dans le trafic ; own : coup à toi (toi ou ton invocation)."""
        if skill < 10_000_000 or actor == target or damage <= 0:  # pas un coup de joueur
            return
        cls = skill // 1_000_000
        with self.lock:
            owner = self.owners.get(actor)
            if owner is not None:  # invocation : ses coups vont à son maître, avec la classe du maître
                actor = owner
                cls = self.casters.get(owner, (cls,))[0]
            elif actor in self.casters:
                self.casters[actor] = (cls, scale)
            if own:
                who = "me"
                if not self.start or self.ended is not None or now - self.last > FIGHT_GAP:
                    self.reset()
                    self.start = now
                self.last = now
                self.mine[target] = self.mine.get(target, 0) + damage
                span = self.spans.setdefault(target, [now, now])
                span[1] = now
                if target not in self.codes and target in mobs:
                    self.codes[target] = mobs[target]
            elif not self.start or self.ended is not None:
                return  # pas de combat à toi en cours : les coups des autres seuls n'ouvrent rien
            else:
                who = actor if actor in self.casters else next(
                    (c for c, cs in self.casters.items() if cs == (cls, scale)), actor)
            rows = self.targets.setdefault(target, {})
            s = rows.get(who)
            if s is None:
                s = rows[who] = {"damage": 0, "hits": 0, "crits": 0, "cls": cls, "first": now, "last": now, "skills": {}}
                if who != "me" and who not in self.numbers:  # (classe, numéro dans la classe)
                    self.numbers[who] = (cls, 1 + sum(1 for c, _ in self.numbers.values() if c == cls))
            s["damage"] += damage
            s["hits"] += 1
            s["crits"] += crit
            s["last"] = now
            k = s["skills"].setdefault(skill_base(skill), {"hits": 0, "damage": 0, "crits": 0, "max": 0})
            k["hits"] += 1
            k["damage"] += damage
            k["crits"] += crit
            k["max"] = max(k["max"], damage)
            self.version += 1

    def tick(self, now):
        with self.lock:
            if self.start and self.ended is None and now - self.last > FIGHT_GAP:
                self.ended = self.last
                self.version += 1

    def main(self):
        return max(self.mine, key=self.mine.get) if self.mine else None

    def showing(self, now=None):
        """Le tableau mérite l'overlay : un boss ou le mannequin (cible frappée 8 s, ou à plusieurs), pendant le combat et
        le temps réglé après (menu Combat)."""
        now = time.time() if now is None else now
        with self.lock:
            main = self.main()
            if main is None or (self.ended is not None and self.keep and now - self.ended > self.keep):
                return False
            first, last = self.spans.get(main, (0, 0))
            return (self.codes.get(main) in TRAINING_DUMMIES or last - first >= LIVE_BOSS_SPAN
                    or len(self.targets.get(main, {})) >= 2)

    def board(self):
        """Instantané pour l'affichage : cible, durée, total et joueurs triés (copies, hors verrou)."""
        with self.lock:
            main = self.main()
            if main is None:
                return None
            rows = self.targets.get(main, {})
            first = min((s["first"] for s in rows.values()), default=self.start)
            last = max((s["last"] for s in rows.values()), default=self.last)
            # (joueur, stats, (classe, numéro), nom ou None, puissance de combat ou None)
            players = [(who, {**s, "skills": {c: dict(v) for c, v in s["skills"].items()}}, self.numbers.get(who),
                        *((None, self.my_power) if who == "me" else self.names.get(who, (None, None))))
                       for who, s in sorted(rows.items(), key=lambda kv: -kv[1]["damage"])]
            return {"code": self.codes.get(main, 0), "duration": max(1.0, last - first), "ended": self.ended is not None,
                    "total": sum(s["damage"] for s in rows.values()), "players": players}


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
        elif op == OP_PLAYER:
            r = parse_player(p)
            if r:
                LIVE.player(*r)
        elif op == OP_POWER and len(p) >= 8:
            power = int.from_bytes(p[:8], "little")
            if 1_000 <= power < 10_000_000:
                LIVE.my_power = power
        elif op == OP_DEATH:
            code = self.mobs.pop(parse_death(p), None)
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
        self.view_mode = d.get("view", "auto")
        saved = d.get("combat") or {}
        self.combat = {**self.combat, **saved, "colors": {**self.combat["colors"], **saved.get("colors", {})}}
        self.combat_imported = d.get("combat_imported", False)
        if d.get("combat_layout", 1) < COMBAT_LAYOUT:  # tableau plus compact depuis la v0.8.3 : tailles par défaut
            self.combat.update(width=COMBAT_DEFAULTS["width"], row_h=COMBAT_DEFAULTS["row_h"])
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
            # ("gather", objet, quantité, ratée) : le résultat d'une récolte (le compteur, lui, vient du serveur)
            _, item, qty, failed = ev
            entry = self.gather_slot()
            if failed:
                entry["failed"] += 1
            else:
                entry["items"][item] = entry["items"].get(item, 0) + qty
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

    def __init__(self, tracker, on_status):
        self.tr, self.on_status = tracker, on_status
        self.due = None
        self.lock = threading.Lock()
        self.status = ("idle", None)  # ("ok", heure) / ("error", message) / ("nokey", None)
        threading.Thread(target=self._loop, daemon=True).start()

    def kick(self, now=False):
        with self.lock:
            self.due = time.time() + (0 if now else self.DELAY)

    def _loop(self):
        while True:
            time.sleep(0.5)
            with self.lock:
                if self.due is None or time.time() < self.due:
                    continue
                self.due = None
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
        "check_update": "Vérifier les mises à jour",
        "up_to_date": "à jour ({v})", "update_dl": "téléchargement de la mise à jour…", "update_fail": "mise à jour impossible",
        "lvl_short": "niv. {n}",
        "maxed_banner": "AU MAX — inutile de le farmer",
        "maxed_short": "AU MAX",
        "skip_hint": "inutile de le farmer",
        "over_max_short": "+{n} âme(s) perdue(s) au-delà du max",
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
        "gather_failed": " · dont {n} ratée(s)",
        "gather_servers": "PAR SERVEUR (LIMITE COMMUNE AUX PERSOS)", "gather_chars": "PAR PERSO", "pet_line": "Familier",
        "tab_combat": "COMBAT", "combat_boss": "Boss", "combat_dummy": "Mannequin", "combat_done": "Combat terminé",
        "combat_none": "Pas de combat en cours", "combat_none_hint": "Le tableau s'ouvre dès que tu frappes un boss ou le mannequin.",
        "combat_sub": "{total} · {dps}/s", "c_power": "Puissance de combat (après le nom)",
        "combat_gone": "Plus de combat en cours pour ce joueur.", "you": "toi", "player": "Joueur",
        "p_damage": "Dégâts", "p_dps": "DPS", "p_share": "Part", "p_hits": "Coups", "p_crit": "Critique", "p_time": "Durée", "p_power": "puissance",
        "p_skill": "Sort", "p_max": "Max",
        "combat_menu": "Combat (tableau)", "c_damage": "Dégâts totaux", "c_dps": "DPS", "c_share": "Contribution (part des dégâts)",
        "c_dps_full": "DPS en entier (27 272)", "c_damage_short": "Dégâts abrégés (1,92 M)", "c_class_colors": "Barres aux couleurs des classes",
        "c_rows": "{n} lignes", "c_width": "Largeur {n} px", "c_row_h": "Lignes de {n} px", "c_keep": "Garder le tableau {n} s après le combat",
        "c_keep_next": "Garder le tableau jusqu'au prochain combat", "c_import": "Reprendre les réglages de NotMeter",
        "combat_imported": "réglages de combat repris de NotMeter", "combat_no_notmeter": "NotMeter introuvable sur ce PC",
        "rec_start": "Enregistrer pour la perception d'espèce (5 min)", "rec_mark": "Repère n° {n}",
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
        "gather_blocked": "Сбор закрыт до {t} (через {d}) : время для питомцев",
        "gather_failed": " · из них неудачных: {n}",
        "gather_servers": "ПО СЕРВЕРАМ (ОБЩИЙ ЛИМИТ)", "gather_chars": "ПО ПЕРСОНАЖАМ", "pet_line": "Питомец",
        "tab_combat": "БОЙ", "combat_boss": "Босс", "combat_dummy": "Манекен", "combat_done": "Бой окончен",
        "combat_none": "Боя нет", "combat_none_hint": "Таблица откроется, как только ты ударишь босса или манекен.",
        "combat_sub": "{total} · {dps}/с", "c_power": "Боевая мощь (после имени)",
        "combat_gone": "У этого игрока больше нет боя.", "you": "ты", "player": "Игрок",
        "p_damage": "Урон", "p_dps": "DPS", "p_share": "Доля", "p_hits": "Удары", "p_crit": "Крит", "p_time": "Время", "p_power": "мощь",
        "p_skill": "Умение", "p_max": "Макс.",
        "combat_menu": "Бой (таблица)", "c_damage": "Общий урон", "c_dps": "DPS", "c_share": "Вклад (доля урона)",
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
        "gather_blocked": "Gathering blocked until {t} (in {d}): time for pets",
        "gather_failed": " · {n} of them failed",
        "gather_servers": "PER SERVER (SHARED BY CHARACTERS)", "gather_chars": "PER CHARACTER", "pet_line": "Pet",
        "tab_combat": "COMBAT", "combat_boss": "Boss", "combat_dummy": "Training dummy", "combat_done": "Fight over",
        "combat_none": "No fight in progress", "combat_none_hint": "The board opens as soon as you hit a boss or the dummy.",
        "combat_sub": "{total} · {dps}/s", "c_power": "Combat power (after the name)",
        "combat_gone": "No fight in progress for this player.", "you": "you", "player": "Player",
        "p_damage": "Damage", "p_dps": "DPS", "p_share": "Share", "p_hits": "Hits", "p_crit": "Crit", "p_time": "Time", "p_power": "power",
        "p_skill": "Skill", "p_max": "Max",
        "combat_menu": "Combat (board)", "c_damage": "Total damage", "c_dps": "DPS", "c_share": "Contribution (share of damage)",
        "c_dps_full": "Full DPS (27,272)", "c_damage_short": "Short damage (1.92 M)", "c_class_colors": "Bars in class colors",
        "c_rows": "{n} rows", "c_width": "Width {n} px", "c_row_h": "{n} px rows", "c_keep": "Keep the board {n} s after the fight",
        "c_keep_next": "Keep the board until the next fight", "c_import": "Take NotMeter's settings",
        "combat_imported": "combat settings taken from NotMeter", "combat_no_notmeter": "NotMeter not found on this PC",
        "rec_start": "Record for genus insight (5 min)", "rec_mark": "Mark #{n}",
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

    cv = tk.Canvas(root, width=W, height=200, bg=ABYSS, highlightthickness=0, bd=0)
    cv.pack()

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
        cv.create_image(cx, cy, image=img)

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
        cv.create_image(x, cy, image=img, anchor="w")

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
        cv.create_image(x, y, image=img, anchor="nw")

    def draw():
        nonlocal W
        cv.delete("all")
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
            draw_sigil(cx, cy, p, maxed=need is None)
            if need is None:
                cv.create_text(cx, cy, text=(f"+{extra}" if extra else "✓"),
                               fill=DONE, font=f_ring)
            else:
                cv.create_text(cx, cy, text=str(need - pts), fill=PEARL, font=f_ring)
            cv.create_text(tx, cy - 20, text=fit(tr.name(hero), f_hero_name, tw),
                           fill=MIST if need is None else PEARL, font=f_hero_name, anchor="w")
            if need is None:
                badge = cv.create_text(tx + 7, cy + 4, text=L["maxed_short"], fill=ABYSS, font=f_num, anchor="w")
                x0, y0, x1, y1 = cv.bbox(badge)
                cv.tag_lower(cv.create_rectangle(x0 - 7, y0 - 2, x1 + 7, y1 + 2, fill=DONE, outline=""), badge)
                cv.create_text(tx, cy + 21, text=L["skip_hint"], fill=DONE, font=f_cap, anchor="w")
                if extra:
                    cv.create_text(tx, cy + 33, text=L["over_max_short"].format(n=extra), fill=MIST, font=f_cap, anchor="w")
            else:
                cv.create_text(tx, cy + 4, text=f"{pts} / {need}", fill=AETHER, font=f_mid, anchor="w")
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
                draw_pips(W - PAD - 132, cy_r, lvl, pts, maxed=need is None)
                if need is None:
                    txt, col = (f"{L['maxed_short']} +{extra}", DONE) if extra else (L["maxed_short"], DONE)
                else:
                    txt, col = f"{pts} / {need}", PEARL
                cv.create_text(W - PAD, cy_r, text=txt, fill=col, font=f_num, anchor="e")
                hits.append((y, y + ROW_H, mid))
                y += ROW_H

        # L'autre vue en une ligne : les récoltes du jour, s'il y en a eu.
        count = tr.gather_entry()["count"]
        if GATHER_READY and count:
            y = draw_side_line(y, L["gather_line"], f"{kfmt(int(count))} / {kfmt(GATHER_CAP)}", min(count / GATHER_CAP, 1.0),
                               ROSE if count >= GATHER_CAP * GATHER_NEAR else GOLD)
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
        cv.create_text(PAD + 12, fy, text=fit(foot, f_cap_font, W - 2 * PAD - 34),
                       fill=ROSE if RECORDER.active or kind in ("error", "nokey") else MIST, font=f_cap, anchor="w")
        cv.create_text(W - PAD, fy, text="⋯", fill=MIST, font=f_mid, anchor="e", tags="menu")

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
        cv.create_oval(x0 - 10, TABS_H / 2 - 3, x0 - 4, TABS_H / 2 + 3, fill=MIST if locked else AETHER, outline="", tags=("tab", "tab_auto"))
        cv.create_line(0, TABS_H, W, TABS_H, fill=RIM)

    def draw_side_line(y, label, value, ratio, color):
        """L'autre vue en une ligne : libellé, barre fine, valeur."""
        cv.create_line(PAD, y + 4, W - PAD, y + 4, fill=RIM)
        cy = y + 18
        cv.create_text(PAD, cy, text=label, fill=PEARL, font=f_cap, anchor="w")
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
    classes, skill_names = load_skills()
    decimal = "," if (tr.ui_lang or ui_language()) in ("fr", "ru") else "."
    PW = 420  # largeur de la fenêtre de détail d'un joueur

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
        """Vue Combat, compacte comme NotMeter : une ligne d'en-tête (cible, durée, total, DPS du groupe), puis une ligne
        par joueur (barre à la couleur de sa classe, nom, puissance de combat, colonnes choisies), toi toujours visible ;
        clic sur une ligne : ses sorts et ses dégâts. En bas, le dernier familier en une ligne."""
        b = LIVE.board()
        y = PAD - 4
        if not b or not b["players"]:
            cv.create_text(PAD, y + 12, text=L["combat_none"], fill=PEARL, font=f_row_name, anchor="w")
            hint = cv.create_text(PAD, y + 24, text=L["combat_none_hint"], fill=MIST, font=f_cap, anchor="nw", width=W - 2 * PAD)
            y = cv.bbox(hint)[3] + 8
        else:
            cfg, colors = tr.combat, tr.combat["colors"]
            players, total, dur = b["players"], b["total"], b["duration"]
            title = L["combat_done"] if b["ended"] else L["combat_dummy"] if b["code"] in TRAINING_DUMMIES else L["combat_boss"]
            head = cv.create_text(PAD, y + 9, text=title.upper(), fill=MIST if b["ended"] else PEARL, font=f_tab, anchor="w")
            cv.create_text(cv.bbox(head)[2] + 8, y + 9, text=f"{int(dur) // 60}:{int(dur) % 60:02d}",
                           fill=MIST if b["ended"] else colors["time"], font=f_tab, anchor="w")
            cv.create_text(W - PAD, y + 9, text=L["combat_sub"].format(total=short(total), dps=short(total / dur)),
                           fill=MIST, font=f_cap, anchor="e")
            y += 20
            ranks = {who: i + 1 for i, (who, *_) in enumerate(players)}
            rows, row_h = cfg["rows"], cfg["row_h"]
            shown = players[:rows]
            if all(who != "me" for who, *_ in shown):  # toi toujours visible, à ton rang
                me = next((p for p in players if p[0] == "me"), None)
                if me:
                    shown = players[:rows - 1] + [me]
            top = max(players[0][1]["damage"], 1)
            # Colonnes choisies (comme NotMeter : dégâts totaux, DPS, contribution), de gauche à droite, calées à droite.
            columns = [(key, width) for key, width in (("damage", 50), ("dps", 50), ("share", 32)) if cfg[key]]
            for who, s, number, name, power in shown:
                tag = ("prow", f"prow_{who}")
                mine = who == "me"
                base = CLASS_COLORS.get(s["cls"], MIST) if cfg["class_colors"] else (AETHER if mine else MIST)
                cv.create_rectangle(PAD, y + 1, PAD + max(2, (W - 2 * PAD) * s["damage"] / top), y + row_h - 1,
                                    fill=mix(ABYSS, base, 0.4 if mine else 0.26), outline="", tags=tag)
                cy = y + row_h / 2
                cv.create_text(PAD + 4, cy, text=str(ranks[who]), fill=MIST, font=f_cap, anchor="w", tags=tag)
                values = {"damage": (short(s["damage"]) if cfg["damage_short"] else kfmt(int(s["damage"])), colors["damage"]),
                          "dps": (kfmt(int(s["damage"] / dur)) if cfg["dps_full"] else short(s["damage"] / dur), colors["dps"]),
                          "share": (f"{round(100 * s['damage'] / max(total, 1))} %", colors["share"])}
                x = W - PAD - 4
                for key, width in reversed(columns):
                    text, color = values[key]
                    cv.create_text(x, cy, text=text, fill=color, font=f_cb_val, anchor="e", tags=tag)
                    x -= width
                power_txt = kfmt(power) if cfg["power"] and power else ""
                room = x - (PAD + 18) - 6 - (f_cap_font.measure(power_txt) + 6 if power_txt else 0)
                label = cv.create_text(PAD + 18, cy, text=fit(player_label(who, number, name), f_cb_name, max(40, room)),
                                       fill=AETHER if mine else PEARL, font=f_cb_name, anchor="w", tags=tag)
                if power_txt:
                    cv.create_text(cv.bbox(label)[2] + 6, cy + 1, text=power_txt, fill=colors["power"], font=f_cap, anchor="w", tags=tag)
                y += row_h
            y += 2
        if hero is not None:  # le familier gagné pendant le combat est compté, sans prendre la place du tableau
            lvl, pts, need, extra = tr.view(hero)
            y = draw_side_line(y, fit(f"{L['pet_line']} · {tr.name(hero)}", f_cap_font, 210),
                               L["maxed_short"] if need is None else f"{pts} / {need}", None, DONE if need is None else AETHER)
        draw_footer(y)

    popups = {}

    def open_player(who):
        """Détail d'un joueur, comme dans un DPS meter : ses chiffres et ses sorts sur le boss, mis à jour chaque seconde."""
        old = popups.get(who)
        if old is not None and old.winfo_exists():
            old.lift()
            return
        win = tk.Toplevel(root)
        win.title(APP_NAME)
        win.configure(bg=ABYSS)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.geometry(f"+{root.winfo_x() + W + 8}+{root.winfo_y()}")
        pc = tk.Canvas(win, width=PW, height=120, bg=ABYSS, highlightthickness=0)
        pc.pack()
        win.bind("<Escape>", lambda e: win.destroy())
        popups[who] = win

        def render():
            if not win.winfo_exists():
                return
            b = LIVE.board()
            row = next((p for p in b["players"] if p[0] == who), None) if b else None
            pc.delete("all")
            y = 14
            if row is None:
                pc.create_text(14, y + 8, text=L["combat_gone"], fill=MIST, font=f_cap, anchor="w")
                y += 30
            else:
                _, s, number, name, power = row
                dur = b["duration"]
                pc.create_text(14, y + 8, text=player_label(who, number, name), fill=AETHER if who == "me" else PEARL, font=f_hero_name, anchor="w")
                cls_txt = classes.get(s["cls"], "") + (f" · {L['p_power']} {kfmt(power)}" if power else "")
                pc.create_text(PW - 14, y + 8, text=cls_txt, fill=MIST, font=f_cap, anchor="e")
                y += 28
                kpis = [(L["p_damage"], short(s["damage"])), (L["p_dps"], short(s["damage"] / dur)),
                        (L["p_share"], f"{round(100 * s['damage'] / max(b['total'], 1))} %"), (L["p_hits"], kfmt(s["hits"])),
                        (L["p_crit"], f"{round(100 * s['crits'] / max(s['hits'], 1))} %"), (L["p_time"], f"{int(dur) // 60}:{int(dur) % 60:02d}")]
                col = (PW - 28) / 3
                for i, (k, v) in enumerate(kpis):
                    x, yy = 14 + (i % 3) * col, y + (i // 3) * 36
                    pc.create_text(x, yy, text=k, fill=MIST, font=f_cap, anchor="nw")
                    pc.create_text(x, yy + 12, text=v, fill=PEARL, font=f_mid, anchor="nw")
                y += 76
                pc.create_line(14, y, PW - 14, y, fill=RIM)
                y += 12
                columns = [(L["p_skill"], 14, "w"), (L["p_hits"], PW - 214, "e"), (L["p_damage"], PW - 150, "e"),
                           ("%", PW - 108, "e"), (L["p_crit"], PW - 62, "e"), (L["p_max"], PW - 14, "e")]
                for label, x, anchor in columns:
                    pc.create_text(x, y, text=label, fill=MIST, font=f_cap, anchor=anchor)
                y += 14
                skills = sorted(s["skills"].items(), key=lambda kv: -kv[1]["damage"])[:16]
                best = max((k["damage"] for _, k in skills), default=1) or 1
                for code, k in skills:
                    pc.create_rectangle(14, y + 1, 14 + (PW - 28) * k["damage"] / best, y + 18,
                                        fill=mix(ABYSS, AETHER if who == "me" else MIST, 0.16), outline="")
                    cells = [fit(skill_names.get(code, str(code)), f_row_name, PW - 250), kfmt(k["hits"]), short(k["damage"]),
                             f"{round(100 * k['damage'] / max(s['damage'], 1))} %", f"{round(100 * k['crits'] / max(k['hits'], 1))} %",
                             short(k["max"])]
                    for (label, x, anchor), text in zip(columns, cells):
                        pc.create_text(x, y + 10, text=text, fill=PEARL, font=f_row_name if anchor == "w" else f_num, anchor=anchor)
                    y += 20
            pc.config(height=y + 10)
            win.after(1000, render)
        render()

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
        """Vue Récolte : récoltes du jour (compteur du jeu, ratées comprises) sur le plafond (alerte dès 90 %), par
        serveur ; en bas, le dernier familier en une ligne. Pas de liste par ressource : le jeu n'envoie qu'un numéro
        d'objet, pas son nom."""
        entry, cap = tr.gather_entry(), GATHER_CAP
        count = entry["count"]
        full, near = count >= cap, count >= cap * GATHER_NEAR
        color = ROSE if near else GOLD
        reset = next_reset()
        reset_at = time.strftime("%H:%M", time.localtime(reset))
        y = PAD
        cv.create_text(PAD, y + 10, text=L["gather_full_title"] if full else L["gather_title"], fill=PEARL, font=f_hero_name, anchor="w")
        cap_id = cv.create_text(W - PAD, y + 12, text=f" / {kfmt(cap)}", fill=MIST, font=f_num, anchor="e")
        cv.create_text(cv.bbox(cap_id)[0], y + 10, text=kfmt(int(count)), fill=color, font=f_big, anchor="e")
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
            sub += L["gather_failed"].format(n=kfmt(int(entry["failed"])))
        sub_id = cv.create_text(PAD, y + 42, text=sub, fill=ROSE if near and not full else MIST, font=f_cap, anchor="nw", width=W - 2 * PAD)
        y = cv.bbox(sub_id)[3] + 6

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
        cv.create_text(PAD, cy_k, text=L["kina"], fill=PEARL, font=f_row_name, anchor="w")
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
            cv.tag_lower(cv.create_rectangle(x0 - 7, y0 - 2, x1 + 7, y1 + 2, fill=DONE, outline=""), rid)
            right = x0 - 14
        else:
            right = cv.bbox(rid)[0] - 10
        if layout == "mini":
            if not maxed:
                draw_pips(right - 36, PAD + 10, lvl, pts)
                right -= 44
            cv.create_text(PAD, PAD + 10, text=fit(tr.name(hero), f_row_name, right - PAD),
                           fill=PEARL, font=f_row_name, anchor="w")
        else:
            cv.create_text(PAD, PAD + 10, text=fit(tr.name(hero), f_hero_name, right - PAD),
                           fill=PEARL, font=f_hero_name, anchor="w")
            draw_bar(PAD, PAD + 26, W - 2 * PAD, 8, p, maxed)
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
            if ev[0] == "gather" or (ev[0] == "gather_count" and ev[2]):
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
        if visible["v"]:
            root.lift()
            root.attributes("-topmost", True)
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
        m = tk.Menu(root, tearoff=0, bg=ABYSS, fg=PEARL, activebackground=RIM,
                    activeforeground=PEARL, bd=0, font=(CAP, 9))
        for it in items:
            if it is None:
                m.add_separator()
            elif isinstance(it[1], list):
                sub = tk.Menu(m, tearoff=0, bg=ABYSS, fg=PEARL, activebackground=RIM, font=(CAP, 9))
                for sub_it in it[1]:
                    if sub_it is None:
                        sub.add_separator()
                    else:
                        sub.add_command(label=sub_it[0], command=sub_it[1])
                m.add_cascade(label=it[0], menu=sub)
            else:
                m.add_command(label=it[0], command=it[1])
        m.tk_popup(e.x_root, e.y_root)
        hide["last"] = time.time()  # menu fermé : pas de masquage immédiat si la souris est restée sur le menu

    def main_menu(e):
        def clear():
            tr.recent.clear(); dirty["v"] = True; draw()
        def set_opacity(a):
            opacity["v"] = a
            if not hide["hidden"]:
                root.attributes("-alpha", a)
        alpha = [(f"{int(a * 100)}%", lambda a=a: set_opacity(a)) for a in (1.0, 0.94, 0.8, 0.6)]
        menu_at(e, [(L["clear"], clear), (L["opacity"], alpha), ("Langue / Language", lang_items()),
                    ("Langue du jeu / Game language", game_lang_items()), None,
                    (L["layout"], layout_items()), (L["theme"], theme_items()), (L["kina_menu"], kina_items()), (L["hide_menu"], hide_items()), (L["combat_menu"], combat_items()),
                    (L["sync_key"], ask_key), (L["sync_now"], lambda: syncer.kick(now=True)),
                    *([(L["update_menu"].format(v=update_state["v"]), apply_update)] if update_state["v"]
                      else [(L["check_update"], lambda: updater.check_now())]),
                    (L["open_site"], lambda: webbrowser.open(SYNC_URL.rsplit("/api/", 1)[0])), None,
                    *rec_items(), (L["fights_menu"], fight_items()), None,
                    *([(L["to_tray"], toggle_visible)] if tray["icon"] else []), (L["quit"], quit_)])

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
                (mark(cfg["share"]) + L["c_share"], flip("share")), None,
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
        return [*[(mark(cur == k) + L["layout_" + k], lambda k=k: set_layout(k)) for k in ("rings", "bar", "mini")], None,
                (mark(tr.show_list) + L["layout_list"], flip_list), None,
                *[(mark(tr.width == n) + L["layout_width"].format(n=n), lambda n=n: width(n)) for n in (270, 300, 330)]]

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
