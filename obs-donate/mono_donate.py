"""
Mono Donate - OBS-плагін (Python-скрипт) для донатів через банку Monobank.

Алерт виводиться прямо в сцену OBS, тому його бачать глядачі на всіх
платформах, куди йде стрім (Twitch, YouTube, TikTok, Kick ...), зокрема
при мультистрімі через Aitum Multistream / Restream.

Встановлення: OBS -> Інструменти -> Скрипти -> "+" -> вибрати цей файл.
Потрібен Python 3, налаштований у вкладці "Налаштування Python".
"""

import json
import queue
import threading
import time
import urllib.error
import urllib.request

try:
    import obspython as obs  # доступний лише всередині OBS
except ImportError:  # дозволяє імпортувати модуль для тестів поза OBS
    obs = None

API_BASE = "https://api.monobank.ua"
POLL_INTERVAL = 61  # Monobank дозволяє 1 запит виписки на 60 секунд
TICK_MS = 250

CURRENCIES = {980: "грн", 840: "USD", 978: "EUR", 985: "PLN", 826: "GBP"}

DEFAULT_TEMPLATE = "Донат від {name}: {amount} {currency}!\n{comment}"
DEFAULT_GOAL_TEMPLATE = "Зібрано: {balance} / {goal} {currency}"


# ---------------------------------------------------------------- логіка ---

def format_money(minor_units):
    """Копійки -> рядок без зайвих нулів: 15000 -> '150', 1550 -> '15.50'."""
    value = minor_units / 100
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def parse_name(description):
    """Monobank пише відправника як 'Від: Ім'я'. Повертає лише ім'я."""
    description = (description or "").strip()
    for prefix in ("Від:", "From:"):
        if description.startswith(prefix):
            return description[len(prefix):].strip() or "Анонім"
    return description or "Анонім"


def build_alert_text(item, template):
    """Будує текст алерту з запису виписки Monobank."""
    data = {
        "name": parse_name(item.get("description")),
        "amount": format_money(item.get("amount", 0)),
        "currency": CURRENCIES.get(item.get("currencyCode"), "грн"),
        "comment": (item.get("comment") or "").strip(),
    }
    try:
        text = template.format(**data)
    except (KeyError, IndexError, ValueError):
        text = DEFAULT_TEMPLATE.format(**data)
    return text.strip()


def filter_new_donations(items, seen_ids, min_amount_minor):
    """Повертає нові вхідні платежі від старих до нових, оновлює seen_ids."""
    result = []
    for item in sorted(items, key=lambda i: i.get("time", 0)):
        item_id = item.get("id")
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        if item.get("amount", 0) >= max(min_amount_minor, 1):
            result.append(item)
    return result


