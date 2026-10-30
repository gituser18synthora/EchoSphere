Bot `bot_59a84478f155`: English fix deployed locally and live on 21 September 2026.

Workflow `wf_7e4cf166c7bd`: local v28 → v30; live v15 → v17.

The English path now uses the existing four-field semantic extractor, scoped
to `en` with `semanticExtractionLanguages`. Intelligible incident descriptions
can advance without inventing an answer to any delivery question. English
evidence guards reject unmentioned location answers and prevent customer-call
details from becoming CX-call answers. English extraction requests JSON output
from the OpenAI provider; ordinary generation remains unchanged.

English `textByLanguage.en` entries were added to the MDND nodes, so fixed
questions and messages do not depend on runtime translation. All original
Hindi node content, matching rules, edges, behavior version, system prompt,
STT settings, voices and greetings were preserved. Comparing the saved
workflow against its original backup confirmed that only the English opt-in,
narrative acceptance flag and English text entries changed.

Validation:

- 1,690 automated tests passed; one opt-in real-model test skipped. This
  includes frozen workflow/router replays and MDND regression tests.
- Separate real-model checks covered the exact reported sentence, “given the
  correct product”, the recorded `NBND` variant, complaint without delivery
  facts, instruction versus completed handover, order still with the partner,
  and explicit negative answers. The final local replay includes history.
- Hindi before/after samples produced identical replies, slots and node
  traces, with zero semantic extraction calls.
- After deployment and service restart, actual local and live
  `/testing/simulate` requests completed four English turns through final
  verification, without repeating the incident question or inventing CX calls.
- Deployed runtime/setup/check-script SHA-256 hashes matched local files.
- Live API, voice worker and gateway restarted only after checking that both
  voice services had zero active sessions. Health checks passed afterward.

Verified live response to the reported complaint:

> I understand your concern. Did you reach the customer's delivery location, and did you call the customer before delivery?

The handover recipient is already `customer (direct)`. After the partner
confirms reaching the location and calling the customer, the bot asks only
whether CX support called. A negative CX answer is correctly summarized as no
CX call. Verification used text/API replays; no new telephone audio call was
placed. STT acoustic accuracy was not changed.

English extraction performs four concurrent model requests per workflow turn;
the observed local replay turns generally took about 1–2 seconds. Hindi keeps
its prior deterministic path and does not incur these requests.

Backups and logs (environment-local paths):

- Local original workflow: `/tmp/mdnd-english-before-kf_q3ueh.json`.
- Live original workflow: `/tmp/mdnd-english-before-rduavg5k.json`.
- Live original code: `/tmp/mdnd-english-code-before-20260921-171102.tar.gz`.
- Local final tests: `/tmp/mdnd-english-final-tests.log`.
- Local final real-model replay: `/tmp/mdnd-english-local-final-replay.log`.
- Local served API check: `/tmp/mdnd-english-local-api-check.log`.
- Live served API check: `/tmp/mdnd-english-live-api-final.log`.

Recheck without changing configuration or creating calls/tickets:
`env/bin/python zepto/tests/check_mdnd_english.py`.
The deployment script `zepto/setup/13_mdnd_english_extraction.py` previews by
default and saves only with `--apply`; reapplying the completed patch is a no-op.
