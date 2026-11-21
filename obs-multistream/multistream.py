"""
Multistream - OBS-плагін (Python-скрипт) для стріму на кілька платформ одночасно.

Кожна платформа (Twitch, YouTube, Kick, TikTok, Facebook, власний RTMP)
отримує ОКРЕМИЙ потік зі своїм енкодером, тому бітрейт і роздільність
задаються для кожної платформи окремо. Бітрейт можна міняти просто під
час стріму - зміна застосовується без перезапуску трансляції.

Встановлення: OBS -> Інструменти -> Скрипти -> "+" -> вибрати цей файл.
"""

import time

try:
    import obspython as obs  # доступний лише всередині OBS
except ImportError:  # дозволяє імпортувати модуль для тестів поза OBS
    obs = None

TARGET_COUNT = 4
TICK_MS = 500
BITRATE_APPLY_DELAY = 1.0  # чекаємо, поки користувач допише число

PLATFORMS = {
    "twitch": ("Twitch", "rtmp://live.twitch.tv/app"),
    "youtube": ("YouTube", "rtmp://a.rtmp.youtube.com/live2"),
    "kick": ("Kick", ""),            # сервер індивідуальний, береться з панелі Kick
    "tiktok": ("TikTok", ""),        # сервер видає TikTok Live Studio / LIVE Center
    "facebook": ("Facebook", "rtmps://live-api-s.facebook.com:443/rtmp/"),
    "custom": ("Власний RTMP", ""),
}

DEFAULT_PLATFORMS = ["twitch", "youtube", "kick", "custom"]
DEFAULT_BITRATES = {"twitch": 6000, "youtube": 20000, "kick": 8000,
                    "tiktok": 6000, "facebook": 6000, "custom": 6000}

# (id енкодера в OBS, назва). Показуються лише ті, що є у вашій збірці OBS.
ENCODERS = [
    ("obs_x264", "x264 (процесор)"),
    ("obs_nvenc_h264_tex", "NVIDIA NVENC H.264"),
    ("jim_nvenc", "NVIDIA NVENC H.264 (старий OBS)"),
    ("ffmpeg_nvenc", "NVIDIA NVENC H.264 (FFmpeg)"),
    ("h264_texture_amf", "AMD AMF H.264"),
    ("obs_qsv11_v2", "Intel QuickSync H.264"),
    ("com.apple.videotoolbox.videoencoder.ave.avc", "Apple VideoToolbox H.264"),
]

DEFAULT_TWITCH_LIMIT = 6000  # рекомендований максимум Twitch
MAX_BITRATE = 51000          # YouTube рекомендує до 51 Мбіт/с для 4K

RESOLUTIONS = [("", "Як у налаштуваннях OBS"), ("1440", "1440p"), ("1080", "1080p"),
               ("936", "936p"), ("720", "720p"), ("480", "480p")]


# ---------------------------------------------------------------- логіка ---

def scaled_size(base_w, base_h, target_h):
    """Розмір під потрібну висоту зі збереженням пропорцій (кратний 2)."""
    if not target_h or target_h >= base_h:
        return base_w, base_h
    w = int(round(base_w * target_h / base_h))
    return w - (w % 2), target_h - (target_h % 2)


def video_encoder_settings(bitrate):
    """Налаштування, які розуміють усі H.264-енкодери OBS."""
    return {"rate_control": "CBR", "bitrate": int(bitrate), "keyint_sec": 2}


def limit_bitrate(platform, bitrate, twitch_limit):
    """Не даємо перевищити ліміт Twitch, інші платформи - без змін."""
    if platform == "twitch" and twitch_limit:
        return min(bitrate, twitch_limit)
    return bitrate


def k(i, name):
    """Ключ налаштування для i-ї платформи: t0_key, t1_bitrate ..."""
    return f"t{i}_{name}"


# ------------------------------------------------------------ OBS частина ---

def log(msg):
    if obs:
        obs.script_log(obs.LOG_INFO, f"[Multistream] {msg}")