class MonoApi:
    def __init__(self, token):
        self.token = token.strip()

    def _get(self, path):
        req = urllib.request.Request(
            API_BASE + path,
            headers={"X-Token": self.token, "User-Agent": "obs-mono-donate"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def jars(self):
        return self._get("/personal/client-info").get("jars", [])

    def statement(self, jar_id, since):
        return self._get(f"/personal/statement/{jar_id}/{int(since)}")


class Poller(threading.Thread):
    """Фоновий потік: опитує банку і кладе нові донати в чергу."""

    def __init__(self, token, jar_id, min_amount_minor, out_queue):
        super().__init__(daemon=True)
        self.api = MonoApi(token)
        self.jar_id = jar_id
        self.min_amount_minor = min_amount_minor
        self.out = out_queue
        self.stop_event = threading.Event()
        self.seen = set()
        self.since = time.time()  # старі донати не показуємо

    def run(self):
        while not self.stop_event.is_set():
            try:
                items = self.api.statement(self.jar_id, self.since - 60)
                for item in filter_new_donations(items, self.seen,
                                                 self.min_amount_minor):
                    self.out.put(("donation", item))
                jar = next((j for j in self.api_jars_cached() if j.get("id") == self.jar_id), None)
                if jar:
                    self.out.put(("jar", jar))
            except urllib.error.HTTPError as e:
                self.out.put(("error", f"Monobank HTTP {e.code}"))
            except Exception as e:  # мережа, JSON тощо
                self.out.put(("error", str(e)))
            self.stop_event.wait(POLL_INTERVAL)

    def api_jars_cached(self):
        # client-info теж обмежений 1 запитом/хв, тому оновлюємо баланс
        # банки не частіше ніж раз на 5 хвилин.
        now = time.time()
        if now - getattr(self, "_jars_at", 0) >= 300:
            self._jars_at = now
            try:
                self._jars = self.api.jars()
            except Exception:
                self._jars = getattr(self, "_jars", [])
        return getattr(self, "_jars", [])

    def stop(self):
        self.stop_event.set()


# ------------------------------------------------------------ OBS частина ---

state = {
    "settings": None,
    "poller": None,
    "queue": queue.Queue(),
    "jars": [],
    "alert_until": 0.0,
    "status": "Не запущено",
}


def log(msg):
    if obs:
        obs.script_log(obs.LOG_INFO, f"[Mono Donate] {msg}")


def set_text(source_name, text):
    if not source_name:
        return
    source = obs.obs_get_source_by_name(source_name)
    if source is None:
        return
    settings = obs.obs_data_create()
    obs.obs_data_set_string(settings, "text", text)
    obs.obs_source_update(source, settings)
    obs.obs_data_release(settings)
    obs.obs_source_release(source)


def play_sound(source_name):
    if not source_name:
        return
    source = obs.obs_get_source_by_name(source_name)
    if source is not None:
        obs.obs_source_media_restart(source)
        obs.obs_source_release(source)


def show_alert(text):
    s = state["settings"]
    set_text(obs.obs_data_get_string(s, "alert_source"), text)
    play_sound(obs.obs_data_get_string(s, "sound_source"))
    duration = obs.obs_data_get_int(s, "duration") or 8
    state["alert_until"] = time.time() + duration


def tick():
    s = state["settings"]
    if s is None:
        return
    now = time.time()
    if state["alert_until"] and now >= state["alert_until"]:
        state["alert_until"] = 0.0
        set_text(obs.obs_data_get_string(s, "alert_source"), "")

    # Нового алерта не показуємо, поки триває поточний.
    while not state["alert_until"]:
        try:
            kind, payload = state["queue"].get_nowait()
        except queue.Empty:
            break
        if kind == "donation":
            template = obs.obs_data_get_string(s, "template") or DEFAULT_TEMPLATE
            text = build_alert_text(payload, template)
            log(f"Донат: {text!r}")
            show_alert(text)
        elif kind == "jar":
            update_goal(payload)
        elif kind == "error":
            log(f"Помилка: {payload}")


def update_goal(jar):
    s = state["settings"]
    source_name = obs.obs_data_get_string(s, "goal_source")
    if not source_name:
        return
    template = obs.obs_data_get_string(s, "goal_template") or DEFAULT_GOAL_TEMPLATE
    data = {
        "balance": format_money(jar.get("balance", 0)),
        "goal": format_money(jar.get("goal") or 0),
        "currency": CURRENCIES.get(jar.get("currencyCode"), "грн"),
        "title": jar.get("title", ""),
    }
    try:
        set_text(source_name, template.format(**data))
    except (KeyError, IndexError, ValueError):
        set_text(source_name, DEFAULT_GOAL_TEMPLATE.format(**data))


def stop_poller():
    if state["poller"]:
        state["poller"].stop()
        state["poller"] = None


def start_poller():
    s = state["settings"]
    token = obs.obs_data_get_string(s, "token")
    jar_id = obs.obs_data_get_string(s, "jar_id")
    enabled = obs.obs_data_get_bool(s, "enabled")
    min_amount = int(obs.obs_data_get_double(s, "min_amount") * 100)
    config = (token, jar_id, enabled, min_amount)
    if config == state.get("config") and state["poller"]:
        return  # нічого важливого не змінилось - не чіпаємо опитування
    state["config"] = config
    stop_poller()
    if not (token and jar_id and enabled):
        return
    state["poller"] = Poller(token, jar_id, min_amount, state["queue"])
    state["poller"].start()
    log("Опитування банки запущено")


def on_load_jars(props, prop):
    token = obs.obs_data_get_string(state["settings"], "token")
    if not token:
        log("Спершу вкажіть токен")
        return False
    try:
        state["jars"] = MonoApi(token).jars()
        log(f"Знайдено банок: {len(state['jars'])}")
    except Exception as e:
        log(f"Не вдалося отримати банки: {e}")
        return False
    fill_jar_list(obs.obs_properties_get(props, "jar_id"))
    return True


def fill_jar_list(jar_list):
    obs.obs_property_list_clear(jar_list)
    for jar in state["jars"]:
        title = jar.get("title") or jar.get("id")
        obs.obs_property_list_add_string(jar_list, title, jar.get("id"))
    current = obs.obs_data_get_string(state["settings"], "jar_id") if state["settings"] else ""
    if current and not any(j.get("id") == current for j in state["jars"]):
        obs.obs_property_list_add_string(jar_list, "(збережена банка)", current)


def on_test(props, prop):
    state["queue"].put(("donation", {
        "id": f"test-{time.time()}",
        "description": "Від: Тестовий глядач",
        "comment": "Слава Україні! Це тестовий донат",
        "amount": 10000,
        "currencyCode": 980,
    }))
    return False


def add_source_list(props, key, label, kinds):
    plist = obs.obs_properties_add_list(
        props, key, label, obs.OBS_COMBO_TYPE_EDITABLE, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(plist, "", "")
    sources = obs.obs_enum_sources()
    if sources:
        for source in sources:
            if obs.obs_source_get_unversioned_id(source) in kinds:
                name = obs.obs_source_get_name(source)
                obs.obs_property_list_add_string(plist, name, name)
        obs.source_list_release(sources)


TEXT_KINDS = ("text_gdiplus", "text_ft2_source")
MEDIA_KINDS = ("ffmpeg_source", "vlc_source")


# ---- стандартні функції OBS-скрипта ----

def script_description():
    return (
        "<b>Mono Donate</b><br>"
        "Донати через банку Monobank з алертом у сцені OBS — "
        "видно на всіх платформах мультистріму.<br>"
        "Токен: <a href='https://api.monobank.ua/'>api.monobank.ua</a>"
    )


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "enabled", True)
    obs.obs_data_set_default_int(settings, "duration", 8)
    obs.obs_data_set_default_double(settings, "min_amount", 1.0)
    obs.obs_data_set_default_string(settings, "template", DEFAULT_TEMPLATE)
    obs.obs_data_set_default_string(settings, "goal_template", DEFAULT_GOAL_TEMPLATE)


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_bool(props, "enabled", "Увімкнено")
    obs.obs_properties_add_text(props, "token", "Токен Monobank", obs.OBS_TEXT_PASSWORD)
    obs.obs_properties_add_button(props, "load_jars", "Завантажити банки", on_load_jars)
    jar_list = obs.obs_properties_add_list(
        props, "jar_id", "Банка", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    fill_jar_list(jar_list)

    add_source_list(props, "alert_source", "Текст алерту", TEXT_KINDS)
    add_source_list(props, "sound_source", "Звук (медіа-джерело)", MEDIA_KINDS)
    add_source_list(props, "goal_source", "Текст збору (необов'язково)", TEXT_KINDS)

    obs.obs_properties_add_text(props, "template", "Шаблон алерту", obs.OBS_TEXT_MULTILINE)
    obs.obs_properties_add_text(props, "goal_template", "Шаблон збору", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_int(props, "duration", "Тривалість алерту (с)", 2, 60, 1)
    obs.obs_properties_add_float(props, "min_amount", "Мінімальна сума", 0, 100000, 1)
    obs.obs_properties_add_button(props, "test", "Тестовий донат", on_test)
    return props


def script_update(settings):
    state["settings"] = settings
    start_poller()


def script_load(settings):
    state["settings"] = settings
    obs.timer_add(tick, TICK_MS)


def script_unload():
    obs.timer_remove(tick)
    stop_poller()
