--[[
  Автонарізка шортсів — логіка кнопки в DaVinci Resolve (Workspace → Scripts → Utility).

  Як працює кнопка «Shorts Autocut»:
    1) Якщо обробки ще не було (або попередній результат уже імпортовано) — відкриває вікно
       запуску (Python): вибір запису, профіль гри, текст заклику. Важка обробка йде окремим
       процесом, DaVinci при цьому можна користуватися.
    2) Якщо обробка ще триває — показує, на якому вона етапі.
    3) Якщо обробка завершилась — збирає в поточному проєкті:
         • таймлайн «<запис> — моменти» з кольоровими маркерами всіх моментів;
         • на кожен шортс окремий таймлайн 1080×1920, 60 fps:
             V1 — гра, V2 — вебкамера, V3 — лічильники (Zoom/Position/Crop у Inspector),
             V4 — титр заклику (Text+), A1 — звук, доріжка субтитрів (SRT).
  Кнопка «Shorts Reimport Last» — зібрати останній результат ще раз.

  Лише безкоштовні функції Resolve. Усі дії записуються в work\logs\resolve_import.log.
]]

local M = {}
local unpack = unpack or table.unpack

local TOOL = nil
local LOGF = nil
local warnings = {}

-- ------------------------------------------------------------------ дрібниці

local function log(msg)
  msg = tostring(msg)
  print("[Шортси] " .. msg)
  if LOGF then
    local f = io.open(LOGF, "a")
    if f then
      f:write(os.date("%Y-%m-%d %H:%M:%S ") .. msg .. "\n")
      f:close()
    end
  end
end

local function warn(msg)
  table.insert(warnings, msg)
  log("[УВАГА] " .. msg)
end

local function file_exists(p)
  local f = io.open(p, "rb")
  if f then f:close() return true end
  return false
end

local function write_file(p, s)
  local f = io.open(p, "wb")
  if not f then return false end
  f:write(s)
  f:close()
  return true
end

local function read_lua(p)
  if not file_exists(p) then return nil end
  local ok, res = pcall(dofile, p)
  if ok then return res end
  log("Не вдалося прочитати " .. p .. ": " .. tostring(res))
  return nil
end

local function json_str(s)
  s = tostring(s):gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "")
  return '"' .. s .. '"'
end

local function round(x) return math.floor(x + 0.5) end
local function frames(t, fps) return round((t or 0) * fps) end

local function tc_from_frame(frame, fps)
  local ifps = round(fps)
  local f = frame % ifps
  local s = math.floor(frame / ifps)
  return string.format("%02d:%02d:%02d:%02d", math.floor(s / 3600), math.floor(s / 60) % 60, s % 60, f)
end

local function pyw()
  local p = TOOL .. "\\venv\\Scripts\\pythonw.exe"
  if file_exists(p) then return p end
  return TOOL .. "\\venv\\Scripts\\python.exe"
end

local function launch_python(script, arg)
  local cmd = 'start "" "' .. pyw() .. '" "' .. TOOL .. '\\tools\\' .. script .. '"'
  if arg then cmd = cmd .. ' "' .. arg .. '"' end
  log("Запуск: " .. cmd)
  local ok, err = pcall(os.execute, cmd)
  if not ok then
    log("os.execute недоступний: " .. tostring(err))
    return false
  end
  return true
end

-- Повідомлення користувачу: вікно DaVinci (якщо доступне) або маленьке вікно Python.
local function notify(msg)
  log(msg)
  local shown = pcall(function()
    local ui = fu.UIManager
    local disp = bmd.UIDispatcher(ui)
    local win = disp:AddWindow({ ID = "ShortsMsg", WindowTitle = "Автонарізка шортсів", Geometry = { 300, 300, 560, 220 } },
      ui:VGroup{
        ui:Label{ ID = "Txt", Text = msg, WordWrap = true, Alignment = { AlignHCenter = true, AlignVCenter = true } },
        ui:Button{ ID = "OK", Text = "OK" },
      })
    function win.On.OK.Clicked(ev) disp:ExitLoop() end
    function win.On.ShortsMsg.Close(ev) disp:ExitLoop() end
    win:Show()
    disp:RunLoop()
    win:Hide()
  end)
  if not shown then
    local p = TOOL .. "\\work\\jobs\\message.txt"
    if write_file(p, msg) then launch_python("msgbox.py", p) end
  end
