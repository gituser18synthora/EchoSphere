# Caller-value confirmation

An ask node can opt into read-back of a value collected from the caller in
the current workflow session. This is shared engine functionality; names,
aliases, wording and disclosure mode belong to each bot's workflow.

```json
{
  "variable": "contact_number",
  "valueReadback": {
    "aliases": ["mobile", "phone number", "मोबाइल"],
    "mode": "last4",
    "speakDigits": true,
    "responses": {
      "en": {
        "template": "The number you gave ends in {value}.",
        "missing": "I do not have that number yet."
      },
      "hi": {
        "template": "आपके बताए नंबर के अंतिम अंक {value} हैं।",
        "missing": "अभी आपका नंबर नहीं मिला है।"
      }
    }
  }
}
```

`full` retains and reads the caller's complete collected value; `last4`
retains only its final four characters for read-back. Enable full disclosure
only for fields the bot is permitted to repeat; do not enable it for OTPs,
PINs or passwords. No read-back is enabled by default. The permitted display
value is checkpointed separately from ordinary masked PII slots and is not
exported in workflow slot results, tool arguments or LLM slot context. The
normal spoken reply still passes through output/transcript guardrails.

The reply stays on the pending node, leaves digit buffers and retries intact,
and does not imply authentication or consent. Templates should answer the
clarification without adding a new yes/no confirmation question. This feature
does not implement correcting a previously collected value: author a separate
correction path if that is required. Pre-existing masked sessions without a
retained value receive the `missing` response, never reconstructed digits.

English, Hindi and Hinglish request forms come from language packs. Additional
languages can add `value_readback_request` and `caller_value_reference` forms.
Aliases match whole phrases. If multiple configured fields match, the engine
does not guess. Generic "repeat" still repeats the bot; a request naming the
caller's value reaches the workflow. Hangup and transfer keep precedence.

For a generic alias such as `number`, configure `excludeAliases` for phrases
that identify a different field (for example `account number`). Exclusions
match whole phrases, so mentioning an undelivered OTP elsewhere in a mobile
confirmation request does not suppress the mobile read-back.

For an ask that must reject digits referring to another field, configure
`rejectAnswerPatterns` (a list of regular expressions), `rejectedAnswerReply`
and optional `rejectedAnswerReplyByLanguage`. Matching input neither fills the
slot nor increments its retries, including when the workflow first starts.

These keys require the updated runtime. Saving them in the database does not
upgrade existing deployed code. Existing workflows without the keys retain
their collection behavior. Align the bot's prompt with its disclosure policy.