def make_data(values):
    data = obs.obs_data_create()
    for key, value in values.items():
        if isinstance(value, bool):
            obs.obs_data_set_bool(data, key, value)
        elif isinstance(value, int):
            obs.obs_data_set_int(data, key, value)
        else:
            obs.obs_data_set_string(data, key, str(value))
    return data


class Target:
    """Один потік на одну платформу: сервіс + RTMP-вихід + власні енкодери."""

    def __init__(self, index):
        self.index = index
        self.output = None
        self.service = None
        self.venc = None
        self.aenc = None
        self.applied_bitrate = None
        self.pending_bitrate = None
        self.pending_since = 0.0

    # ---- налаштування з панелі скрипта ----
    def cfg(self, settings):
        i = self.index
        platform = obs.obs_data_get_string(settings, k(i, "platform")) or "custom"
        wanted = obs.obs_data_get_int(settings, k(i, "bitrate"))
        bitrate = limit_bitrate(platform, wanted,
                                obs.obs_data_get_int(settings, "twitch_limit"))
        return {
            "enabled": obs.obs_data_get_bool(settings, k(i, "enabled")),
            "platform": platform,
            "title": PLATFORMS.get(platform, PLATFORMS["custom"])[0],
            "server": obs.obs_data_get_string(settings, k(i, "server")).strip(),
            "key": obs.obs_data_get_string(settings, k(i, "key")).strip(),
            "encoder": obs.obs_data_get_string(settings, k(i, "encoder")) or "obs_x264",
            "bitrate": bitrate,
            "limited": bitrate != wanted,
            "audio_bitrate": obs.obs_data_get_int(settings, k(i, "audio_bitrate")) or 160,
            "height": obs.obs_data_get_string(settings, k(i, "height")),
            "auto": obs.obs_data_get_bool(settings, k(i, "auto")),
        }

    @property
    def active(self):
        return self.output is not None and obs.obs_output_active(self.output)

    def name(self, cfg):
        return f"multistream_{self.index}_{cfg['platform']}"

    # ---- старт / стоп ----
    def start(self, settings):
        cfg = self.cfg(settings)
        if self.active:
            log(f"{cfg['title']}: вже йде")
            return
        if not cfg["server"] or not cfg["key"]:
            log(f"{cfg['title']}: не вказано сервер або ключ потоку")
            return
        self.release()
        name = self.name(cfg)
        if cfg["limited"]:
            log(f"{cfg['title']}: бітрейт обмежено до {cfg['bitrate']} кбіт/с (ліміт Twitch)")

        service_data = make_data({"server": cfg["server"], "key": cfg["key"]})
        self.service = obs.obs_service_create("rtmp_custom", name + "_service", service_data, None)
        obs.obs_data_release(service_data)

        venc_data = make_data(video_encoder_settings(cfg["bitrate"]))
        self.venc = obs.obs_video_encoder_create(cfg["encoder"], name + "_video", venc_data, None)
        obs.obs_data_release(venc_data)
        if self.venc is None:
            log(f"{cfg['title']}: енкодер {cfg['encoder']} недоступний")
            self.release()
            return
        obs.obs_encoder_set_video(self.venc, obs.obs_get_video())
        if cfg["height"]:
            ovi = obs.obs_video_info()
            obs.obs_get_video_info(ovi)
            w, h = scaled_size(ovi.output_width, ovi.output_height, int(cfg["height"]))
            if (w, h) != (ovi.output_width, ovi.output_height):
                obs.obs_encoder_set_scaled_size(self.venc, w, h)

        aenc_data = make_data({"bitrate": cfg["audio_bitrate"]})
        self.aenc = obs.obs_audio_encoder_create("ffmpeg_aac", name + "_audio", aenc_data, 0, None)
        obs.obs_data_release(aenc_data)
        obs.obs_encoder_set_audio(self.aenc, obs.obs_get_audio())

        self.output = obs.obs_output_create("rtmp_output", name, None, None)
        obs.obs_output_set_video_encoder(self.output, self.venc)
        obs.obs_output_set_audio_encoder(self.output, self.aenc, 0)
        obs.obs_output_set_service(self.output, self.service)
        obs.obs_output_set_reconnect_settings(self.output, 20, 10)

        if obs.obs_output_start(self.output):
            self.applied_bitrate = cfg["bitrate"]
            log(f"{cfg['title']}: стрім запущено, {cfg['bitrate']} кбіт/с")
        else:
            err = obs.obs_output_get_last_error(self.output)
            log(f"{cfg['title']}: не вдалося запустити ({err or 'невідома помилка'})")
            self.release()

    def stop(self):
        if self.active:
            obs.obs_output_stop(self.output)
            log(f"Потік #{self.index + 1}: зупинено")

    def release(self):
        for attr, fn in (("output", obs.obs_output_release),
                         ("venc", obs.obs_encoder_release),
                         ("aenc", obs.obs_encoder_release),
                         ("service", obs.obs_service_release)):
            obj = getattr(self, attr)
            if obj is not None:
                fn(obj)
                setattr(self, attr, None)
        self.applied_bitrate = None
        self.pending_bitrate = None

    # ---- зміна бітрейту на льоту ----
    def request_bitrate(self, bitrate):
        if not self.active or bitrate == self.applied_bitrate:
            self.pending_bitrate = None
            return
        if bitrate != self.pending_bitrate:
            self.pending_bitrate = bitrate
            self.pending_since = time.time()

    def apply_pending_bitrate(self, title):
        if self.pending_bitrate is None:
            return
        if time.time() - self.pending_since < BITRATE_APPLY_DELAY:
            return
        bitrate = self.pending_bitrate
        self.pending_bitrate = None
        if not self.active:
            return
        data = make_data({"bitrate": bitrate})
        obs.obs_encoder_update(self.venc, data)
        obs.obs_data_release(data)
        log(f"{title}: бітрейт змінено {self.applied_bitrate} -> {bitrate} кбіт/с")
        self.applied_bitrate = bitrate


