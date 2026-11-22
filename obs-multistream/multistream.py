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
    # AV1 / HEVC: краща якість при тому ж бітрейті, але приймає лише YouTube.
    ("obs_nvenc_av1_tex", "NVIDIA NVENC AV1 (лише YouTube)"),
    ("obs_nvenc_hevc_tex", "NVIDIA NVENC HEVC (лише YouTube)"),
    ("av1_texture_amf", "AMD AMF AV1 (лише YouTube)"),
    ("obs_qsv11_av1", "Intel QuickSync AV1 (лише YouTube)"),
]

# Платформи, що через RTMP приймають лише H.264.
H264_ONLY_PLATFORMS = {"twitch", "kick", "tiktok", "facebook"}

DEFAULT_TWITCH_LIMIT = 6000  # рекомендований максимум Twitch
MAX_BITRATE = 51000          # YouTube рекомендує до 51 Мбіт/с для 4K

MAIN_ENCODER = "main"  # спільний енкодер з основною кнопкою OBS "Почати трансляцію"

FPS_OPTIONS = [("1", "Як в OBS"), ("2", "Половина (60 -> 30)")]

STOP_REASONS = {
    -1: "неправильний сервер",
    -2: "не вдалося підключитися до сервера",
    -3: "сервер відхилив потік (перевірте ключ)",
    -4: "помилка виводу",
    -5: "з'єднання розірвано",
    -6: "сервер не підтримує цей формат",
    -8: "помилка енкодера",
}

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


def encoder_allowed(platform, codec):
    """AV1/HEVC не можна слати на платформи, що приймають лише H.264."""
    return not (platform in H264_ONLY_PLATFORMS and codec and codec != "h264")


