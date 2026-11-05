"""Real test calls: originate command building and the ESL background job.

The Studio's "Real call" dials a tester's number through the dialer's
FreeSWITCH. These tests pin the safety contract of ``shared.esl_originate``
(number normalization, ``vb_`` mapping of the bot's customer JSON, quoting
that cannot be broken out of, platform-owned variables that user input
cannot override) and ``ESLClient.originate`` (bgapi with our own Job-UUID,
result taken from the BACKGROUND_JOB event, bounded wait).
"""

import asyncio

import pytest

import voice_runtime.freeswitch as fs
from shared.esl_originate import (
    BOT_ID_PARAM,
    MANDATORY_ESL_PARAMS,
    REQUIRED_NON_EMPTY,
    OriginateValidationError,
    build_originate_plan,
    customer_variable_name,
    format_variable_block,
    map_customer_variables,
    mask_phone_number,
    national_number,
    normalize_phone_number,
    parse_originate_result,
    quote_esl_value,
    render_dial_string,
    validate_application,
    validate_variables,
)

BOT = "bot_450a4f7b4832"
TENANT = "tn_fb39b67f5361"
JOB = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"

# The Haier bot's saved test JSON (APIs tab → runtime context test payload).
HAIER_TEST_PAYLOAD = {
    "customer_name": "Rohan",
    "product_name": "Split AC",
    "rejection_reason": "customer_refused_visit",
    "rejection_reason_label": "the visit was declined when our technician called",
    "l3_tagging": "installation",
}


def _base(**overrides):
    base = {name: "x" for name in MANDATORY_ESL_PARAMS}
    base.update({
        "sip_route_uri": "sip:192.168.60.124:5060",
        "sip_h_route-header": "9210",
        "domain_name": "kam-freeswitch.edas.tech",
        "kamailioHost": "192.168.60.124",
        "operatorPrefix": "+91",
        "cli": "+918047133633",
        "origination_caller_id_number": "+918047133633",
        "dialTimeout": "45",
        "ignore_early_media": "true",
    })
    base.update(overrides)
    return base


def _plan(number="+91 98765 43210", **kwargs):
    defaults = dict(
        phone_number=number, bot_id=BOT, tenant_id=TENANT,
        base_params=_base(), param_overrides={}, customer_data=HAIER_TEST_PAYLOAD,
        dial_template="sofia/external/0{number}@{domain_name}",
        application="{cli} XML voicebot_calling",
    )
    defaults.update(kwargs)
    return build_originate_plan(**defaults)


class TestPhoneNumber:
    def test_spaces_dashes_and_brackets_are_dropped(self):
        assert normalize_phone_number("+91 (98765) 43-210") == "+919876543210"
        assert normalize_phone_number("09876543210") == "09876543210"

    @pytest.mark.parametrize("bad", ["", "12345", "+91 98765 abcde", "9876543210;foo", "1" * 16])
    def test_anything_but_digits_is_rejected(self, bad):
        with pytest.raises(OriginateValidationError) as exc:
            normalize_phone_number(bad)
        assert exc.value.errors[0]["field"] == "phoneNumber"

    def test_mask_keeps_last_four(self):
        assert mask_phone_number("+919876543210") == "XXXXXXXX3210"

    def test_national_number_strips_india_prefixes_only(self):
        # The demo dialer dials 0<10 digits> and sends 10-digit customerNumber/cli.
        assert national_number("+917738691223") == "7738691223"
        assert national_number("917738691223") == "7738691223"
        assert national_number("07738691223") == "7738691223"
        assert national_number("7738691223") == "7738691223"
        assert national_number("+14155550123") == "14155550123"


