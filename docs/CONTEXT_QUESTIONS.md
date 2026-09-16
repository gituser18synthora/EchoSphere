# Questions about the current call

A caller can ask who is calling, why the call was placed, or what an earlier
statement meant without answering the workflow's pending question. These
questions need an answer grounded in the current bot and call.

## Runtime behavior

- The decision engine and legacy classifier interpret meaning across languages
  and emit `context_question: true` for a standalone question about the call.
  There is no shared list of tenant names, purposes or language-specific phrases.
- Validation rejects this hint for out-of-scope/injection decisions, low
  confidence, confirmed/denied answers, supplied slots and tool requests.
  Configured knowledge, tool, transfer and hangup intents retain their handling.
- An opening question reaches response generation instead of the unclear-speech
  retry. The bot's authored opening question remains pending. An acknowledgement
  that does not start a configured workflow repeats this question directly,
  without an unclear-speech apology or free-chat business question.
- During an active workflow, an accepted hint reads the pending checkpoint
  without invoking the graph. It does not capture a slot, spend a retry, execute
  a node or advance the flow. The next actual answer resumes that same step.
- Response generation receives the full current bot prompt, permitted session
  context, conversation history and pending step. Identity verification and
  disclosure restrictions continue to apply. Unknown facts, including physical
  calling locations, must not be invented.
- The response request carries the current question task next to the final
  caller message as well as in the system instructions. This prevents general
  script/refusal examples or an earlier mistaken reply from defining the task.
  The temporary note never enters stored history or the transcript. Opening
  questions and non-KB off-script questions also receive this grounding when
  the classifier omits the context hint. `context_question_reply` records use
  of this response path without logging the prompt or customer facts.
- The response task distinguishes a request to repeat the last question from
  a question about the agent's identity. It receives the authoritative pending
  question and the previous bot utterance. After an informational answer it
  returns to that same question; a repeat request restates it once. If the
  generated answer contains no question, the runtime speaks the saved question
  separately and records `pending_question_resumed`. This fallback checks
  question punctuation; it does not prove semantic equivalence of a generated
  question. No new workflow step is executed by the fallback.
- The brain supplies the current workflow's pending question to the decision
  stage, tracks it separately from the last generated reply, and restores it
  when an interrupted workflow turn is rolled back.
- A polite acknowledgement before a question is distinguished from an actual
  answer to the pending step. "Yes, who is calling?" alone does not establish
  that the intended person answered the phone.
- A mixed utterance that both answers the step and asks a question is not a
  standalone context question; existing slot/action handling still processes it.

No database migration or per-tenant prompt rewrite is required. Each call keeps
its own bot configuration and session state. Response language continues to use
the conversation's existing language selection.

## Verification and limits

`tests/unit/test_context_questions.py` covers isolated bot contexts, unverified
private facts, opening routing, protected intent routes, and unchanged in-memory
workflow checkpoints followed by normal resumption. Router, classifier, goal,
policy, guardrail, language and workflow regression suites cover adjacent paths.

The context hint depends on semantic classification. On model timeout or invalid
output, the existing deterministic fallback remains in use; it does not acquire
new guarantees for every language or utterance. Routing events expose
`context_question`, and a paused turn emits `workflow_off_script`. Text-level
checks do not verify speech recognition, synthesis or telephone audio quality.

## Follow-up and local rollout (2026-09-16)

The reported call `cv_2273a70927e3` showed that routing alone was insufficient:
the first call-origin question was classified in-scope with
`context_question=true`, yet generation refused it and asked a later workflow
question. The second identity question had `signal=question` but no context
hint. Both now receive the request-scoped response task described above.

The next reported call, `cv_98a5ab69500d`, exposed an overcorrection: the response
task explicitly prohibited follow-up questions, leaving the caller without a
pending question and eventually triggering the silence timer. It also showed
an opening acknowledgement generating a business question before the workflow
started. The current behavior above replaces that answer-only instruction and
prevents that acknowledgement path from inventing a question.

The resume fix passed 193 routing/context/language/guardrail regression tests
and 116 delivery, silence, workflow response and rollback tests (overlapping
suites). Fictional clinic/hotel model checks exercised Hindi, English, Marathi,
Tamil and Malayalam. The exact Hindi repeat request returned the current
question instead of reintroducing the agent; active cases preserved pending
steps, collected values and retry counts. These are sample text-level checks;
wording can still vary across models and languages. Real tenant prompts and
call records were not sent externally by the diagnostic scripts.

The local development voice worker is reloaded only after verifying zero
active calls, then checked for health and Redis connectivity. No database
change or live deployment is required. Validate with a fresh local audio call:
ask the caller-identity/origin question, acknowledge the answer, request the
pending question again, and then answer that question normally.