end

-- ------------------------------------------------------------------ Resolve

local function get_resolve()
  local r = nil
  if Resolve then pcall(function() r = Resolve() end) end
  if not r and bmd and bmd.scriptapp then pcall(function() r = bmd.scriptapp("Resolve") end) end
  if not r and resolve then r = resolve end
  return r
end

local function selected_clip_paths(mp)
  local out = {}
  pcall(function()
    local sel = mp:GetSelectedClips()
    if sel then
      for _, clip in pairs(sel) do
        local p = clip:GetClipProperty("File Path")
        if p and p ~= "" then table.insert(out, p) end
      end
    end
  end)
  return out
end

local function import_media(mp, path)
  local items = mp:ImportMedia({ path })
  if items and items[1] then return items[1] end
  return nil
end

local function append_one(mp, info)
  local ok, res = pcall(function() return mp:AppendToTimeline({ info }) end)
  if ok and res and res[1] then return res[1] end
  return nil
end

local function unique_timeline(mp, project, name)
  local tl = mp:CreateEmptyTimeline(name)
  local n = 2
  while not tl and n < 50 do
    tl = mp:CreateEmptyTimeline(name .. " (" .. n .. ")")
    n = n + 1
  end
  if tl then project:SetCurrentTimeline(tl) end
  return tl
end

-- Як Resolve вписує кадр джерела в таймлайн (налаштування «Mismatched resolution files»).
local function mismatch_mode(tl, project, cfg)
  local forced = cfg and cfg.mismatch_mode or "auto"
  if forced ~= "auto" then return forced end
  local v = nil
  pcall(function() v = tl:GetSetting("timelineInputResMismatchBehavior") end)
  if (not v or v == "") and project then pcall(function() v = project:GetSetting("timelineInputResMismatchBehavior") end) end
  v = tostring(v or ""):lower()
  log("Налаштування невідповідності роздільності: '" .. v .. "'")
  if v:find("fit") then return "fit" end
  if v:find("center") then return "none" end
  if v:find("stretch") then return "stretch" end
  if v:find("crop") or v:find("fill") then return "fill" end
  warn("Не вдалося визначити режим вписування кадру ('" .. v .. "') — вважаю 'fit'. Якщо рамки не там, " ..
       "вкажіть resolve.mismatch_mode у settings.json (fit / fill / none).")
  return "fit"
end

-- Шар (src → dst) → властивості Inspector. Та сама математика, що в shorts/layout.py:resolve_params.
local function layer_props(layer, tlw, tlh, mode, cfg)
  local sw, sh = layer.src_w, layer.src_h
  local src, dst = layer.src, layer.dst   -- {x, y, w, h}
  local bx, by
  if mode == "fill" then
    bx = math.max(tlw / sw, tlh / sh); by = bx
  elseif mode == "none" then
    bx = 1.0; by = 1.0
  elseif mode == "stretch" then
    bx = tlw / sw; by = tlh / sh
  else
    bx = math.min(tlw / sw, tlh / sh); by = bx
  end
  local fw, fh = sw * bx, sh * by
  local zx = (dst[3] / src[3]) / bx
  local zy = (dst[4] / src[4]) / by
  local du = (src[1] + src[3] / 2) * bx - fw / 2
  local dv = (src[2] + src[4] / 2) * by - fh / 2
  local Xc = dst[1] + dst[3] / 2
  local Yc = dst[2] + dst[4] / 2
  local pan = Xc - tlw / 2 - zx * du
  local tilt_down = Yc - tlh / 2 - zy * dv
  local tilt = (cfg.tilt_up_positive == false) and tilt_down or -tilt_down
  local cl, cr, ct, cb = src[1] * bx, (sw - src[1] - src[3]) * bx, src[2] * by, (sh - src[2] - src[4]) * by
  if cfg.crop_units == "source" then
    cl, cr, ct, cb = src[1], sw - src[1] - src[3], src[2], sh - src[2] - src[4]
  end
  return {
    ZoomX = zx, ZoomY = zy, Pan = pan, Tilt = tilt,
    CropLeft = cl, CropRight = cr, CropTop = ct, CropBottom = cb,
  }