class TestCustomerMapping:
    def test_prefix_rule(self):
        assert customer_variable_name("customer_name") == "vb_customer_name"
        assert customer_variable_name("rejection_reason") == "vb_rejection_reason"
        assert customer_variable_name("rejection_reason_label") == "vb_rejection_reason_label"
        # No double prefix, no renaming of mandatory / system parameters.
        assert customer_variable_name("vb_already") == "vb_already"
        assert customer_variable_name("customerNumber") == "customerNumber"
        assert customer_variable_name(BOT_ID_PARAM) == BOT_ID_PARAM

    def test_haier_payload_maps_with_values_preserved(self):
        mapped = map_customer_variables(HAIER_TEST_PAYLOAD)
        assert mapped.variables == {
            "vb_customer_name": "Rohan",
            "vb_product_name": "Split AC",
            "vb_rejection_reason": "customer_refused_visit",
            "vb_rejection_reason_label": "the visit was declined when our technician called",
            "vb_l3_tagging": "installation",
        }
        assert mapped.skipped == []

    def test_scalars_stringify_nested_and_null_skip_system_left_alone(self):
        mapped = map_customer_variables({
            "age": 42, "score": 4.5, "vip": True, "note": None,
            "address": {"city": "Pune"}, "tags": ["a"], "vb_keep": "yes",
            "customerNumber": "999", "": "blank",
        })
        assert mapped.variables == {
            "vb_age": "42", "vb_score": "4.5", "vb_vip": "true", "vb_keep": "yes",
        }
        reasons = {s["key"]: s["reason"] for s in mapped.skipped}
        assert reasons == {
            "note": "null", "address": "nested", "tags": "nested",
            "customerNumber": "system",
        }

    def test_values_are_bounded_to_what_the_webhook_keeps(self):
        mapped = map_customer_variables({"customer_name": "x" * 500})
        assert len(mapped.variables["vb_customer_name"]) == 200

    def test_invalid_keys_are_skipped_not_sent(self):
        mapped = map_customer_variables({"bad key": "v", "k" * 60: "v"})
        assert mapped.variables == {}
        assert {s["reason"] for s in mapped.skipped} == {"invalid_key"}


class TestQuoting:
    def test_plain_tokens_stay_bare_everything_else_is_quoted(self):
        assert quote_esl_value("45") == "45"
        assert quote_esl_value("sip:kamailio.example:5060") == "sip:kamailio.example:5060"
        assert quote_esl_value("Split AC") == "'Split AC'"
        assert quote_esl_value("<sip:k;lr>") == "'<sip:k;lr>'"
        assert quote_esl_value("") == ""  # bare, like the dialer's operatorId=

    def test_single_quote_cannot_terminate_a_value(self):
        cleaned = validate_variables({"vb_customer_name": "O'Brien, Sr."})
        assert cleaned == {"vb_customer_name": "O’Brien, Sr."}
        assert format_variable_block(cleaned) == "{vb_customer_name='O’Brien, Sr.'}"

    @pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b", "tab\there"])
    def test_control_characters_are_rejected(self, bad):
        with pytest.raises(OriginateValidationError):
            validate_variables({"x": bad})

    @pytest.mark.parametrize("name", ["", "1abc", "has space", "x" * 70, "a=b"])
    def test_bad_variable_names_are_rejected(self, name):
        with pytest.raises(OriginateValidationError):
            validate_variables({name: "v"})

    def test_hyphenated_sip_header_name_is_allowed(self):
        assert validate_variables({"sip_h_route-header": "<sip:x;lr>"}) == {
            "sip_h_route-header": "<sip:x;lr>",
        }

    def test_too_long_value_is_rejected(self):
        with pytest.raises(OriginateValidationError):
            validate_variables({"x": "v" * 501})


class TestDialStringAndApplication:
    def test_placeholders_fill_from_number_and_variables(self):
        out = render_dial_string(
            "sofia/external/0{number}@{domain_name}",
            {"domain_name": "kam-freeswitch.edas.tech"}, "9876543210",
        )
        assert out == "sofia/external/09876543210@kam-freeswitch.edas.tech"

    def test_unknown_placeholder_and_unsafe_result_are_rejected(self):
        with pytest.raises(OriginateValidationError):
            render_dial_string("sofia/gateway/{gw}/{number}", {}, "9876543210")
        with pytest.raises(OriginateValidationError):
            render_dial_string("sofia/external/{number} &park()", {}, "9876543210")
        with pytest.raises(OriginateValidationError):
            render_dial_string("", {}, "9876543210")

    def test_application_shape(self):
        assert validate_application(" &lua(voicebot.lua) ") == "&lua(voicebot.lua)"
        assert validate_application("&park()") == "&park()"
        # The demo dialer's form: the cli as exten into the voicebot_calling context.
        assert validate_application("{cli} XML voicebot_calling", {"cli": "8047133633"}) == (
            "8047133633 XML voicebot_calling"
        )
        assert validate_application("8071420593 XML voicebot_calling") == "8071420593 XML voicebot_calling"
        for bad in ("lua(voicebot.lua)", "&lua(x) &park()", "&lua(a\nb)", "&lua({x})",
                    "8047133633 XML", "8047133633 XML voicebot_calling extra", "{cli} XML voicebot_calling"):
            with pytest.raises(OriginateValidationError):
                validate_application(bad, {})


