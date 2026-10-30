# Malayalam wording review — bot_f49776262f1a

Reviewed 23 October 2026. Changes were applied only to the local development database. No production configuration, application code or running service was changed.

## Findings

- Both local and live configurations selected OpenAI `gpt-4.1`, temperature 0, maximum 300 output tokens. Malayalam used the Sarvam `bulbul:v3` / `kavya` language override; the primary ElevenLabs selection was not the voice used in the 19 October Malayalam calls.
- Live call `cv_e20e9806e5a6` on 8 October contained formal sympathy such as `ഞാൻ ദുഖിതനാണ്`, repeated reason questions and translated phrasing. Call `cv_1f372ad74abd` on 14 October contained formal recording disclosure and a Hindi honorific in Malayalam. Call `cv_05164b7b4487` on 19 October was better but still followed awkward mixed-language examples and repeated complaint summaries.
- System prompt v27 already requested colloquial Malayalam. Some of its examples were themselves awkward, forced English replacements for ordinary Malayalam words, or suggested an unverified action. The greeting retained formal recording language and an assumed male honorific.
- Workflow v34 contained Hindi speech plus instructions requiring Hindi/verbatim delivery. The constrained workflow translation path does not receive the full bot persona prompt. Explicit Malayalam node text addresses that path without an application code change.
- These observations and same-model comparisons support correcting the authored instructions first. They do not prove the model will never make a Malayalam error or establish comparative quality against other models.

## Local changes

| Configuration | Previous | Current |
| --- | --- | --- |
| System prompt | 27 | 28 |
| Greeting | 6 | 8 |
| Workflow | 34 | 35 |

Greeting version 7 already existed as an inactive historical version and was preserved. Local published prompt pointers and the local release workflow pin were advanced to the new versions. An audit record and workflow revision snapshots were saved. The local bot configuration cache was invalidated.

- Added respectful spoken Malayalam examples, concise acknowledgements, polite questions, one question per reply, and handling of prior reasons and suggestions.
- Replaced only the Malayalam greeting variant; retained Hindi and English greetings.
- Added `textByLanguage.ml` to 21 workflow nodes and Malayalam recognition examples to existing identity, experience, reuse and further-help values.
- Removed conflicting Hindi-only closing and acknowledgement delivery instructions. Updated the Goal Engine's authored `tone` prompt so its separate response path receives the same Malayalam guidance.
- Retained all 61 workflow edges, node kinds, rating range, support options, recording disclosure, model, generation settings and voice selections.

Example generated complaint response:

> അങ്ങനെ ബുദ്ധിമുട്ടുണ്ടായതിൽ sorry. എന്താണ് സംഭവിച്ചതെന്ന് ഒന്ന് പറയാമോ?

Example after the caller has already requested system updates and staff training:

> പറഞ്ഞത് മനസ്സിലായി, നന്ദി. വേറെ എന്തെങ്കിലും സഹായം വേണോ?

## Validation and limits

- Workflow structural validation: zero errors, zero warnings. Full prompt validation passed.
- Compared eight synthetic scenarios before and after on `gpt-4.1` using the configured temperature and output limit. Intermediate failures prompted revisions to duplicate-question and storage-announcement examples. All eight scenarios passed the final text checks: purpose, negative experience, complaint-to-rating, low rating with a known reason, undecided reuse, suggestion already given, closing and explicit stop.
- Final checks covered output completion, a maximum of one question mark, selected unwanted expressions, Hindi-script leakage, required closing details and the observed suggestion storage claim. These are bounded text checks and manual review, not an independent native-speaker certification.
- The existing workflow interpreter emitted the authored Malayalam greeting, purpose, experience and rating prompts during an in-memory replay. A complete workflow replay did **not** pass: rating `4` reached the low-rating-reason node. This is an existing routing problem: both rating edges are unlabelled and the node's advisory `routing` instructions do not enforce that choice in this interpreter. Its routing was left unchanged and needs a separate workflow correction and regression review. Successful LLM response tests must not be mistaken for a successful complete workflow test.
- No new call recording, acoustic pronunciation, voice prosody, language-switch detection or end-to-end hang-up test was performed. The 8 October recording also shows hang-up and language-handling symptoms; the text update does not establish that those runtime symptoms are fixed.

Raw local/live snapshots, transcript excerpts and generated comparisons are retained under the ignored `.devrun/malayalam-wording-review/` directory. Reviewable prompt and workflow artifacts are in this directory. No runtime Python, frontend or provider implementation files were edited for this task.

The approach follows official OpenAI guidance on explicit instructions, examples and representative evaluations: [Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering).