state = {"settings": None, "targets": [Target(i) for i in range(TARGET_COUNT)]}


def each_target(only_auto=False):
    s = state["settings"]
    for t in state["targets"]:
        cfg = t.cfg(s)
        if cfg["enabled"] and (cfg["auto"] or not only_auto):
            yield t


def start_all(only_auto=False):
    for t in each_target(only_auto):
        t.start(state["settings"])


def stop_all():
    for t in state["targets"]:
        t.stop()


def tick():
    s = state["settings"]
    if s is None:
        return
    for t in state["targets"]:
        if t.pending_bitrate is not None:
            t.apply_pending_bitrate(t.cfg(s)["title"])


def on_frontend_event(event):
    # Стартуємо/зупиняємось разом із кнопкою OBS "Почати трансляцію".
    if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTED:
        start_all(only_auto=True)
    elif event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED:
        for t in each_target(only_auto=True):
            t.stop()
    elif event == obs.OBS_FRONTEND_EVENT_EXIT:
        stop_all()


# ---- кнопки ----

def on_start_all(props, prop):
    start_all()
    return False


def on_stop_all(props, prop):
    stop_all()
    return False


def make_target_button(i, action):
    def handler(props, prop):
        t = state["targets"][i]
        if action == "start":
            t.start(state["settings"])
        else:
            t.stop()
        return False
    return handler


def make_platform_changed(i):
    def handler(props, prop, settings):
        platform = obs.obs_data_get_string(settings, k(i, "platform"))
        server = obs.obs_data_get_string(settings, k(i, "server"))
        known = {url for _, url in PLATFORMS.values() if url}
        if platform in PLATFORMS and (not server or server in known):
            obs.obs_data_set_string(settings, k(i, "server"), PLATFORMS[platform][1])
        return True
    return handler


