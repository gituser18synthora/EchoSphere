-- Connect the current FreeSWITCH channel to EchoSphere through mod_audio_fork.

local session = session
if not session or not session:ready() then
  return
end

local function script_dir()
  local source = debug.getinfo(1, "S").source:gsub("^@", "")
  return source:match("(.*)[/\\]") or "/usr/local/freeswitch/scripts"
end

local function shell_quote(value)
  return "'" .. tostring(value or ""):gsub("'", "'\\''") .. "'"
end

local uuid = session:get_uuid()
local caller = session:getVariable("caller_id_number")
  or session:getVariable("effective_caller_id_number")
  or ""
local transfer_destination = session:getVariable("voicebot_transfer_destination") or ""
local transfer_dialplan = session:getVariable("voicebot_transfer_dialplan") or "XML"
local transfer_context = session:getVariable("voicebot_transfer_context") or "public"

-- Transfer targets are controlled by the FreeSWITCH dialplan, not by an
-- arbitrary WebSocket payload. EchoSphere only requests the handoff.
if transfer_destination ~= ""
  and not transfer_destination:match("^[%w_+%.%-]+$")
then
  freeswitch.consoleLog(
    "err",
    "voicebot: ignoring unsafe transfer destination\n"
  )
  transfer_destination = ""
end
if not transfer_dialplan:match("^[%w_-]+$") then
  transfer_dialplan = "XML"
end
if not transfer_context:match("^[%w_-]+$") then
  transfer_context = "public"
end

-- mod_audio_fork converts {"type":"transfer","data":{...}} into this
-- FreeSWITCH custom event. Each running script receives custom events, so the
-- Unique-ID check below is mandatory before acting on one.
local transfer_events = freeswitch.EventConsumer(
  "CUSTOM",
  "mod_audio_fork::transfer"
)
-- The media socket closing means the voice worker ENDED or LOST this call
-- (bot goodbye, fatal provider error, worker restart). Without watching for
-- it, the loop below keeps the caller on an endless silence stream. Builds
-- that do not emit these subclasses simply never deliver an event here —
-- EchoSphere's event-socket uuid_kill is the other half of this fix.
local disconnect_events = freeswitch.EventConsumer(
  "CUSTOM",
  "mod_audio_fork::disconnect"
)
local error_events = freeswitch.EventConsumer(
  "CUSTOM",
  "mod_audio_fork::error"
)
local connect_failed_events = freeswitch.EventConsumer(
  "CUSTOM",
  "mod_audio_fork::connect_failed"
)

-- EchoSphere's transfer monitor subscribes to CUSTOM echosphere::transfer
-- over the event socket: these are its deterministic initiated/failed
-- signals, keyed by Unique-ID like every core channel event.
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

-- Drain a consumer, returning the first event that belongs to THIS channel.
-- Custom events fan out to every running script, so foreign-uuid events are
-- discarded rather than acted on.
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

