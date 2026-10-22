"""PendingAsk — the question a short caller reply answers, resolved from the
paused workflow ask, the collections policy, the opening step or the bot's own
last utterance; plus the engine's ``awaitingAsk`` shape."""
import asyncio
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.ask_resolution as ask
import shared.orchestration.workflow_engine as we
from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import PENDING_ASK_NONE, ConversationBrain, PendingAsk, pending_kind_label


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


class _LLMStub:
    """Records the system prompt and streams a fixed reply."""

    def __init__(self, reply="ठीक है।"):
        self.reply = reply
        self.systems = []

    def stream(self, history, *, system, temperature, max_tokens):
        self.systems.append(system)

        async def _gen():
            yield self.reply

        return _gen()


class _WorkflowStub:
    def __init__(self, result):
        self.result = result

    async def handle_turn_detailed(self, **kwargs):
        return self.result


def make_brain(llm=None, workflow_engine=None, **kwargs) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.", **kwargs,
    )
    brain = ConversationBrain(config=config, llm=llm, recorder=_RecorderStub(),
                              workflow_engine=workflow_engine, finalize_grace=0.05)
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel_task
    return brain


# ── (a) paused workflow ask ──────────────────────────────────────────────────

class TestWorkflowAsk:
    def test_question_and_engine_shape_win(self):
        brain = make_brain()
        brain._active_workflow = "wf"
        brain._pending_workflow_question = "पूरा payment करेंगे या partial?"
        brain._pending_ask_shape = {"kind": "choice", "options": ["full", "partial"]}
        brain._last_bot_reply = "Kya aap Rahul ji bol rahe hain?"   # outranked
        assert brain._pending_ask() == PendingAsk(
            "पूरा payment करेंगे या partial?", "choice", ("full", "partial"))

    def test_missing_shape_defaults_to_free_text(self):
        brain = make_brain()
        brain._active_workflow = "wf"
        brain._pending_workflow_question = "Aapka naam kya hai?"
        assert brain._pending_ask().kind == "free_text"

    def test_shape_is_snapshotted_and_restored_with_the_question(self):
        brain = make_brain()
        brain._pending_workflow_question = "Q1?"
        brain._pending_ask_shape = {"kind": "yes_no", "options": []}
        brain._open_turn_pending_question = brain._pending_workflow_question
        brain._open_turn_pending_ask_shape = brain._pending_ask_shape
        brain._pending_workflow_question, brain._pending_ask_shape = "Q2?", {"kind": "number", "options": []}
        brain._pending_workflow_question = brain._open_turn_pending_question
        brain._pending_ask_shape = brain._open_turn_pending_ask_shape
        assert brain._pending_ask_shape == {"kind": "yes_no", "options": []}


# ── (b) collections policy ───────────────────────────────────────────────────

class TestPolicyAsk:
    def test_pending_identity_is_yes_no(self):
        brain = make_brain()
        brain._policy = SimpleNamespace(awaiting_identity=True, verified=False,
                                        awaiting_reference=False, transaction_reference=None)
        brain._last_bot_reply = "क्या मैं राहुल जी से बात कर रहा हूँ?"
        assert brain._pending_ask() == PendingAsk("क्या मैं राहुल जी से बात कर रहा हूँ?", "yes_no")

    def test_pending_reference_is_free_text_and_outranks_identity(self):
        brain = make_brain()
        brain._policy = SimpleNamespace(awaiting_identity=True, verified=False,
                                        awaiting_reference=True, transaction_reference=None)
        brain._last_bot_reply = "Transaction number bataiye?"
        assert brain._pending_ask().kind == "free_text"

    def test_captured_reference_no_longer_pends(self):
        brain = make_brain()
        brain._policy = SimpleNamespace(awaiting_identity=False, verified=True,
                                        awaiting_reference=True, transaction_reference="UTR123")
        brain._last_bot_reply = "Dhanyavaad."
        assert brain._pending_ask() == PENDING_ASK_NONE