end

local function set_props(ti, props, what)
  local order = { "ZoomX", "ZoomY", "Pan", "Tilt", "CropLeft", "CropRight", "CropTop", "CropBottom" }
  local failed = {}
  for _, k in ipairs(order) do
    local ok, res = pcall(function() return ti:SetProperty(k, props[k]) end)
    if not ok or res == false then table.insert(failed, k) end
  end
  local s = string.format("%s: Zoom %.4f, Pan %.1f, Tilt %.1f, Crop L%.1f R%.1f T%.1f B%.1f", what,
    props.ZoomX, props.Pan, props.Tilt, props.CropLeft, props.CropRight, props.CropTop, props.CropBottom)
  log("  " .. s)
  if #failed > 0 then warn(what .. ": не вдалося задати " .. table.concat(failed, ", ")) end
end

-- Зв'язати кліпи шматка (як ПКМ → Link Clips): край, переміщення й видалення — для всіх разом.
local function link_clips(tl, items)
  local ok, res = pcall(function() return tl:SetClipsLinked(items, true) end)
  if ok and res ~= false then return true end
  log("  SetClipsLinked не спрацював: " .. tostring(res))
  return false
end

local function add_marker(tl, frame, color, name, note, used)
  used = used or {}
  while used[frame] do frame = frame + 1 end
  used[frame] = true
  local ok = pcall(function() tl:AddMarker(frame, color, name, note or "", 1) end)
  return ok
end

-- ------------------------------------------------------------------ таймлайн маркерів

