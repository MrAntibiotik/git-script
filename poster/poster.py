"""poster — автопублікація шортсів на YouTube / TikTok / Instagram / Facebook
з підтвердженням через Telegram.

    python poster.py                 запустити службу (бот + розклад)
    python poster.py check           перевірити налаштування і доступи
    python poster.py auth youtube    авторизація YouTube (OAuth у браузері)
    python poster.py auth tiktok     авторизація TikTok
    python poster.py auth meta       токен сторінки Facebook + Instagram
    python poster.py queue           показати чергу
    python poster.py test-slot 10    позаплановий слот через 10 хв (для тесту)
"""
from __future__ import annotations

import argparse
import html
import logging
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import NAMES, ConfigError, enabled_platforms, fmt_slot, load_config, now_ts, setup_logging  # noqa: E402
from db import DB  # noqa: E402
from platforms import build_platforms, get_class  # noqa: E402

log = logging.getLogger("poster")
esc = html.escape


class App:
    def __init__(self, cfg: dict, bot=None):
        self.cfg = cfg
        for key in ("ready", "posted", "skipped", "data", "tokens"):
            Path(cfg["paths"][key]).mkdir(parents=True, exist_ok=True)
        self.db = DB(Path(cfg["paths"]["data"]) / "poster.sqlite3")
        self.platforms = build_platforms(cfg)
        from publisher import Publisher
        from scheduler import Scheduler

        self.publisher = Publisher(self)
        self.scheduler = Scheduler(self)
        if bot is None:
            from bot import Bot
            bot = Bot(self)
        self.bot = bot

    # ---------- тексти для команд ----------
    def queue_text(self) -> str:
        now = now_ts()
        active = self.db.items("status IN ('notified','approved','publishing')", order="slot_at")
        queue = self.db.queue()
        lines = [f"📦 <b>Черга: {len(queue)}</b>"]
        st_names = {"notified": "⏳ чекає підтвердження", "approved": "👍 підтверджено", "publishing": "⏫ публікується"}
        for it in active:
            slot = fmt_slot(self.cfg, it["slot_at"], now) if it["slot_at"] else ""
            lines.append(f"▶️ #{it['id']} {esc(Path(it['mp4']).stem)} — {st_names[it['status']]} {slot}")
        for n, it in enumerate(queue[:25], 1):
            game = f"[{esc(it['game'])}] " if it["game"] else ""
            sk = f" (пропусків: {it['skips']})" if it["skips"] else ""
            lines.append(f"{n}. #{it['id']} {game}{esc(Path(it['mp4']).stem)}{sk}")
        if len(queue) > 25:
            lines.append(f"… і ще {len(queue) - 25}")
        failed = self.db.items("status='failed'")
        if failed:
            lines.append("\n❌ Не опубліковано: " + ", ".join(f"#{i['id']}" for i in failed) + " (/retry N)")
        return "\n".join(lines)

    def status_text(self) -> str:
        now = now_ts()
        lines = ["🕐 <b>Найближчі слоти</b>"]
        for s in self.scheduler.upcoming(now, 6):
            who = f"#{s['item_id']}" if s["item_id"] else "—"
            src = " (ручний)" if s["source"] == "manual" else ""
            lines.append(f"• {fmt_slot(self.cfg, s['slot_at'], now)}{src}: {who}")
        lines.append(f"\n📦 У черзі: {len(self.db.queue())}")
        lines.append("Платформи: " + ", ".join(NAMES[p] for p in enabled_platforms(self.cfg)))
        tt = self.cfg["platforms"]["tiktok"]
        if tt.get("enabled"):
            lines.append("TikTok: " + ("публічно" if tt.get("public") else "приватно (SELF_ONLY)"))
        if self.cfg.get("dry_run"):
            lines.append("🧪 dry_run увімкнено")
        return "\n".join(lines)


# ======================================================================
def single_instance(port: int) -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        return None