# ── (c) opening question ─────────────────────────────────────────────────────

class TestOpeningAsk:
    def test_opening_question_is_yes_no(self):
        brain = make_brain()
        brain._pending_opening_question = lambda: "क्या मैं राहुल जी से बात कर रहा हूँ?"
        assert brain._pending_ask() == PendingAsk("क्या मैं राहुल जी से बात कर रहा हूँ?", "yes_no")


# ── (d) last-reply heuristics ────────────────────────────────────────────────

class TestLastReplyHeuristics:
    def _ask(self, reply):
        brain = make_brain()
        brain._last_bot_reply = reply
        return brain._pending_ask()

    def test_no_question_mark_means_nothing_pending(self):
        assert self._ask("Theek hai, dhanyavaad.") == PENDING_ASK_NONE
        assert self._ask("") == PENDING_ASK_NONE

    def test_plain_question_is_free_text(self):
        pending = self._ask("Aapka naam bataiye?")
        assert pending.kind == "free_text" and pending.question == "Aapka naam bataiye?"

    def test_yes_no_constructions(self):
        for reply in ("Kya aap abhi payment karenge?", "क्या आप राहुल जी हैं?",
                      "Aapka address Mumbai hai, sahi hai?", "Theek hai?",
                      "Main dobara call karun, ji?", "Haan ya nahi?",
                      "Aapka order aa gaya. Kya sab theek tha?", "To kya aap aayenge?"):
            assert self._ask(reply).kind == "yes_no", reply

    def test_wh_kya_is_not_yes_no(self):
        # mid-sentence "kya" = "what": a name/value is expected, not yes/no.
        for reply in ("Aapka naam kya hai?", "Aur kya chahiye?", "आपका नाम क्या है?"):
            assert self._ask(reply).kind == "free_text", reply

    def test_choice_with_easy_options(self):
        pending = self._ask("Subah ya sham?")
        assert pending.kind == "choice" and pending.options == ("Subah", "sham")
        devanagari = self._ask("पहला या दूसरा?")
        assert devanagari.kind == "choice" and devanagari.options == ("पहला", "दूसरा")
        # A long "A ya B" is still a choice, but its sides are not extracted.
        long = self._ask("Aap subah ka slot lenge ya sham ka?")
        assert long.kind == "choice" and long.options == ()

    def test_choice_without_easy_options(self):
        pending = self._ask("1) cash 2) UPI 3) card — kaunsa?")
        assert pending.kind == "choice" and pending.options == ()

    def test_danda_after_question_mark(self):
        assert self._ask("क्या आप सुन पा रहे हैं?।").kind == "yes_no"

    def test_labels(self):
        assert pending_kind_label(PendingAsk("q", "yes_no")) == "yes/no"
        assert pending_kind_label(PendingAsk("q", "choice", ("a", "b"))) == "one of [a | b]"
        assert pending_kind_label(PENDING_ASK_NONE) == "none"


# ── the short-reply block in every generation ────────────────────────────────