class TestOriginatePlan:
    def test_platform_owned_values_come_from_the_bot_never_from_input(self):
        plan = _plan(param_overrides={
            "customerNumber": "1111", "tenantId": "tn_other", BOT_ID_PARAM: "bot_other",
            "dialTimeout": "30",
        }, customer_data={**HAIER_TEST_PAYLOAD, "voicebotID": "bot_evil"})
        v = plan.variables
        assert v["customerNumber"] == "9876543210"
        assert v["tenantId"] == TENANT
        assert v[BOT_ID_PARAM] == BOT
        assert v["dialTimeout"] == "30"           # a normal override is honoured
        assert v["vb_customer_name"] == "Rohan"   # customer data rides along
        assert "vb_voicebotID" not in v and v[BOT_ID_PARAM] == BOT
        assert plan.skipped_customer_fields == [
            {"key": "voicebotID", "variable": "voicebotID", "reason": "system"},
        ]
        assert len(plan.origination_uuid) == 36
        assert v["origination_uuid"] == plan.origination_uuid

    def test_command_matches_the_demo_dialer(self):
        # qabackend providerAdaptor.dialPhoneForVoicebot:
        #   {…}sofia/external/0<10 digits>@kam-freeswitch.edas.tech <cli> XML voicebot_calling
        plan = _plan(number="+91 77386 91223", origination_uuid=JOB)
        cmd = plan.command
        v = plan.variables
        assert cmd.startswith("originate {")
        assert cmd.endswith("}sofia/external/07738691223@kam-freeswitch.edas.tech 8047133633 XML voicebot_calling")
        assert v["customerNumber"] == "7738691223"
        assert v["cli"] == "8047133633"
        assert v["origination_caller_id_number"] == "+918047133633"
        assert v["operatorPrefix"] == "+91"
        assert "sip_route_uri=sip:192.168.60.124:5060" in cmd
        assert "sip_h_route-header=9210" in cmd
        assert "vb_product_name='Split AC'" in cmd
        assert f"origination_uuid={JOB}" in cmd
        assert f"voicebotID={BOT}" in cmd
        assert "\n" not in cmd
        assert plan.phone_masked == "XXXXXX1223"

    def test_ten_digit_input_and_bare_cli_are_normalised_the_same_way(self):
        plan = _plan(number="7738691223", base_params=_base(cli="8047133633", origination_caller_id_number=""))
        assert plan.dial_string == "sofia/external/07738691223@kam-freeswitch.edas.tech"
        assert plan.variables["origination_caller_id_number"] == "+918047133633"
        assert plan.application == "8047133633 XML voicebot_calling"

    def test_every_mandatory_parameter_is_present_and_required_blanks_are_reported(self):
        base = _base()
        base["sip_route_uri"] = ""
        base.pop("operatorId")  # the dialer itself sends operatorId= empty without a pool
        base["leadId"] = ""
        plan = _plan(base_params=base)
        for name in MANDATORY_ESL_PARAMS:
            assert name in plan.variables
        assert plan.missing_mandatory == ["sip_route_uri"]
        assert set(REQUIRED_NON_EMPTY) <= set(MANDATORY_ESL_PARAMS)

    def test_override_fills_a_required_blank(self):
        base = _base()
        base["sip_h_route-header"] = ""
        plan = _plan(base_params=base, param_overrides={"sip_h_route-header": "9210"})
        assert plan.missing_mandatory == []

    def test_bad_origination_uuid_is_rejected(self):
        with pytest.raises(OriginateValidationError):
            _plan(origination_uuid="not-a-uuid")

    def test_validation_errors_bubble_with_fields(self):
        with pytest.raises(OriginateValidationError) as exc:
            _plan(param_overrides={"sip_route_uri": "a\nb"})
        assert exc.value.errors[0]["field"] == "sip_route_uri"


class TestResultParsing:
    def test_ok_and_err(self):
        assert parse_originate_result("+OK 1a2b3c4d-0000-1111-2222-333344445555\n") == (
            True, "1a2b3c4d-0000-1111-2222-333344445555",
        )
        assert parse_originate_result("-ERR USER_BUSY\n") == (False, "USER_BUSY")
        assert parse_originate_result("")[0] is False


