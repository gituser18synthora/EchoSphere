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
local caller = session:getVariable("voicebot_from_number")
if not caller or caller == "" then
  caller = session:getVariable("caller_id_number")
    or session:getVariable("effective_caller_id_number")
    or ""
end
local did = session:getVariable("voicebot_to_number")
local raw_to = did and did ~= ""
if not did or did == "" then
  did = session:getVariable("voicebot_did")
    or session:getVariable("destination_number")
    or ""
end
local helper = script_dir() .. "/voicebot_webhook.py"
-- Enumerate names only; getVariable preserves the original value rather than
-- forwarding the URL-encoded values in uuid_dump's event headers.
local api = freeswitch.API()
local channel_dump = api:execute("uuid_dump", uuid) or ""
local extra_arguments = {}
for name in channel_dump:gmatch("variable_(vb_[^:\r\n]+):") do
  local key = name:sub(4)
  if key ~= "" then
    extra_arguments[#extra_arguments + 1] = " --field "
      .. shell_quote(key) .. " " .. shell_quote(session:getVariable(name) or "")
  end
end
local command = "/usr/bin/python3 " .. shell_quote(helper)
  .. " --from-number " .. shell_quote(caller)
  .. " --to-number " .. shell_quote(did)
  .. (raw_to and " --to-raw" or "")
  .. " --call-id " .. shell_quote(uuid)
  .. table.concat(extra_arguments)
  .. " 2>&1"

local pipe = io.popen(command)
if not pipe then
  freeswitch.consoleLog("err", "voicebot: could not start webhook helper\n")
  session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  return
end

-- Forward request diagnostics immediately, without mixing them into the URL.
local output_lines = {}
for line in pipe:lines() do
  if line:match("^voicebot_webhook: request ") then
    freeswitch.consoleLog("info", line .. "\n")
  else
    output_lines[#output_lines + 1] = line
  end
end
local output = table.concat(output_lines, "\n")
pipe:close()
local ws_url = output:match("^%s*(wss?://[^%s]+)%s*$")
if not ws_url
  or not ws_url:match(
    "^ws://echosphere%.edas%.tech:9011/ws/telephony/freeswitch/"
      .. "vs_[%w_-]+%?transport=audio_fork$"
  )
then
  output = output:gsub("[\r\n]+", " "):sub(1, 2300)
  freeswitch.consoleLog("err", "voicebot: session creation failed: " .. output .. "\n")
  session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  return
end

session:answer()
session:sleep(200)
session:setVariable("voicebot_ws_url", ws_url)

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

-- Media-bug order matters: attach the recorder after mod_audio_fork so the
-- stereo write channel contains the AI audio injected by WRITE_REPLACE.
if session:getVariable("voicebot_record_calls") == "true" then
  local recording_path = "/home/ashik/echosphere-" .. uuid .. ".wav"
  session:execute("record_session", recording_path)
  freeswitch.consoleLog(
    "info", "voicebot: recording started path=" .. recording_path .. "\n"
  )
end

-- Event handling runs independently so this call leg can keep one continuous
-- write-media clock. Restarting silence_stream every second removed about
-- 40 ms of caller audio at each boundary and produced the audible dot pattern.
local monitor_result = api:execute(
  "luarun", script_dir() .. "/voicebot_event_monitor.lua " .. uuid
) or ""
if monitor_result:lower():find("error", 1, true)
  or monitor_result:lower():find("fail", 1, true)
then
  freeswitch.consoleLog(
    "err", "voicebot: event monitor start failed: " .. monitor_result .. "\n"
  )
  api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
  session:execute("hangup", "NORMAL_TEMPORARY_FAILURE")
  return
end

-- mod_audio_fork injects its playout buffer through WRITE_REPLACE. Endless
-- silence provides uninterrupted outgoing frames. killAudio deliberately
-- raises BREAK to clear bot playout, which also returns this playback call;
-- restart it unless the event monitor marked a real transfer/disconnect.
while session:ready()
  and session:getVariable("voicebot_monitor_stop") ~= "true"
do
  session:execute("playback", "silence_stream://-1")
end

pcall(function()
  api:execute("uuid_audio_fork", uuid .. " stop echosphere {}")
end)
