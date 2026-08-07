"""Post-call intelligence: structured conversation memory + Next Best Action.

One bounded LLM analysis per completed call produces a validated
:class:`~shared.post_call.schema.PostCallAnalysis`; a deterministic,
configuration-driven layer (:mod:`shared.post_call.nba`) reconciles the
proposed Next Best Action with the bot's goal policy and the call's verified
state. Persistence and scheduling live in :mod:`shared.post_call.processor`;
loading the memory into the NEXT call lives in :mod:`shared.post_call.recall`.
"""