class TestShortReplyRulesInPrompt:
    async def test_plain_chat_turn_gets_the_block_with_placeholders_filled(self):
        llm = _LLMStub()
        brain = make_brain(llm=llm)
        brain._last_bot_reply = "Kya aap kal subah aa sakte hain?"
        brain._history.append({"role": "assistant", "content": brain._last_bot_reply})
        await brain._handle_turn("haan ji")
        system = llm.systems[-1]
        assert "# Answering short replies (runtime rules)" in system
        assert "Pending question: Kya aap kal subah aa sakte hain?" in system
        assert "Expected answer: yes/no" in system
        # after the per-call static prefix + length rule, before the language suffix
        assert system.index("# Voice response length") < system.index("# Answering short replies")
        assert system.index("# Answering short replies") < system.index("# Reply language")

    async def test_nothing_pending_still_states_none(self):
        llm = _LLMStub()
        brain = make_brain(llm=llm)
        brain._last_bot_reply = "Theek hai, dhanyavaad."
        await brain._handle_turn("mujhe apna balance jaanna hai")
        system = llm.systems[-1]
        assert "Pending question: none" in system
        assert "Expected answer: none" in system

    async def test_workflow_block_already_quoting_the_question_is_not_repeated(self):
        llm = _LLMStub()
        workflow = _WorkflowStub({
            "reply": "", "done": False, "status": "collecting", "source": "definition",
            "workflowId": "wf_1", "trace": ["n_ask"], "slots": {}, "handoffQueue": None,
            "offScript": True, "nodePrompt": "Poora ya partial?", "signal": "question",
            "awaitingKind": "ask", "awaitingAsk": {"kind": "choice", "options": ["full", "partial"]},
        })
        brain = make_brain(llm=llm, workflow_engine=workflow)
        brain._active_workflow = "wf_1"
        brain._pending_workflow_question = "Poora ya partial?"
        brain._pending_ask_shape = {"kind": "choice", "options": ["full", "partial"]}
        await brain._handle_turn("iska matlab kya hai bhai")
        system = llm.systems[-1]
        assert 'waiting on this step: "Poora ya partial?"' in system
        assert "Pending question: (stated above)" in system
        assert "Expected answer: one of [full | partial]" in system
        block = system[system.index("# Answering short replies"):system.index("# Reply language")]
        assert "Poora ya partial?" not in block


# ── router: short answer to a pending question ───────────────────────────────

class TestShortAnswerRouting:
    async def test_bare_name_with_pending_question_is_answered_not_clarified(self):
        llm = _LLMStub()
        brain = make_brain(llm=llm)
        brain._last_bot_reply = "Aapka naam bataiye?"
        await brain._handle_turn("Rahul")
        reasons = [d.get("reason") for k, d in brain._recorder.events if k == "route_decision"]
        assert reasons == ["short_answer_to_pending"]
        assert llm.systems, "the reply LLM must answer the short reply"
        assert "Pending question: Aapka naam bataiye?" in llm.systems[-1]

    async def test_bare_name_with_nothing_pending_keeps_the_canned_clarify(self):
        llm = _LLMStub()
        brain = make_brain(llm=llm)
        brain._last_bot_reply = "Theek hai, dhanyavaad."
        await brain._handle_turn("Rahul")
        reasons = [d.get("reason") for k, d in brain._recorder.events if k == "route_decision"]
        assert reasons == ["too_short"]
        assert llm.systems == []


# ── "hmm" on the brain's fallback signal path ────────────────────────────────

class TestHmmAffirm:
    def test_only_when_a_yes_no_question_is_pending(self):
        brain = make_brain()
        brain._last_bot_reply = "Kya aap kal aa sakte hain?"
        assert brain._hmm_affirm("hmm") == "affirm"
        assert brain._hmm_affirm("hmm theek") is None
        brain._last_bot_reply = "Aapka naam bataiye?"
        assert brain._hmm_affirm("hmm") is None
        brain._last_bot_reply = "Dhanyavaad."
        assert brain._hmm_affirm("hmm") is None


# ── Goal Engine live state ───────────────────────────────────────────────────

class TestOrchestrationState:
    def test_choice_ask_adds_expected_answer_and_options(self):
        brain = make_brain()
        brain._active_workflow = "wf"
        brain._pending_workflow_question = "Poora ya partial?"
        brain._pending_ask_shape = {"kind": "choice", "options": ["full", "partial"]}
        state = brain._orchestration_state()
        assert state["pending_question"] == "Poora ya partial?"
        assert state["expected_answer"] == "choice"
        assert state["options_offered"] == ["full", "partial"]

    def test_yes_no_from_last_reply_and_nothing_pending(self):
        brain = make_brain()
        brain._last_bot_reply = "Kya aap kal aa sakte hain?"
        assert brain._orchestration_state()["expected_answer"] == "yes/no"
        brain._last_bot_reply = "Dhanyavaad."
        state = brain._orchestration_state()
        assert "expected_answer" not in state and "options_offered" not in state


