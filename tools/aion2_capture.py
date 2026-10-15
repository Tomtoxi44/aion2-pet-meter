"""Запись трафика Aion 2 для поиска пакета с очками духа.

Запуск:  python aion2_capture.py [имя_файла.jsonl]
  F9  - метка "убил моба" (нажимать, когда в чате появилось "получены очки духа")
  F10 - произвольная метка (например, "открыл окно питомцев")
  Ctrl+C или файл stop.flag рядом со скриптом - остановить запись
"""
import json, os, sys, time, threading
import psutil
from scapy.all import sniff, conf, IP, TCP, Raw

OUT = sys.argv[1] if len(sys.argv) > 1 else time.strftime("capture_%Y%m%d_%H%M%S.jsonl")
STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop.flag")
if os.path.exists(STOP_FILE):
    os.remove(STOP_FILE)

# Локальные порты TCP-соединений игры (кроме http/https); обновляются на лету,
# т.к. при смене персонажа игра может переподключиться к другому серверу.
game_ports = {}  # local_port -> "ip:port" сервера


def refresh_game_ports():
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "aion2.exe"}
    ports = {}
    for c in psutil.net_connections(kind="tcp"):
        if c.pid in pids and c.raddr and not c.raddr[0].startswith("127.") and c.raddr[1] not in (80, 443):
            ports[c.laddr[1]] = f"{c.raddr[0]}:{c.raddr[1]}"
    for lp, srv in ports.items():
        if game_ports.get(lp) != srv:
            print(f"\nСоединение игры: :{lp} <-> {srv}", flush=True)
    game_ports.update(ports)


refresh_game_ports()
if not game_ports:
    print("Соединение игры пока не найдено, жду...")
ifaces = [i.network_name for i in conf.ifaces.values() if i.ip and not i.ip.startswith("127.")]
print(f"Запись -> {OUT}")

lock = threading.Lock()
out = open(OUT, "w", encoding="utf-8")
t0 = time.time()
stats = {"pkts": 0, "kills": 0, "marks": 0}
stop = threading.Event()


def write(rec):
    with lock:
        out.write(json.dumps(rec) + "\n")
        out.flush()


def on_packet(p):
    if IP not in p or TCP not in p or Raw not in p:
        return
    tcp = p[TCP]
    if tcp.dport in game_ports:
        d, conn = "S", f"{p[IP].src}:{tcp.sport}"
    elif tcp.sport in game_ports:
        d, conn = "C", f"{p[IP].dst}:{tcp.dport}"
    else:
        return
    write({"t": round(time.time() - t0, 4), "d": d, "conn": conn,
           "seq": tcp.seq, "data": bytes(p[Raw].load).hex()})
    stats["pkts"] += 1


def mark(kind):
    key = "kills" if kind == "kill" else "marks"
    stats[key] += 1
    write({"t": round(time.time() - t0, 4), "mark": kind, "n": stats[key]})
    print(f"\n[{kind} #{stats[key]}]", flush=True)


try:
    import keyboard
    keyboard.add_hotkey("f9", lambda: mark("kill"))
    keyboard.add_hotkey("f10", lambda: mark("mark"))
    print("F9 = убил моба, F10 = метка, Ctrl+C = стоп")
except Exception as e:  # хоткеи не обязательны
    print("Горячие клавиши недоступны:", e)


def background():
    while not stop.is_set():
        time.sleep(2)
        refresh_game_ports()
        if os.path.exists(STOP_FILE):
            stop.set()
        print(f"\rпакетов: {stats['pkts']}  убийств: {stats['kills']}  меток: {stats['marks']}   ", end="", flush=True)


threading.Thread(target=background, daemon=True).start()
try:
    sniff(iface=ifaces, filter="tcp and not port 443 and not port 80", prn=on_packet,
          store=False, stop_filter=lambda _: stop.is_set())
except KeyboardInterrupt:
    pass
finally:
    out.close()
    print(f"\nСохранено: {OUT}")