-- Outbound test/campaign calls originated by EchoSphere (Studio "Real
-- call", backend/routers/real_calls.py) carry the bot in the `voicebotID`
-- channel variable and the customer data as `vb_<key>` channel variables.
-- The helper forwards them as the webhook's `To`/`botId` and `variables`,
-- so the bot runs on the same call context a campaign call would carry.
-- Inbound calls without them keep the static-DID behaviour.
local function url_decode(value)
  -- uuid_dump encodes with %XX only (a '+' stays a literal plus sign).
  return (tostring(value or ""):gsub("%%(%x%x)", function(hex)
    return string.char(tonumber(hex, 16))
  end))
end

local function json_string(value)
  local escaped = tostring(value or ""):gsub('[%c"\\]', function(ch)
    if ch == '"' then return '\\"' end
    if ch == "\\" then return "\\\\" end
    if ch == "\n" then return "\\n" end
    if ch == "\r" then return "\\r" end
    if ch == "\t" then return "\\t" end
    return string.format("\\u%04x", ch:byte())
  end)
  return '"' .. escaped .. '"'
end

-- Channel variables cannot be enumerated from Lua; `uuid_dump` lists them
-- (values URL-encoded) as `variable_<name>: <value>` lines.
local function collect_vb_variables()
  local dump = freeswitch.API():execute("uuid_dump", uuid) or ""
  local parts = {}
  for name, value in dump:gmatch("\nvariable_vb_([%w_%.%-]+): ([^\n]*)") do
    parts[#parts + 1] = json_string(name) .. ":" .. json_string(url_decode(value))
  end
  if #parts == 0 then
    return ""
  end
  return "{" .. table.concat(parts, ",") .. "}"
end

local bot_id = session:getVariable("voicebotID") or ""
if bot_id ~= "" and not bot_id:match("^bot_[%w]+$") then
  freeswitch.consoleLog("err", "voicebot: ignoring malformed voicebotID\n")
  bot_id = ""
end
local variables_json = collect_vb_variables()

local helper = script_dir() .. "/voicebot_webhook.py"
local command = "/usr/bin/python3 " .. shell_quote(helper)
  .. " --from-number " .. shell_quote(caller)
  .. " --call-id " .. shell_quote(uuid)
if bot_id ~= "" then
  command = command .. " --bot-id " .. shell_quote(bot_id)
end
if variables_json ~= "" then
  command = command .. " --variables-json " .. shell_quote(variables_json)
end
command = command .. " 2>&1"

local pipe = io.popen(command)
if not pipe then
  freeswitch.consoleLog("err", "voicebot: could not start webhook helper\n")
  session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  return
end

local output = pipe:read("*a") or ""
pipe:close()
local ws_url = output:match("^%s*(wss?://[^%s]+)%s*$")
if not ws_url
  or not ws_url:match(
    "^ws://echosphere%.edas%.tech:9011/ws/telephony/freeswitch/"
      .. "vs_[%w_-]+%?transport=audio_fork$"
  )
then
  output = output:gsub("[\r\n]+", " "):sub(1, 300)
  freeswitch.consoleLog("err", "voicebot: session creation failed: " .. output .. "\n")
  session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  return
end

session:answer()
session:sleep(200)
session:setVariable("voicebot_ws_url", ws_url)

local api = freeswitch.API()
local result = api:execute(
  "uuid_audio_fork",
  -- Keep the narrowband PCMU/PCMA caller leg native at 8 kHz through
  -- EchoSphere and streaming STT. JSON playback also remains 8 kHz.
  uuid .. " start " .. ws_url
    .. " mono 8k echosphere {} true false 8000"
) or ""
if result:lower():find("error", 1, true)
  or result:lower():find("fail", 1, true)
then
  freeswitch.consoleLog("err", "voicebot: audio stream start failed: " .. result .. "\n")
  session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  return
end

freeswitch.consoleLog(
  "info",
  "voicebot: EchoSphere audio fork started for uuid=" .. uuid .. "\n"
)

local function handle_transfer_request()
  if transfer_destination == "" then
    freeswitch.consoleLog(
      "warning",
      "voicebot: transfer requested but "
        .. "voicebot_transfer_destination is not configured\n"
    )
    -- Surface the misconfiguration to EchoSphere instead of silently
    -- keeping the caller on the bot after it announced a handoff.
    fire_transfer_event(
      "failed", "", "voicebot_transfer_destination not set"
    )
    return false
  end
  freeswitch.consoleLog(
    "info",
    "voicebot: transferring uuid=" .. uuid
      .. " destination=" .. transfer_destination
      .. " dialplan=" .. transfer_dialplan
      .. " context=" .. transfer_context .. "\n"
  )
  api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
  fire_transfer_event("initiated", transfer_destination)
  local transfer_result = api:execute(
    "uuid_transfer",
    uuid .. " " .. transfer_destination
      .. " " .. transfer_dialplan
      .. " " .. transfer_context
  ) or ""
  local flat_result = transfer_result:gsub("[\r\n]+", " "):sub(1, 300)
  freeswitch.consoleLog(
    "info",
    "voicebot: uuid_transfer result=" .. flat_result .. "\n"
  )
  if not transfer_result:match("^%+OK") then
    -- The fork is already stopped: with no bot and no agent the caller
    -- would otherwise sit in the silence loop below. Fail loudly so the
    -- monitor records it, and clear the call.
    fire_transfer_event(
      "failed", transfer_destination, flat_result:sub(1, 120)
    )
    session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  end
  return true
end

while session:ready() do
  -- The fork injects its internal playout buffer through WRITE_REPLACE.
  -- A continuous one-second silence source supplies outgoing media frames
  -- for that buffer to replace; session:sleep() does not provide this clock.
  session:execute("playback", "silence_stream://1000")

  local transfer_event = pop_matching(transfer_events)
  if transfer_event then
    local event_body = transfer_event:getBody() or "{}"
    freeswitch.consoleLog(
      "info",
      "voicebot: EchoSphere transfer event call_uuid=" .. uuid
        .. " data=" .. event_body:sub(1, 500) .. "\n"
    )
    if handle_transfer_request() then
      -- Transferred (or cleared on failure): this script is done either way.
      return
    end
  end

  local ended = pop_matching(disconnect_events)
  if ended then
    freeswitch.consoleLog(
      "info",
      "voicebot: EchoSphere media socket closed for uuid=" .. uuid
        .. " — clearing the call\n"
    )
    pcall(function()
      api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
    end)
    session:execute("hangup", "NORMAL_CLEARING")
    return
  end

  local failed = pop_matching(error_events)
    or pop_matching(connect_failed_events)
  if failed then
    freeswitch.consoleLog(
      "err",
      "voicebot: EchoSphere media stream failed for uuid=" .. uuid
        .. " — clearing the call\n"
    )
    pcall(function()
      api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
    end)
    session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
    return
  end
end

pcall(function()
  api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
end)