def run_service(cfg: dict) -> int:
    lock = single_instance(int(cfg["lock_port"]))
    if lock is None:
        log.error("poster уже запущено (порт %s зайнятий) — виходжу", cfg["lock_port"])
        return 1
    app = App(cfg)
    log.info("poster запущено. Платформи: %s. dry_run=%s", list(app.platforms), cfg.get("dry_run"))
    app.scheduler.scan()
    app.bot.send(f"🟢 poster запущено. У черзі: {len(app.db.queue())}. "
                 f"Найближчий слот: " + (fmt_slot(cfg, s[0]["slot_at"]) if (s := app.scheduler.upcoming(now_ts(), 1)) else "—"))
    last_tick = 0.0
    errors = 0
    while True:
        try:
            if time.time() - last_tick >= 15:
                last_tick = time.time()
                app.scheduler.tick()
            app.bot.poll_once(timeout=10)
            errors = 0
        except KeyboardInterrupt:
            log.info("Зупинено користувачем")
            return 0
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.exception("Помилка в головному циклі: %s", e)
            time.sleep(min(60, 5 * errors))


def cmd_check(cfg: dict) -> int:
    ok = True

    def line(mark, text):
        print(f" {mark} {text}")

    print("Налаштування:", cfg["config_path"])
    for key in ("ready", "posted", "skipped"):
        line("•", f"{key}: {cfg['paths'][key]}")
    line("•", f"Слоти: {', '.join(cfg['slots'])} ({cfg['timezone']})")
    import media
    try:
        r = media.run_hidden([cfg["ffmpeg"], "-version"], timeout=30)
        line("✅", "ffmpeg: " + r.stdout.decode(errors="ignore").splitlines()[0][:60])
    except media.MediaError as e:
        line("⚠️", f"ffmpeg: {e} (потрібен лише для файлів > 50 МБ)")
    try:
        from telegram_api import TelegramAPI
        me = TelegramAPI(cfg["secrets"]["TELEGRAM_BOT_TOKEN"]).call("getMe")
        line("✅", f"Telegram: @{me['username']}, chat_id={cfg['secrets']['TELEGRAM_CHAT_ID'] or '— НЕ ЗАДАНО'}")
        if not cfg["secrets"]["TELEGRAM_CHAT_ID"]:
            ok = False
    except Exception as e:  # noqa: BLE001
        ok = False
        line("❌", f"Telegram: {e}")
    for key in enabled_platforms(cfg):
        try:
            info = get_class(key)(cfg).check()
            line("✅", f"{NAMES[key]}: {info}")
        except Exception as e:  # noqa: BLE001
            ok = False
            line("❌", f"{NAMES[key]}: {type(e).__name__}: {e}")
    if cfg.get("dry_run"):
        line("🧪", "dry_run увімкнено — публікації імітуються")
    return 0 if ok else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Автопублікація шортсів")
    ap.add_argument("--config", help="шлях до config.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("check")
    sub.add_parser("queue")
    a = sub.add_parser("auth")
    a.add_argument("platform", choices=["youtube", "tiktok", "meta", "instagram", "facebook"])
    t = sub.add_parser("test-slot")
    t.add_argument("minutes", type=int, nargs="?", default=10)
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print("Помилка налаштувань:", e, file=sys.stderr)
        return 2
    setup_logging(cfg, args.verbose)
    os.chdir(cfg["root"])

    cmd = args.cmd or "run"
    if cmd == "run":
        return run_service(cfg)
    if cmd == "check":
        return cmd_check(cfg)
    if cmd == "auth":
        if args.platform in ("meta", "instagram", "facebook"):
            from platforms.meta_graph import authorize_meta
            authorize_meta(cfg)
        else:
            get_class(args.platform)(cfg).authorize()
        return 0

    class _NoBot:  # для CLI-команд без Telegram
        def __getattr__(self, name):
            return lambda *a, **k: None

    app = App(cfg, bot=_NoBot())
    if cmd == "queue":
        app.scheduler.scan()
        app.scheduler.ensure_slots(now_ts())
        import re
        print(re.sub(r"<[^>]+>", "", app.queue_text()))
        print(re.sub(r"<[^>]+>", "", app.status_text()))
    elif cmd == "test-slot":
        ts = app.scheduler.add_manual_slot(args.minutes)
        print(f"Додано позаплановий слот о {fmt_slot(cfg, ts)}. Запущена служба надішле сповіщення протягом хвилини.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