def service_type(server):
    """WHIP (WebRTC) або звичайний RTMP/RTMPS."""
    return "whip_custom" if "whip" in server.lower() else "rtmp_custom"


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
        self.shared = False  # енкодери взяті в основного потоку OBS
        self.signals = []
        self.title = f"Потік #{index + 1}"
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
            "fps_div": int(obs.obs_data_get_string(settings, k(i, "fps_div")) or 1),
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

        self.title = cfg["title"]

        stype = service_type(cfg["server"])
        key_field = "bearer_token" if stype == "whip_custom" else "key"
        service_data = make_data({"server": cfg["server"], key_field: cfg["key"]})
        self.service = obs.obs_service_create(stype, name + "_service", service_data, None)
        obs.obs_data_release(service_data)

        ok = (self.use_main_encoders(cfg) if cfg["encoder"] == MAIN_ENCODER
              else self.create_encoders(cfg, name))
        if not ok:
            self.release()
            return

        out_type = obs.obs_service_get_preferred_output_type(self.service) or "rtmp_output"
        self.output = obs.obs_output_create(out_type, name, None, None)
        obs.obs_output_set_video_encoder(self.output, self.venc)
        obs.obs_output_set_audio_encoder(self.output, self.aenc, 0)
        obs.obs_output_set_service(self.output, self.service)
        obs.obs_output_set_reconnect_settings(self.output, 20, 10)
        apply_profile_delay(self.output)
        self.connect_signals()

        if obs.obs_output_start(self.output):
            self.applied_bitrate = None if self.shared else cfg["bitrate"]
            rate = "енкодер основного потоку" if self.shared else f"{cfg['bitrate']} кбіт/с"
            log(f"{cfg['title']}: стрім запускається, {rate}")
        else:
            err = obs.obs_output_get_last_error(self.output)
            log(f"{cfg['title']}: не вдалося запустити ({err or 'невідома помилка'})")
            self.release()

    def create_encoders(self, cfg, name):
        codec = obs.obs_get_encoder_codec(cfg["encoder"])
        if not encoder_allowed(cfg["platform"], codec):
            log(f"{cfg['title']}: приймає лише H.264, а вибрано {codec}. "
                f"Виберіть H.264-енкодер (наприклад, NVENC H.264)")
            return False
        venc_data = make_data(video_encoder_settings(cfg["bitrate"]))
        self.venc = obs.obs_video_encoder_create(cfg["encoder"], name + "_video", venc_data, None)
        obs.obs_data_release(venc_data)
        if self.venc is None:
            log(f"{cfg['title']}: енкодер {cfg['encoder']} недоступний")
            return False
        obs.obs_encoder_set_video(self.venc, obs.obs_get_video())
        if cfg["height"]:
            ovi = obs.obs_video_info()
            obs.obs_get_video_info(ovi)
            w, h = scaled_size(ovi.output_width, ovi.output_height, int(cfg["height"]))
            if (w, h) != (ovi.output_width, ovi.output_height):
                obs.obs_encoder_set_scaled_size(self.venc, w, h)
                optional_call("obs_encoder_set_gpu_scale_type", self.venc,
                              getattr(obs, "OBS_SCALE_BICUBIC", 2))
        if cfg["fps_div"] > 1:
            optional_call("obs_encoder_set_frame_rate_divisor", self.venc, cfg["fps_div"])

        aenc_data = make_data({"bitrate": cfg["audio_bitrate"]})
        self.aenc = obs.obs_audio_encoder_create("ffmpeg_aac", name + "_audio", aenc_data, 0, None)
        obs.obs_data_release(aenc_data)
        obs.obs_encoder_set_audio(self.aenc, obs.obs_get_audio())
        self.shared = False
        return True

    def use_main_encoders(self, cfg):
        """Ділимо енкодер з основним потоком OBS: нуль додаткового навантаження,
        але бітрейт і роздільність такі самі, як в основному потоці."""
        main = obs.obs_frontend_get_streaming_output()
        try:
            if main is None or not obs.obs_output_active(main):
                log(f"{cfg['title']}: спершу запустіть основну трансляцію OBS")
                return False
            # Енкодерами володіє основний потік - ми їх лише позичаємо
            # і не відпускаємо в release().
            self.venc = obs.obs_output_get_video_encoder(main)
            self.aenc = obs.obs_output_get_audio_encoder(main, 0)
        finally:
            if main is not None:
                obs.obs_output_release(main)
        self.shared = True
        return self.venc is not None and self.aenc is not None

    # ---- події виходу: статус і причини обриву ----
    def connect_signals(self):
        handler = obs.obs_output_get_signal_handler(self.output)

        def on_start(cd):
            log(f"{self.title}: в ефірі")

        def on_reconnect(cd):
            log(f"{self.title}: зв'язок втрачено, перепідключення...")

        def on_reconnected(cd):
            log(f"{self.title}: перепідключено")

        def on_stop(cd):
            code = obs.calldata_int(cd, "code")
            if code:
                reason = STOP_REASONS.get(code, f"код {code}")
                log(f"{self.title}: зупинено - {reason}")
            else:
                log(f"{self.title}: зупинено")

        self.signals = [("start", on_start), ("reconnect", on_reconnect),
                        ("reconnect_success", on_reconnected), ("stop", on_stop)]
        for sig, cb in self.signals:
            obs.signal_handler_connect(handler, sig, cb)

    def disconnect_signals(self):
        if self.output is not None and self.signals:
            handler = obs.obs_output_get_signal_handler(self.output)
            for sig, cb in self.signals:
                obs.signal_handler_disconnect(handler, sig, cb)
        self.signals = []

    def stop(self):
        if self.active:
            obs.obs_output_stop(self.output)

    def release(self):
        self.disconnect_signals()
        if self.shared:
            self.venc = self.aenc = None
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
        self.shared = False

    # ---- зміна бітрейту на льоту ----
    def request_bitrate(self, bitrate):
        if not self.active or self.shared or bitrate == self.applied_bitrate:
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


def optional_call(fn_name, *args):
    """Функції, яких немає в старих версіях OBS, просто пропускаємо."""
    fn = getattr(obs, fn_name, None)
    if fn is not None:
        fn(*args)


def apply_profile_delay(output):
    """Затримка трансляції з налаштувань OBS (Розширені -> Затримка потоку),
    щоб усі платформи мали однакову затримку з основним потоком."""
    try:
        config = obs.obs_frontend_get_profile_config()
        if config is None or not obs.config_get_bool(config, "Output", "DelayEnable"):
            return
        sec = obs.config_get_int(config, "Output", "DelaySec")
        preserve = obs.config_get_bool(config, "Output", "DelayPreserve")
        flags = getattr(obs, "OBS_OUTPUT_DELAY_PRESERVE", 1) if preserve else 0
        obs.obs_output_set_delay(output, sec, flags)
    except AttributeError:
        pass  # стара збірка obspython без config_get_*


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
    elif event in (obs.OBS_FRONTEND_EVENT_EXIT, obs.OBS_FRONTEND_EVENT_PROFILE_CHANGED):
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
    return (result or ENCODERS[:1]) + [(MAIN_ENCODER, "Як основний потік OBS (без навантаження)")]


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
        obs.obs_data_set_default_string(settings, k(i, "fps_div"), "1")
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

        fps = obs.obs_properties_add_list(group, k(i, "fps_div"), "Кадри/с",
                                          obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
        for value, title in FPS_OPTIONS:
            obs.obs_property_list_add_string(fps, title, value)

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
