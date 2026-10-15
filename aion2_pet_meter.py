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


def log(*a):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + " ".join(map(str, a)) + "\n")


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


def parse_death(p):
    eid, _ = read_varint(p, 0)
    return eid


def parse_crystal(p):
    """(espèce, quantité) ou None."""
    if len(p) < 6 or p[0] != 1:
        return None
    amount = int.from_bytes(p[2:6], "little")
    return (p[1], amount) if 0 < amount < 1000 else None


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


class Decoder:
    """Байты -> события ("state", levels, points) / ("gain", id, amount)."""

    def __init__(self, emit):
        self.emit = emit
        self.streams = {}
        self.mobs = {}     # entité -> code de créature (apparitions récentes)
        self.deaths = []   # [(heure, code)] des dernières morts, pour les cristaux

    def segment(self, conn, seq, data):
        st = self.streams.setdefault(conn, Stream())
        for fr in st.add(seq, data):
            try:
                for op, p in unpack(fr):
                    self.handle(op, p)
            except Exception as e:
                log("frame:", e, fr[:32].hex())

    def handle(self, op, p):
        """Un message du serveur : âmes, collection complète, apparitions / morts, cristaux."""
        if op == OP_GAIN:
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
        elif op == OP_DEATH:
            code = self.mobs.pop(parse_death(p), None)
            if code is not None:
                now = time.time()
                self.deaths = [d for d in self.deaths if now - d[0] < CRYSTAL_WINDOW] + [(now, code)]
        elif op == OP_CRYSTAL:
            r = parse_crystal(p)
            if r:
                now = time.time()
                recent = [d for d in self.deaths if now - d[0] < CRYSTAL_WINDOW]
                codes = [c for t, c in reversed(recent)]
                self.deaths = recent[:-1]  # une mort ne sert qu'une fois (la plus récente)
                self.emit(("crystal", r[0], r[1], codes))
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
        for mid in list(self.points):  # файлы старой версии копили очки без повышения уровня
            self.add_points(mid, 0)

    def save(self):
        d = {"levels": self.levels, "points": self.points, "extra": self.extra,
             "names": self.own_names(), "votes": self.votes_by_lang,
             "phrases": self.phrases, "ocr_lang": self.ocr_lang,
             "game_lang": self.game_lang, "ui_lang": self.ui_lang,
             "recent": self.recent, "hidden": sorted(self.hidden),
             "synced": self.synced, "pos": self.pos, "sync_key": self.sync_key, "theme": self.theme, "layout": self.layout}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)

    def on_event(self, ev):
        if ev[0] == "state":
            self.levels, self.points = ev[1], ev[2]
            self.synced = time.strftime("%d.%m %H:%M")
        elif ev[0] == "gain":
            _, mid, amount = ev
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
            self.levels[mid] = MAX_LEVEL  # des cristaux = familier forcément au max
            self.points[mid] = 0
            self.extra[mid] = self.extra.get(mid, 0) + ev[2]
            self.hidden.discard(mid)
            if mid in self.recent:
                self.recent.remove(mid)
            self.recent.insert(0, mid)
            del self.recent[30:]
        elif ev[0] == "vote":
            self.vote(*ev[1:])
        elif ev[0] == "phrase":
            self.phrases[ev[1]] = self.phrases.get(ev[1], 0) + 1
        elif ev[0] == "ocr_lang":
            self.ocr_lang = ev[1]

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
        import subprocess
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
        subprocess.Popen([exe], close_fds=True)
        return True


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
        "layout_rings": "Anneaux", "layout_bar": "Barre (compact)", "layout_mini": "Minimal (une ligne)",
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
    W, PAD, HERO_H, ROW_H, FOOT_H, ROWS = 330, 14, 100, 24, 24, 5
    RADII = (25, 34, 43)  # кольца уровней 1-3: чем больше порог, тем больше кольцо
    RING_W = 5

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
    f_mid = (NUM_M, 12)
    f_num = (NUM, 11)
    f_cap = (CAP, 8)
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
        cv.delete("all")
        hits.clear()
        layout = tr.layout or "rings"
        shown = [m for m in tr.recent if m not in tr.hidden][:(3 if layout == "mini" else ROWS) + 1]
        hero, rest = (shown[0] if shown else None), shown[1:]
        hero_bottom = PAD + HERO_H

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
            cv.create_text(cx, cy, text="!", fill=ROSE, font=f_big)
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
            cv.create_text(cx, cy, text="—", fill=MIST, font=f_big)
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
                               fill=DONE, font=f_big)
            else:
                cv.create_text(cx, cy, text=str(need - pts), fill=PEARL, font=f_big)
            cv.create_text(tx, cy - 20, text=fit(tr.name(hero), f_hero_name, tw),
                           fill=MIST if need is None else PEARL, font=f_hero_name, anchor="w")
            if need is None:
                badge = cv.create_text(tx + 7, cy + 4, text=L["maxed_short"], fill=ABYSS, font=f_num, anchor="w")
                x0, y0, x1, y1 = cv.bbox(badge)
                cv.tag_lower(cv.create_rectangle(x0 - 7, y0 - 2, x1 + 7, y1 + 2, fill=DONE, outline=""), badge)
                cv.create_text(tx, cy + 24, text=L["skip_hint"], fill=DONE, font=f_cap, anchor="w")
                if extra:
                    cv.create_text(tx, cy + 37, text=L["over_max_short"].format(n=extra), fill=MIST, font=f_cap, anchor="w")
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

        # --- подвал
        if info_state["v"] and time.time() < info_state["v"][1] and not update_state["v"]:
            cv.create_text(PAD, y + 12, text=L[info_state["v"][0]].format(v=VERSION), fill=AETHER, font=f_cap, anchor="w")
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
        cv.create_text(PAD + 12, fy, text=fit(foot, f_cap_font, W - 2 * PAD - 34),
                       fill=ROSE if kind in ("error", "nokey") else MIST, font=f_cap, anchor="w")
        cv.create_text(W - PAD, fy, text="⋯", fill=MIST, font=f_mid, anchor="e", tags="menu")

        cv.create_rectangle(0, 0, W - 1, h - 1, outline=RIM)

        cv.config(height=h)
        root.geometry(f"{W}x{h}")

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
            if ev[0] == "tray":  # команды из значка в трее (приходят из его потока)
                toggle_visible() if ev[1] == "toggle" else quit_()
                continue
            if ev[0] == "gain":
                mid = ev[1]
                start = anim["p"] if anim["mid"] == mid and anim["p"] is not None else progress(mid)
                tr.on_event(ev)
                anim.update(mid=mid, p=start, to=progress(mid), t0=time.time())
                anim["from"] = start
                root.after(16, tick_anim)
            else:
                tr.on_event(ev)
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

    cv.tag_bind("update", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("update", "<Leave>", lambda e: cv.config(cursor=""))
    cv.tag_bind("link", "<Enter>", lambda e: cv.config(cursor="hand2"))
    cv.tag_bind("link", "<Leave>", lambda e: cv.config(cursor=""))
    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", release)

    def quit_():
        if visible["v"]:
            tr.pos = [root.winfo_x(), root.winfo_y()]
        tr.save()
        if tray["icon"]:
            tray["icon"].stop()
        root.destroy()

    visible = {"v": True}
    tray = {"icon": None}

    def toggle_visible():
        if visible["v"]:
            tr.pos = [root.winfo_x(), root.winfo_y()]
            root.withdraw()
        else:
            root.deiconify()
            root.geometry(f"+{tr.pos[0]}+{tr.pos[1]}" if tr.pos else "")
            root.attributes("-topmost", True)
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

    def main_menu(e):
        def clear():
            tr.recent.clear(); dirty["v"] = True; draw()
        alpha = [(f"{int(a * 100)}%", lambda a=a: root.attributes("-alpha", a)) for a in (1.0, 0.94, 0.8, 0.6)]
        menu_at(e, [(L["clear"], clear), (L["opacity"], alpha), ("Langue / Language", lang_items()),
                    ("Langue du jeu / Game language", game_lang_items()), None,
                    (L["layout"], layout_items()), (L["theme"], theme_items()),
                    (L["sync_key"], ask_key), (L["sync_now"], lambda: syncer.kick(now=True)),
                    *([(L["update_menu"].format(v=update_state["v"]), apply_update)] if update_state["v"]
                      else [(L["check_update"], lambda: updater.check_now())]),
                    (L["open_site"], lambda: webbrowser.open(SYNC_URL.rsplit("/api/", 1)[0])), None,
                    *([(L["to_tray"], toggle_visible)] if tray["icon"] else []), (L["quit"], quit_)])

    def layout_items():
        cur = tr.layout or "rings"
        return [(("✓ " if cur == k else "    ") + L["layout_" + k], lambda k=k: set_layout(k)) for k in ("rings", "bar", "mini")]

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
    elif "--selftest" in sys.argv:
        selftest()
    else:
        run_overlay()
