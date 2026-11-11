"""Звіти: щоденний підсумок і перегляди через 24 год після публікації."""
from __future__ import annotations

import html
import logging
from datetime import datetime, time as dtime, timedelta

from config import NAMES, PLATFORMS, SHORT, fmt_slot, local_dt, now_ts
from meta import read_json, write_json

log = logging.getLogger("reports")
esc = html.escape


def daily_report(app, now: int | None = None) -> str:
    now = now or now_ts()
    cfg, db = app.cfg, app.db
    day = local_dt(cfg, now).date()
    start = int(datetime.combine(day, dtime(0, 0), cfg["tz"]).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), dtime(0, 0), cfg["tz"]).timestamp())
    ev = db.events(start, end)

    lines = [f"📊 <b>Підсумок за {day.strftime('%d.%m.%Y')}</b>"]

    published = [e for e in ev if e["kind"] == "published"]
    lines.append(f"\n✅ <b>Опубліковано: {len(published)}</b>")
    for e in published:
        d = e["details"] or {}
        links = " ".join(f'<a href="{esc(u)}">{SHORT[p]}</a>' if u else SHORT[p]
                         for p, u in (d.get("urls") or {}).items())
        fail = f" | ❌ {', '.join(SHORT[p] for p in d.get('failed', []))}" if d.get("failed") else ""
        lines.append(f"• {local_dt(cfg, e['ts']).strftime('%H:%M')} {esc(d.get('title', ''))} — {links}{fail}")

    skipped = [e for e in ev if e["kind"] == "skipped"]
    moved = [e for e in ev if e["kind"] == "moved_to_skipped"]
    empty = [e for e in ev if e["kind"] == "empty_slot"]
    lines.append(f"\n⏭ <b>Пропущено слотів: {len(skipped)}</b>" + (f" (порожніх слотів: {len(empty)})" if empty else ""))
    for e in skipped:
        d = e["details"] or {}
        lines.append(f"• {esc(d.get('title', ''))} ({d.get('skips')}/{cfg['max_skips']})")
    for e in moved:
        lines.append(f"• 🗑 {esc((e['details'] or {}).get('title', ''))} → skipped/")

    errors = [e for e in ev if e["kind"] in ("platform_error", "publish_failed")]
    lines.append(f"\n⚠️ <b>Помилки: {len(errors)}</b>")
    for e in errors:
        d = e["details"] or {}
        if e["kind"] == "platform_error":
            lines.append(f"• #{e['item_id']} {NAMES.get(d.get('platform'), d.get('platform'))}: "
                         f"{esc(str(d.get('error', ''))[:150])}")
        else:
            lines.append(f"• #{e['item_id']} {esc(d.get('title', ''))}: не опубліковано ніде")

    queue = db.queue()
    nxt = app.scheduler.upcoming(now, 1)
    lines.append(f"\n📦 У черзі: {len(queue)}"
                 + (f" | Наступний слот: {fmt_slot(cfg, nxt[0]['slot_at'], now)}" if nxt else ""))
    if cfg.get("dry_run"):
        lines.append("🧪 Режим dry_run — реальних публікацій немає")
    return "\n".join(lines)


def send_stats(app, item: dict) -> None:
    """Перегляди через 24 год: повідомлення в Telegram + запис у json."""
    from meta import load_meta

    db = app.db
    views: dict[str, int | None | str] = {}
    for p in PLATFORMS:
        r = item["results"].get(p) or {}
        if r.get("status") != "ok" or p not in app.platforms:
            continue
        try:
            views[p] = None if app.cfg.get("dry_run") else app.platforms[p].get_views(r)
        except Exception as e:  # noqa: BLE001
            log.warning("Перегляди #%s %s: %s", item["id"], p, e)
            views[p] = f"помилка: {str(e)[:80]}"
    db.update_item(item["id"], stats_sent=1)
    try:
        title = load_meta(item["json"], item["title_override"]).yt_title
    except Exception:  # noqa: BLE001
        title = f"#{item['id']}"
    lines = [f"👁 <b>Перегляди за 24 год</b>\n🎬 {esc(title)}"]
    total = 0
    for p, v in views.items():
        url = item["results"][p].get("url")
        name = f'<a href="{esc(url)}">{NAMES[p]}</a>' if url else NAMES[p]
        if isinstance(v, int):
            total += v
            lines.append(f"{SHORT[p]} {name}: <b>{v:,}</b>".replace(",", " "))
        elif v is None:
            lines.append(f"{SHORT[p]} {name}: н/д" + (" (приватний пост)" if p == "tiktok" else ""))
        else:
            lines.append(f"{SHORT[p]} {name}: {esc(v)}")
    lines.append(f"Разом: <b>{total:,}</b>".replace(",", " "))
    app.bot.send("\n".join(lines), html_mode=True)
    try:
        data = read_json(item["json"])
        data.setdefault("posting", {})["views_24h"] = views
        write_json(item["json"], data)
    except Exception as e:  # noqa: BLE001
        log.warning("Не вдалося записати перегляди в json #%s: %s", item["id"], e)
