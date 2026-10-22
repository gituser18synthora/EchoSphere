"""Call-start prompt warm-up (Phase 2): fired once per call from the greeting
path, recorded as a decision_warmup event, never blocking and never a
decision attempt."""
import asyncio

from tests.unit.test_agentic_orchestration import _AgenticLLMStub, make_agentic_brain

INTENTS = [{"name": "pay_now", "description": "wants to pay", "samples": ["payment karna hai"]}]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


class TestDecisionWarmup:
    async def test_greeting_path_fires_one_warm_up_and_records_it(self):
        llm = _AgenticLLMStub([{"scope": "in_scope", "next_action": "answer"}])
        brain = make_agentic_brain(llm=llm, intents=INTENTS)
        assert brain._goal_engine.enabled
        spoken = []

        async def _say(text, **kwargs):
            spoken.append(text)

        brain._say = _say
        await brain._open_session()
        await _settle()
        await brain._open_session()          # a second open must not warm up again
        await _settle()
        warmups = [data for kind, data in brain._recorder.events if kind == "decision_warmup"]
        assert len(warmups) == 1
        assert warmups[0]["ok"] is True and warmups[0]["before_first_decision"] is True
        assert [c["max_tokens"] for c in llm.generate_calls] == [1]
        assert brain._goal_engine.warmup is not None and not brain._goal_engine.warmup_pending
        # the scripted decision is still queued for the first real turn
        assert llm._decisions == [{"scope": "in_scope", "next_action": "answer"}]
        assert spoken, "the greeting still plays"

    async def test_warm_up_failure_never_reaches_the_call(self):
        class _Broken(_AgenticLLMStub):
            async def generate(self, messages, *, max_tokens=None, **kwargs):
                if max_tokens == 1:
                    raise RuntimeError("provider down")
                return await super().generate(messages, max_tokens=max_tokens, **kwargs)

        brain = make_agentic_brain(llm=_Broken([]), intents=INTENTS)

        async def _say(text, **kwargs):
            pass

        brain._say = _say
        await brain._open_session()
        await _settle()
        warmups = [data for kind, data in brain._recorder.events if kind == "decision_warmup"]
        assert warmups == [{"ok": False, "error": "RuntimeError",
                            "latency_ms": warmups[0]["latency_ms"], "before_first_decision": True}]
        assert brain._goal_engine.enabled and not brain._goal_engine._disabled_after_timeouts

    async def test_engine_off_bots_do_not_warm_up(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(llm=llm)       # no intents / policy → engine disabled
        assert not brain._goal_engine.enabled

        async def _say(text, **kwargs):
            pass

        brain._say = _say
        await brain._open_session()
        await _settle()
        assert brain._decision_warmup_task is None
        assert not any(kind == "decision_warmup" for kind, _ in brain._recorder.events)
        assert llm.generate_calls == []

    async def test_cleanup_cancels_a_pending_warm_up(self):
        class _Slow(_AgenticLLMStub):
            async def generate(self, messages, *, max_tokens=None, **kwargs):
                if max_tokens == 1:
                    await asyncio.sleep(5)
                return await super().generate(messages, max_tokens=max_tokens, **kwargs)

        brain = make_agentic_brain(llm=_Slow([]), intents=INTENTS)

        async def _say(text, **kwargs):
            pass

        brain._say = _say
        await brain._open_session()
        await _settle()
        task = brain._decision_warmup_task
        assert task is not None and not task.done()
        await brain.cleanup()
        await _settle()
        assert task.cancelled() or task.done()
        assert brain._decision_warmup_task is None