local function build_master(project, mp, item, rec, job)
  local tl = unique_timeline(mp, project, rec.master_name)
  if not tl then warn("Не вдалося створити таймлайн " .. rec.master_name) return end
  local ok = pcall(function() mp:AppendToTimeline({ item }) end)
  if not ok then warn("Не вдалося покласти запис на таймлайн маркерів") end
  local fps = tonumber(tl:GetSetting("timelineFrameRate")) or rec.fps
  local used, n = {}, 0
  for _, m in ipairs(rec.markers or {}) do
    if add_marker(tl, frames(m.t, fps), m.color, m.name, m.note, used) then n = n + 1 end
  end
  log("Таймлайн «" .. rec.master_name .. "»: маркерів " .. n .. " з " .. #(rec.markers or {}))
end

-- ------------------------------------------------------------------ шортс

local function set_text_plus(ti, text, style)
  local comp = ti:GetFusionCompByIndex(1)
  if not comp then return false end
  local tool = nil
  pcall(function()
    local list = comp:GetToolList(false, "TextPlus")
    if list then for _, t in pairs(list) do tool = t break end end
  end)
  if not tool then pcall(function() tool = comp:FindTool("Template") end) end
  if not tool then return false end
  tool:SetInput("StyledText", text)
  pcall(function() tool:SetInput("Font", "Arial") end)
  pcall(function() tool:SetInput("Style", "Bold") end)
  pcall(function() tool:SetInput("Size", (style.font_size or 70) / 1080 * 1.25) end)
  pcall(function() tool:SetInput("Center", { 0.5, 1 - (style.y or 1680) / 1920 }) end)
  -- контур (елемент 2) і тінь (елемент 3), як у субтитрів
  pcall(function()
    tool:SetInput("Enabled2", 1); tool:SetInput("Thickness2", 0.12)
    tool:SetInput("Red2", 0); tool:SetInput("Green2", 0); tool:SetInput("Blue2", 0)
  end)
  pcall(function()
    tool:SetInput("Enabled3", 1); tool:SetInput("Red3", 0); tool:SetInput("Green3", 0); tool:SetInput("Blue3", 0)
    tool:SetInput("Alpha3", 0.7); tool:SetInput("Softness3", 2)
  end)
  return true
end

local function insert_cta(tl, sh, start, tlfps, job)
  while tl:GetTrackCount("video") < 4 do tl:AddTrack("video") end
  local locked = {}
  for i = 1, 3 do pcall(function() tl:SetTrackLock("video", i, true); table.insert(locked, i) end) end
  pcall(function() tl:SetTrackLock("audio", 1, true) end)
  local ti = nil
  local ok, err = pcall(function()
    local cta_start = start + frames(sh.cta.start, tlfps)
    local video_end = start + frames(sh.duration, tlfps)
    tl:SetCurrentTimecode(tc_from_frame(cta_start, tlfps))
    ti = tl:InsertFusionTitleIntoTimeline("Text+")
    if ti and ti:GetEnd() > video_end + 1 then
      -- стандартна тривалість титру довша за залишок шортсу — ставимо так, щоб закінчувався з відео
      local d = ti:GetEnd() - ti:GetStart()
      tl:DeleteClips({ ti })
      tl:SetCurrentTimecode(tc_from_frame(math.max(start, video_end - d), tlfps))
      ti = tl:InsertFusionTitleIntoTimeline("Text+")
    end
  end)
  for _, i in ipairs(locked) do pcall(function() tl:SetTrackLock("video", i, false) end) end
  pcall(function() tl:SetTrackLock("audio", 1, false) end)
  if not ok or not ti then
    warn("Титр заклику (Text+) не вставився: " .. tostring(err or "без відповіді"))
    return false
  end
  if not set_text_plus(ti, sh.cta.text, job.cta_style or {}) then
    warn("Титр заклику вставлено, але текст не вдалося задати — впишіть його в Inspector.")
  end
  local tt = ""
  pcall(function() local a, b = ti:GetTrackTypeAndIndex(); tt = tostring(a) .. tostring(b) end)
  log(string.format("  Заклик: Text+ %d–%d %s", ti:GetStart(), ti:GetEnd(), tt))
  return true
end

local function add_srt(mp, tl, path, start, track_no)
  if not path or not file_exists(path) then return false end
  local item = import_media(mp, path)
  if not item then warn("Не вдалося імпортувати " .. path) return false end
  while tl:GetTrackCount("subtitle") < track_no do tl:AddTrack("subtitle") end
  local placed = append_one(mp, { mediaPoolItem = item, recordFrame = start, trackIndex = track_no })
  if not placed then
    pcall(function() mp:AppendToTimeline({ item }) end)
  end
  local items = {}
  pcall(function() items = tl:GetItemListInTrack("subtitle", track_no) or {} end)
  local first = items[1]
  if first and first:GetStart() == start then
    log("  Субтитри: " .. #items .. " фраз на доріжці ST" .. track_no)
    return true
  end
  if first then
    log("  Субтитри лягли не з початку (" .. tostring(first:GetStart()) .. " замість " .. start .. ") — прибираю")
    pcall(function() tl:DeleteClips(items) end)
  end
  warn("Субтитри не вдалося покласти на таймлайн автоматично — SRT лежить у медіапулі, перетягніть його на " ..
       "таймлайн «" .. tl:GetName() .. "» (у самий початок).")
  return false
end

local function build_short(project, mp, item, cam_item, rec, sh, job, tl_cfg)
  local tl = unique_timeline(mp, project, sh.name)
  if not tl then warn("Не вдалося створити таймлайн " .. sh.name) return false end
  local W, H, FPS = tl_cfg.width, tl_cfg.height, tl_cfg.fps
  local settings = {
    useCustomSettings = "1",
    timelineResolutionWidth = tostring(W), timelineResolutionHeight = tostring(H),
    timelineOutputResolutionWidth = tostring(W), timelineOutputResolutionHeight = tostring(H),
    timelineFrameRate = tostring(FPS),
  }
  for k, v in pairs(settings) do
    local ok, res = pcall(function() return tl:SetSetting(k, v) end)
    if not ok or res == false then log("  SetSetting " .. k .. "=" .. v .. " → не вдалося") end
  end
  local gw, gh = tl:GetSetting("timelineResolutionWidth"), tl:GetSetting("timelineResolutionHeight")
  local tlfps = tonumber(tl:GetSetting("timelineFrameRate")) or FPS
  log("Таймлайн «" .. sh.name .. "»: " .. tostring(gw) .. "×" .. tostring(gh) .. ", " .. tostring(tlfps) .. " fps")
  if tostring(gw) ~= tostring(W) or tostring(gh) ~= tostring(H) then
    warn("Таймлайн «" .. sh.name .. "» не став " .. W .. "×" .. H .. " — перевірте Timeline Settings.")
  end
  if math.abs(tlfps - FPS) > 0.01 then
    warn("Частота таймлайну " .. tostring(tlfps) .. " замість " .. FPS .. " (у Resolve вона задається до додавання кліпів).")
  end
  while tl:GetTrackCount("video") < 3 do tl:AddTrack("video") end
  local mode = mismatch_mode(tl, project, job.resolve or {})
  log("  Режим вписування кадру: " .. mode)

  local src_fps = tonumber(item:GetClipProperty("FPS")) or rec.fps
  local cam_fps = cam_item and (tonumber(cam_item:GetClipProperty("FPS")) or rec.webcam.fps) or src_fps
  local start = tl:GetStartFrame()
  local pos = start
  local linked_ok, linked_total = 0, 0
  for si, seg in ipairs(sh.segments) do
    local group = {}   -- кліпи цього шматка (V1–V3 + A1) — зв'яжемо в одну групу
    local sf = frames(seg.s, src_fps)
    local ef = frames(seg.e, src_fps) - 1
    -- довжина на таймлайні = рівно стільки кадрів, скільки взяли з джерела (без накладань і дірок)
    local len_tl = round((ef - sf + 1) * tlfps / src_fps)
    for _, layer in ipairs(sh.layers) do
      local use_cam = layer.source == "webcam_file" and cam_item ~= nil
      local src_item = use_cam and cam_item or item
      local a, b = sf, ef
      if use_cam then
        local off = rec.webcam.offset or 0
        a, b = frames(seg.s + off, cam_fps), frames(seg.e + off, cam_fps) - 1
      end
      local ti = append_one(mp, { mediaPoolItem = src_item, startFrame = a, endFrame = b,
                                  trackIndex = layer.track, recordFrame = pos, mediaType = 1 })
      if ti then
        table.insert(group, ti)
        if si == 1 then
          set_props(ti, layer_props(layer, W, H, mode, job.resolve or {}), "V" .. layer.track .. " " .. layer.name)
        else
          set_props(ti, layer_props(layer, W, H, mode, job.resolve or {}), "V" .. layer.track)
        end
        if ti:GetStart() ~= pos then
          log(string.format("  [!] V%d шматок %d ліг на %s замість %d", layer.track, si, tostring(ti:GetStart()), pos))
        end
      else
        warn("Не вдалося покласти шматок " .. si .. " на V" .. layer.track .. " у «" .. sh.name .. "»")
      end
    end
    local ai = append_one(mp, { mediaPoolItem = item, startFrame = sf, endFrame = ef, trackIndex = 1,
                                recordFrame = pos, mediaType = 2 })
    if not ai then warn("Не вдалося покласти звук шматка " .. si) end
    if ai then table.insert(group, ai) end
    if #group > 1 and not (job.resolve and job.resolve.link_clips == false) then
      linked_total = linked_total + 1
      if link_clips(tl, group) then linked_ok = linked_ok + 1 end
    end
    pos = pos + len_tl
  end

  if linked_total > 0 then
    if linked_ok == linked_total then
      log("  Зв'язано шматків: " .. linked_ok .. " (V1–V3 + A1 тягнуться разом)")
    else
      warn("Зв'язано лише " .. linked_ok .. " з " .. linked_total .. " шматків у «" .. sh.name ..
           "» — решту можна зв'язати вручну: виділити кліпи шматка → ПКМ → Link Clips.")
    end
  end
  if job.resolve and job.resolve.add_short_markers then
    local used = {}
    for _, m in ipairs(sh.markers or {}) do
      add_marker(tl, frames(m.t, tlfps), m.color, m.name, m.note, used)
    end
  end
  add_srt(mp, tl, sh.srt, start, 1)
  if sh.cta then
    if not insert_cta(tl, sh, start, tlfps, job) and sh.cta_srt then
      add_srt(mp, tl, sh.cta_srt, start, 2)
      warn("Заклик для «" .. sh.name .. "» покладено окремою доріжкою субтитрів ST2.")
    end
  end
  pcall(function() tl:SetCurrentTimecode(tc_from_frame(start, tlfps)) end)
  return true
end

-- ------------------------------------------------------------------ імпорт результату

local function import_job(status)
  warnings = {}
  local job = read_lua(status.result_file or "")
  if not job then
    notify("Не знайдено файл результату обробки:\n" .. tostring(status.result_file) ..
           "\nЗапустіть обробку ще раз.")
    return
  end
  local resolve_app = get_resolve()
  if not resolve_app then notify("Не вдалося підключитися до DaVinci Resolve.") return end
  local project = resolve_app:GetProjectManager():GetCurrentProject()
  if not project then
    notify("Відкрийте (або створіть) проєкт у DaVinci і натисніть кнопку ще раз.")
    return
  end
  local mp = project:GetMediaPool()
  pcall(function() resolve_app:OpenPage("edit") end)
  log("=== Імпорт " .. tostring(job.job_id) .. " у проєкт «" .. tostring(project:GetName()) .. "» ===")
  local folder = mp:AddSubFolder(mp:GetRootFolder(), job.folder_name)
  if folder then mp:SetCurrentFolder(folder) end

  local n_short, n_ok = 0, 0
  for _, rec in ipairs(job.recordings or {}) do
    local item = import_media(mp, rec.path)
    if not item then
      warn("Не вдалося імпортувати запис " .. rec.path)
    else
      local cam_item = nil
      if rec.webcam and rec.webcam.path then
        cam_item = import_media(mp, rec.webcam.path)
        if not cam_item then warn("Не вдалося імпортувати файл вебкамери " .. rec.webcam.path) end
      end
      build_master(project, mp, item, rec, job)
      for _, sh in ipairs(rec.shorts or {}) do
        n_short = n_short + 1
        local ok, err = pcall(build_short, project, mp, item, cam_item, rec, sh, job, job.timeline)
        if ok and err then n_ok = n_ok + 1
        else warn("Помилка при збиранні «" .. tostring(sh.name) .. "»: " .. tostring(err)) end
      end
    end
  end
  write_file(TOOL .. "\\work\\jobs\\" .. tostring(job.job_id) .. ".imported", os.date("%Y-%m-%d %H:%M:%S"))
  local msg = "Готово! Зібрано таймлайнів шортсів: " .. n_ok .. " з " .. n_short ..
              "\nПапка в медіапулі: «" .. tostring(job.folder_name) .. "»" ..
              "\nГотові MP4 і звіт: " .. tostring(job.job_dir)
  if #warnings > 0 then
    msg = msg .. "\n\nЗауважень: " .. #warnings .. " (перше: " .. warnings[1] .. ")\nУсі — у work\\logs\\resolve_import.log"
  end
  notify(msg)
end

-- ------------------------------------------------------------------ вхід

function M.main(tool_dir, mode)
  TOOL = tool_dir
  LOGF = TOOL .. "\\work\\logs\\resolve_import.log"
  local jobs = TOOL .. "\\work\\jobs\\"
  local status = read_lua(jobs .. "latest.lua")

  if mode == "reimport" then
    if status and status.result_file then import_job(status)
    else notify("Ще немає жодного готового результату обробки.") end
    return
  end

  if status and status.status == "running" and (os.time() - (status.heartbeat or 0)) < 90 then
    notify("Обробка ще триває: " .. tostring(status.stage or "") .. " (" .. tostring(status.percent or 0) ..
           " %).\nВікно з прогресом — на панелі завдань. Коли буде готово, натисніть кнопку ще раз.")
    return
  end
  if status and status.status == "done" and not file_exists(jobs .. tostring(status.job_id) .. ".imported") then
    import_job(status)
    return
  end

  -- Новий запуск: передаємо вибрані в медіапулі кліпи як запис за замовчуванням.
  local resolve_app = get_resolve()
  if resolve_app then
    pcall(function()
      local project = resolve_app:GetProjectManager():GetCurrentProject()
      if project then
        local paths = selected_clip_paths(project:GetMediaPool())
        local parts = {}
        for _, p in ipairs(paths) do table.insert(parts, json_str(p)) end
        write_file(jobs .. "request.json", '{"selected": [' .. table.concat(parts, ", ") .. ']}')
      end
    end)
  end
  if not launch_python("run_job.py") then
    notify("Не вдалося відкрити вікно запуску. Запустіть вручну файл run_job.bat у папці " .. TOOL)
  end
end

return M
