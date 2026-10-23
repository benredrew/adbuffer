-- adbuffer: ad-softening player (~/adbuffer).
-- SUPER+CTRL+Y opens the app. SUPER+ALT+Y in a Brave tab copies the tab's URL
-- (Ctrl+L, Ctrl+C) and plays it through adbuffer; elsewhere it just opens the app.
o.bind("SUPER + CTRL + Y", "adbuffer", { launch = "adbuffer-app" })

local function adbuffer_passthrough()
  local w = hl.get_active_window()
  local class = (w and w.class or ""):lower()
  if not class:find("brave") or class:find("127.0.0.1", 1, true) then
    hl.exec_cmd("adbuffer-app")
    return
  end
  local function tap(mods, key, at)
    hl.timer(function() hl.dispatch(hl.dsp.send_key_state({ mods = mods, key = key, state = "down" })) end,
      { timeout = at, type = "oneshot" })
    hl.timer(function() hl.dispatch(hl.dsp.send_key_state({ mods = mods, key = key, state = "up" })) end,
      { timeout = at + 50, type = "oneshot" })
  end
  tap("CTRL", "L", 10)
  tap("CTRL", "C", 150)
  hl.timer(function() hl.exec_cmd("adbuffer-passthrough") end, { timeout = 400, type = "oneshot" })
end
o.bind("SUPER + ALT + Y", "adbuffer: play this Brave tab with ads softened", adbuffer_passthrough)
