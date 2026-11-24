"""Question ownership for generated speech, separate from caller routing.

Caller routing deliberately recognizes wh-words anywhere. Generated answers
also contain embedded wh-clauses ("I do not know where it happened"), so that
wide caller heuristic cannot by itself identify a new spoken question.
"""

import re

from shared.orchestration import lang
from shared.orchestration.router import looks_like_question


_WH = re.compile(
    r"(?<!\w)(?:" + lang.alternatives("question_markers") + r")(?!\w)", re.I,
)
_AUX_QUESTION = re.compile(
    r"^\W*(?:" + lang.alternatives("question_aux") + r")\s+(?:"
    + lang.alternatives("question_subjects") + r")(?!\w)", re.I,
)
_REQUEST = re.compile(
    r"^\W*(?:please\s+)?(?:tell|confirm|let me know|provide|share|give|explain)\b"
    r"|^\W*(?:बताइए|बताइये|बताना|बताओ|दीजिए|दीजिये|bataiye|batao|batana|dijiye)(?![\wऀ-ॿ])",
    re.I,
)
# Linguistic predicates, not business facts: the subject reports its knowledge
# or the availability of information. Both sides of a comma can participate
# in one declarative sentence in Hindi/Hinglish as well as English.
_KNOWLEDGE_STATEMENT = re.compile(
    r"^\W*(?:i|we)\s+(?:(?:do|did|can|could)\s+not\s+|cannot\s+|"
    r"(?:don't|don’t|didn't|didn’t|can't|can’t|couldn't|couldn’t)\s+)?"
    r"(?:know|understand|remember|have\s+(?:no\s+)?(?:information|details)|confirm|say)\b"
    r"|^\W*(?:the\s+)?(?:information|details|location|record|context)\b.{0,160}"
    r"\b(?:is|was|are|were|does|do)\s+(?:not\s+)?(?:available|known|recorded|specified|clear|show)\b"
    r"|^\W*(?:मुझे|हमें)\s+.{0,100}(?:पता|जानकारी|मालूम|ज्ञात)(?![\wऀ-ॿ])"
    r"|^\W*(?:मेरे|हमारे)\s+पास\s+.{0,100}(?:जानकारी|विवरण|पता)(?![\wऀ-ॿ])"
    r"|^\W*(?:इसकी|उसकी|इसके|उसके|इस|उस)\s+(?:जानकारी|विवरण|बारे)\s+"
    r"(?:(?:मेरे|हमारे)\s+पास|उपलब्ध|दर्ज|ज्ञात|स्पष्ट|नहीं)(?![\wऀ-ॿ])"
    r"|^\W*(?:mujhe|hamein|humein)\s+.{0,100}(?:pata|jaankari|jankari|maloom)\b"
    r"|^\W*(?:mere|hamare)\s+paas\s+.{0,100}(?:pata|jaankari|jankari|details|information)\b"
    r"|^\W*(?:iski|uski|iske|uske)\s+(?:jaankari|jankari|details|information)\s+"
    r"(?:(?:mere|hamare)\s+paas|available|nahi|nahin)\b",
    re.I,
)


def is_generated_question_clause(clause: str, following_clause: str = "") -> bool:
    """Whether this generated clause asks the caller a new question/value.

    ``following_clause`` allows an embedded-wh subject followed by its main
    declarative predicate: "Where it happened, I do not know." Explicit
    question marks, interrogative auxiliary openings and requests win.
    """
    clause = clause.strip()
    if any(mark in clause for mark in "?？؟") or _REQUEST.search(clause) or _AUX_QUESTION.search(clause):
        return True
    if _WH.search(clause) and (
        _KNOWLEDGE_STATEMENT.search(clause)
        or (_KNOWLEDGE_STATEMENT.search(following_clause.strip())
            and not any(mark in following_clause for mark in "?？؟"))
    ):
        return False
    return looks_like_question(clause)
