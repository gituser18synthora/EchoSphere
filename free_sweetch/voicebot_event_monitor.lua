-- Handle mod_audio_fork control events while voicebot.lua supplies one
-- uninterrupted media clock with silence_stream://-1.

local argv = argv or {}
local uuid = tostring(argv[1] or "")
if not uuid:match("^[%w%-]+$") then
  freeswitch.consoleLog("err", "voicebot monitor: invalid call uuid\n")
  return
end

local api = freeswitch.API()
local transfer_events = freeswitch.EventConsumer(
  "CUSTOM", "mod_audio_fork::transfer"
)
local disconnect_events = freeswitch.EventConsumer(
  "CUSTOM", "mod_audio_fork::disconnect"
)
local error_events = freeswitch.EventConsumer(
  "CUSTOM", "mod_audio_fork::error"
)
local connect_failed_events = freeswitch.EventConsumer(
  "CUSTOM", "mod_audio_fork::connect_failed"
)
local hangup_events = freeswitch.EventConsumer("CHANNEL_HANGUP_COMPLETE")

local function trim(value)
  return tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function channel_variable(name, fallback)
  local value = trim(api:execute("uuid_getvar", uuid .. " " .. name))
  if value == "" or value == "_undef_" then
    return fallback or ""
  end
  return value
end

-- Custom events fan out to every monitor, so discard events for other calls.
local function pop_matching(consumer)
  local event = consumer:pop(0)
  while event do
    local event_uuid = event:getHeader("Unique-ID")
      or event:getHeader("Channel-Unique-ID")
      or ""
    if event_uuid == uuid then
      return event
    end
    event = consumer:pop(0)
  end
  return nil
end

local function fire_transfer_event(status, destination, detail)
  local event = freeswitch.Event("CUSTOM", "echosphere::transfer")
  event:addHeader("Unique-ID", uuid)
  event:addHeader("Transfer-Status", status)
  event:addHeader("Transfer-Destination", destination or "")
  if detail and detail ~= "" then
    event:addHeader("Transfer-Detail", detail:sub(1, 200))
  end
  event:fire()
end

local function stop_fork()
  pcall(function()
    api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
  end)
end

local function stop_media_clock()
  api:execute("uuid_setvar", uuid .. " voicebot_monitor_stop true")
end

local function handle_transfer_request(event)
  local destination = channel_variable("voicebot_transfer_destination")
  local dialplan = channel_variable("voicebot_transfer_dialplan", "XML")
  local context = channel_variable("voicebot_transfer_context", "public")

  if destination == "" or not destination:match("^[%w_+%.%-]+$") then
    freeswitch.consoleLog(
      "warning",
      "voicebot monitor: transfer requested but destination is missing or unsafe"
        .. " uuid=" .. uuid .. "\n"
    )
    fire_transfer_event("failed", "", "transfer destination not configured")
    return false
  end
  if not dialplan:match("^[%w_-]+$") then
    dialplan = "XML"
  end
  if not context:match("^[%w_-]+$") then
    context = "public"
  end

  local event_body = event:getBody() or "{}"
  freeswitch.consoleLog(
    "info",
    "voicebot monitor: EchoSphere transfer event call_uuid=" .. uuid
      .. " data=" .. event_body:sub(1, 500) .. "\n"
  )
  stop_media_clock()
  stop_fork()
  fire_transfer_event("initiated", destination)
  local result = api:execute(
    "uuid_transfer",
    uuid .. " " .. destination .. " " .. dialplan .. " " .. context
  ) or ""
  local flat_result = result:gsub("[\r\n]+", " "):sub(1, 300)
  freeswitch.consoleLog(
    "info", "voicebot monitor: uuid_transfer result=" .. flat_result .. "\n"
  )
  if not result:match("^%+OK") then
    fire_transfer_event("failed", destination, flat_result:sub(1, 120))
    api:execute("uuid_kill", uuid .. " NORMAL_TEMPORARY_FAILURE")
  end
  return true
end

while true do
  local transfer_event = pop_matching(transfer_events)
  if transfer_event and handle_transfer_request(transfer_event) then
    return
  end

  if pop_matching(disconnect_events) then
    freeswitch.consoleLog(
      "info",
      "voicebot monitor: EchoSphere media socket closed uuid=" .. uuid .. "\n"
    )
    stop_media_clock()
    stop_fork()
    api:execute("uuid_kill", uuid .. " NORMAL_CLEARING")
    return
  end

  if pop_matching(error_events) or pop_matching(connect_failed_events) then
    freeswitch.consoleLog(
      "err", "voicebot monitor: EchoSphere media stream failed uuid=" .. uuid .. "\n"
    )
    stop_media_clock()
    stop_fork()
    api:execute("uuid_kill", uuid .. " NORMAL_TEMPORARY_FAILURE")
    return
  end

  if pop_matching(hangup_events) then
    return
  end
  freeswitch.msleep(20)
end