# ── ESLClient.originate over a scripted socket ──────────────────────────────


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._buf = b"".join(chunks)
        self.block = asyncio.Event()

    async def readline(self) -> bytes:
        if not self._buf:
            # Nothing scripted: behave like a quiet socket (block forever).
            await self.block.wait()
            return b""
        index = self._buf.find(b"\n")
        line, self._buf = self._buf[: index + 1], self._buf[index + 1:]
        return line

    async def readexactly(self, n: int) -> bytes:
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


class _Writer:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.append(data.decode())

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _reply(text: str) -> bytes:
    return f"Content-Type: command/reply\nReply-Text: {text}\n\n".encode()


def _job_event(job: str, body: str) -> bytes:
    event = (
        "Event-Name: BACKGROUND_JOB\n"
        f"Job-UUID: {job}\n"
        "Job-Command: originate\n"
        f"Content-Length: {len(body)}\n"
        "\n"
        f"{body}"
    )
    return (
        f"Content-Type: text/event-plain\nContent-Length: {len(event)}\n\n{event}"
    ).encode()


def _client(chunks: list[bytes]) -> tuple[fs.ESLClient, _Writer]:
    client = fs.ESLClient(host="127.0.0.1", port=1, password="x", timeout=0.5)
    reader, writer = _Reader(chunks), _Writer()

    async def _connect():
        client._reader, client._writer = reader, writer

    client.connect = _connect  # type: ignore[method-assign]
    return client, writer


class TestESLOriginate:
    async def test_bgapi_with_our_job_uuid_and_result_from_the_event(self):
        client, writer = _client([
            _reply("+OK filter added"),
            _reply("+OK event listener enabled plain"),
            _reply(f"+OK Job-UUID: {JOB}"),
            _job_event(JOB, "+OK 1a2b3c4d-0000-1111-2222-333344445555\n"),
        ])
        result = await client.originate("originate {a=1}sofia/x &park()", job_uuid=JOB, timeout=2)
        assert result == (True, "1a2b3c4d-0000-1111-2222-333344445555")
        assert writer.sent == [
            f"filter Job-UUID {JOB}\n\n",
            "event plain BACKGROUND_JOB\n\n",
            f"bgapi originate {{a=1}}sofia/x &park()\nJob-UUID: {JOB}\n\n",
        ]

    async def test_failure_cause_is_returned_not_raised(self):
        client, _ = _client([
            _reply("+OK"), _reply("+OK"), _reply(f"+OK Job-UUID: {JOB}"),
            _job_event(JOB, "-ERR USER_BUSY\n"),
        ])
        assert await client.originate("originate x", job_uuid=JOB, timeout=2) == (False, "USER_BUSY")

    async def test_event_racing_the_reply_is_not_lost(self):
        client, _ = _client([
            _reply("+OK"), _reply("+OK"),
            _job_event(JOB, "-ERR NO_ROUTE_DESTINATION\n"),
            _reply(f"+OK Job-UUID: {JOB}"),
        ])
        assert await client.originate("originate x", job_uuid=JOB, timeout=2) == (
            False, "NO_ROUTE_DESTINATION",
        )

    async def test_other_jobs_are_ignored_and_the_wait_is_bounded(self):
        client, _ = _client([
            _reply("+OK"), _reply("+OK"), _reply(f"+OK Job-UUID: {JOB}"),
            _job_event("ffffffff-0000-1111-2222-333344445555", "+OK other\n"),
        ])
        assert await client.originate("originate x", job_uuid=JOB, timeout=0.3) is None

    async def test_rejected_bgapi_raises(self):
        client, _ = _client([_reply("+OK"), _reply("+OK"), _reply("-ERR command not found")])
        with pytest.raises(fs.ProviderError):
            await client.originate("originate x", job_uuid=JOB, timeout=1)

    async def test_line_breaks_and_bad_job_ids_never_reach_the_socket(self):
        client, writer = _client([])
        with pytest.raises(fs.ProviderError):
            await client.originate("originate x\nuuid_kill all", job_uuid=JOB, timeout=1)
        with pytest.raises(fs.ProviderError):
            await client.originate("originate x", job_uuid="x; rm", timeout=1)
        assert writer.sent == []