def available_encoders():
    result = [(eid, title) for eid, title in ENCODERS if obs.obs_get_encoder_codec(eid)]
    return result or ENCODERS[:1]


# ---- стандартні функції OBS-скрипта ----

def script_description():
    return (
        "<b>Multistream</b><br>"
        "Стрім на кілька платформ одночасно, окремий бітрейт для кожної.<br>"
        "Бітрейт можна міняти під час стріму - зміна застосується за ~1 с."
    )


def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "twitch_limit", DEFAULT_TWITCH_LIMIT)
    for i, platform in enumerate(DEFAULT_PLATFORMS):
        obs.obs_data_set_default_string(settings, k(i, "platform"), platform)
        obs.obs_data_set_default_string(settings, k(i, "server"), PLATFORMS[platform][1])
        obs.obs_data_set_default_string(settings, k(i, "encoder"), "obs_x264")
        obs.obs_data_set_default_int(settings, k(i, "bitrate"), DEFAULT_BITRATES[platform])
        obs.obs_data_set_default_int(settings, k(i, "audio_bitrate"), 160)
        obs.obs_data_set_default_bool(settings, k(i, "auto"), True)
        if platform == "twitch":  # Twitch зазвичай приймає максимум 1080p
            obs.obs_data_set_default_string(settings, k(i, "height"), "1080")


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_button(props, "start_all", "▶ Запустити всі", on_start_all)
    obs.obs_properties_add_button(props, "stop_all", "■ Зупинити всі", on_stop_all)
    obs.obs_properties_add_int(props, "twitch_limit",
                               "Ліміт бітрейту Twitch (кбіт/с)", 1000, 20000, 500)
    encoders = available_encoders()

    for i in range(TARGET_COUNT):
        group = obs.obs_properties_create()

        p = obs.obs_properties_add_list(group, k(i, "platform"), "Платформа",
                                        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
        for pid, (title, _) in PLATFORMS.items():
            obs.obs_property_list_add_string(p, title, pid)
        obs.obs_property_set_modified_callback(p, make_platform_changed(i))

        obs.obs_properties_add_text(group, k(i, "server"), "Сервер", obs.OBS_TEXT_DEFAULT)
        obs.obs_properties_add_text(group, k(i, "key"), "Ключ потоку", obs.OBS_TEXT_PASSWORD)

        enc = obs.obs_properties_add_list(group, k(i, "encoder"), "Енкодер",
                                          obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
        for eid, title in encoders:
            obs.obs_property_list_add_string(enc, title, eid)

        obs.obs_properties_add_int_slider(group, k(i, "bitrate"),
                                          "Бітрейт відео (кбіт/с)", 500, MAX_BITRATE, 100)
        obs.obs_properties_add_int(group, k(i, "audio_bitrate"),
                                   "Бітрейт аудіо (кбіт/с)", 64, 320, 32)

        res = obs.obs_properties_add_list(group, k(i, "height"), "Роздільність",
                                          obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
        for value, title in RESOLUTIONS:
            obs.obs_property_list_add_string(res, title, value)

        obs.obs_properties_add_bool(group, k(i, "auto"),
                                    "Старт разом з «Почати трансляцію»")
        obs.obs_properties_add_button(group, k(i, "start"), "▶ Старт",
                                      make_target_button(i, "start"))
        obs.obs_properties_add_button(group, k(i, "stop"), "■ Стоп",
                                      make_target_button(i, "stop"))

        obs.obs_properties_add_group(props, k(i, "enabled"), f"Потік #{i + 1}",
                                     obs.OBS_GROUP_CHECKABLE, group)
    return props


def script_update(settings):
    state["settings"] = settings
    for t in state["targets"]:
        t.request_bitrate(t.cfg(settings)["bitrate"])


def script_load(settings):
    state["settings"] = settings
    obs.timer_add(tick, TICK_MS)
    obs.obs_frontend_add_event_callback(on_frontend_event)


def script_unload():
    obs.timer_remove(tick)
    for t in state["targets"]:
        t.stop()
        t.release()
