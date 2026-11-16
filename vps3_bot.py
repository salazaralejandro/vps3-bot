#!/usr/bin/env python3
"""
Bot que avisa cuando el VPS-3 de OVH queda libre en algún datacenter de Europa.
Fuente: https://vps-in-stock.ovh/

Solo usa la librería estándar de Python (3.9+).

Avisos:
  - Notificación de macOS (siempre, si estás en un Mac)
  - Telegram (opcional): define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID

Uso:
  python3 vps3_bot.py            # bucle: comprueba cada INTERVAL segundos
  python3 vps3_bot.py --once     # una sola comprobación (para cron/launchd)
  python3 vps3_bot.py --status   # muestra el estado actual y sale, sin avisar
  python3 vps3_bot.py --test     # envía un aviso de prueba
  python3 vps3_bot.py --setup-telegram   # conecta tu bot de Telegram (una vez)

Variables de entorno (todas opcionales):
  MODEL=vps-2027-model3     plan a vigilar
  OS=linux                  linux | windows | any
  INTERVAL=120              segundos entre comprobaciones
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

URL = "https://vps-in-stock.ovh/"
MODEL = os.environ.get("MODEL", "vps-2027-model3")
OS_WANTED = os.environ.get("OS", "linux").lower()
INTERVAL = int(os.environ.get("INTERVAL", "120"))
STATE_FILE = Path(__file__).with_name(".vps3_state.json")
TG_FILE = Path(__file__).with_name(".telegram.json")


def _load_tg():
    try:
        cfg = json.loads(TG_FILE.read_text())
    except Exception:  # noqa: BLE001
        cfg = {}
    return (
        os.environ.get("TELEGRAM_TOKEN") or cfg.get("token", ""),
        os.environ.get("TELEGRAM_CHAT_ID") or str(cfg.get("chat_id", "")),
    )


TG_TOKEN, TG_CHAT = _load_tg()
CONFIG_URL = f"https://www.ovhcloud.com/es-es/vps/configurator/?planCode={MODEL}"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch_page():
    req = urllib.request.Request(URL, headers={"User-Agent": "vps3-stock-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def clean(cell):
    text = re.sub(r"<[^>]+>", " ", cell)
    return " ".join(html.unescape(text).split())


def parse_stock(page):
    """Devuelve {codigo_dc: {"name", "linux", "windows"}} para los DC de Europa."""
    m = re.search(
        r"<h2>[^<]*\(" + re.escape(MODEL) + r"\)</h2>(.*?)(?=<h2>|\Z)", page, re.S
    )
    if not m:
        raise RuntimeError(f"No encuentro la sección de {MODEL} en la página")
    result = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 5:
            continue
        name, code, _overall, linux, windows = cells[:5]
        if not code.startswith("eu-"):  # solo Europa
            continue
        result[code] = {
            "name": name,
            "linux": linux.startswith("available"),
            "windows": windows.startswith("available"),
        }
    if not result:
        raise RuntimeError("La sección existe pero no he leído ningún datacenter")
    return result


def is_free(dc):
    if OS_WANTED == "windows":
        return dc["windows"]
    if OS_WANTED == "any":
        return dc["linux"] or dc["windows"]
    return dc["linux"]


def notify(title, body):
    log(f"AVISO: {title} — {body}")
    if sys.platform == "darwin":
        script = (
            f"display notification {json.dumps(body, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)} "
            f'sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script], check=False)
    if TG_TOKEN and TG_CHAT:
        data = urllib.parse.urlencode(
            {"chat_id": TG_CHAT, "text": f"{title}\n{body}", "disable_web_page_preview": "true"}
        ).encode()
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=20
            )
        except Exception as e:  # noqa: BLE001
            log(f"Error enviando a Telegram: {e}")


def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:  # noqa: BLE001
        return set()


def save_state(free):
    STATE_FILE.write_text(json.dumps(sorted(free)))


def check(send=True):
    stock = parse_stock(fetch_page())
    free = {code for code, dc in stock.items() if is_free(dc)}
    previous = load_state()
    newly = free - previous
    gone = previous - free

    summary = ", ".join(f"{stock[c]['name']} ({c})" for c in sorted(free)) or "ninguno"
    log(f"{MODEL} [{OS_WANTED}] libre en Europa: {summary}")

    if send and newly:
        names = ", ".join(f"{stock[c]['name']} ({c})" for c in sorted(newly))
        notify(f"🟢 VPS-3 disponible en Europa ({OS_WANTED})", f"{names}\n{CONFIG_URL}")
    if send and gone:
        log("Se ha agotado en: " + ", ".join(sorted(gone)))
    if send:
        save_state(free)
    return stock, free


def tg_api(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(url, data, timeout=20) as r:
        return json.loads(r.read().decode())


def setup_telegram():
    """Pide el token del bot, detecta tu chat_id y lo guarda en .telegram.json."""
    import getpass

    print("1) En Telegram, habla con @BotFather → /newbot → copia el token.")
    token = getpass.getpass("Pega el token (no se mostrará): ").strip()
    try:
        me = tg_api(token, "getMe")["result"]
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Token no válido: {e}")
    print(f"\n2) Abre https://t.me/{me['username']} , pulsa Iniciar y escríbele cualquier cosa.")
    print("   Esperando tu mensaje (hasta 2 minutos)...")
    chat_id = None
    for _ in range(60):
        updates = tg_api(token, "getUpdates").get("result", [])
        for u in reversed(updates):
            msg = u.get("message") or u.get("edited_message")
            if msg:
                chat_id = msg["chat"]["id"]
                break
        if chat_id:
            break
        time.sleep(2)
    if not chat_id:
        sys.exit("No ha llegado ningún mensaje. Vuelve a ejecutar --setup-telegram.")
    TG_FILE.write_text(json.dumps({"token": token, "chat_id": chat_id}))
    os.chmod(TG_FILE, 0o600)
    tg_api(token, "sendMessage", {"chat_id": chat_id, "text": "✅ Bot VPS-3 conectado. Te avisaré aquí."})
    print(f"\nListo. Guardado en {TG_FILE.name}. Te he mandado un mensaje de confirmación.")


def main():
    args = set(sys.argv[1:])
    if "--setup-telegram" in args:
        setup_telegram()
        return
    if "--test" in args:
        notify("🔔 Prueba del bot VPS-3", "Si ves esto, los avisos funcionan.")
        return
    if "--status" in args:
        stock, _ = check(send=False)
        for code, dc in sorted(stock.items()):
            print(f"  {dc['name']:<14} {code:<16} linux={'SÍ' if dc['linux'] else 'no':<3} "
                  f"windows={'SÍ' if dc['windows'] else 'no'}")
        return
    if "--once" in args:
        check()
        return
    log(f"Vigilando {MODEL} (OS={OS_WANTED}) cada {INTERVAL}s. Ctrl+C para parar.")
    while True:
        try:
            check()
        except Exception as e:  # noqa: BLE001
            log(f"Error en la comprobación: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