# ── ask_answer_shape / engine awaitingAsk ────────────────────────────────────

def _ask_node(config):
    return {"id": "n_ask", "kind": "ask", "config": {"question": "Q?", "variable": "v", **config}}


class TestAskAnswerShape:
    def test_yes_no_synonyms(self):
        node = _ask_node({"synonyms": {"yes (called)": ["haan", "call kiya"], "no": ["nahi"]}})
        assert ask.ask_answer_shape(node, "v") == {"kind": "yes_no", "options": []}

    def test_choice_options_follow_the_spoken_order(self):
        node = _ask_node({"question": "partial karenge ya poora?", "entityType": "list",
                          "synonyms": {"full": ["poora", "पूरा"], "partial": ["partial", "aadha"]}})
        assert ask.ask_answer_shape(node, "v") == {"kind": "choice", "options": ["partial", "full"]}

    def test_allowed_values_unmentioned_keep_declaration_order(self):
        node = _ask_node({"allowedValues": ["cash", "upi", "card"]})
        assert ask.ask_answer_shape(node, "v")["options"] == ["cash", "upi", "card"]

    def test_number_date_time_and_free_text(self):
        assert ask.ask_answer_shape(_ask_node({"entityType": "phone"}), "v")["kind"] == "number"
        assert ask.ask_answer_shape(_ask_node({"pattern": r"\d{6}"}), "v")["kind"] == "number"
        assert ask.ask_answer_shape(_ask_node({"entityType": "spoken_day"}), "v")["kind"] == "date"
        assert ask.ask_answer_shape(_ask_node({"entityType": "time"}), "v")["kind"] == "time"
        assert ask.ask_answer_shape(_ask_node({"entityType": "text"}), "v")["kind"] == "free_text"


def _definition(ask_config: dict):
    return {"id": "wf_ask", "name": "ask_wf", "version": 1, "nodes": [
        {"id": "n_start", "kind": "start", "config": {}},
        {"id": "n_ask", "kind": "ask", "config": {"question": "Poora ya partial?", "variable": "ptype",
                                                  "entityType": "list", **ask_config}},
        {"id": "n_end", "kind": "end", "config": {}},
    ], "edges": [{"id": "e0", "from": "n_start", "to": "n_ask"}, {"id": "e1", "from": "n_ask", "to": "n_end"}]}


def _engine(monkeypatch, definition):
    monkeypatch.setattr(we, "load_workflow_definition", lambda t, b, n: definition)
    wf = we.WorkflowEngine()

    async def _memory():
        if wf._checkpointer is None:
            wf._checkpointer = MemorySaver()
        return wf._checkpointer

    monkeypatch.setattr(wf, "_get_checkpointer", _memory)
    return wf


class TestEngineReportsAwaitingAsk:
    async def test_paused_choice_ask_carries_its_shape(self, monkeypatch):
        engine = _engine(monkeypatch, _definition(
            {"synonyms": {"full": ["poora"], "partial": ["partial", "aadha"]}}))
        result = await engine.handle_turn_detailed(
            session_id="s", tenant_id="tn", bot_id="bot", workflow_name="ask_wf",
            user_text="start", language="hi-IN", reset_state=True)
        assert result["nodePrompt"] == "Poora ya partial?"
        assert result["awaitingAsk"] == {"kind": "choice", "options": ["full", "partial"]}

    async def test_finished_flow_reports_none(self, monkeypatch):
        engine = _engine(monkeypatch, _definition({"synonyms": {"full": ["poora"], "partial": ["partial"]}}))
        await engine.handle_turn_detailed(
            session_id="s2", tenant_id="tn", bot_id="bot", workflow_name="ask_wf",
            user_text="start", language="hi-IN", reset_state=True)
        result = await engine.handle_turn_detailed(
            session_id="s2", tenant_id="tn", bot_id="bot", workflow_name="ask_wf",
            user_text="poora", language="hi-IN")
        assert result["done"] is True
        assert result["awaitingAsk"] is None
