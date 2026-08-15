"""Deterministic enforcement of prescriber framing on model-written clinical text.

CLAUDE.md Critical Safety Rule #4 says no clinical output is ever presented as certain, and
that imperative clinical language is a bug rather than a style preference. Until now the only
thing enforcing it was the paragraph of instructions every agent prompt carries
(``app.agents.prompts._SAFETY_FRAMING``), and ``regulatory_service`` published that paragraph
as the *control* for the rule. A prompt is not a control. It is a request, made of a model that
is picked at deployment time from whatever provider answers — in this deployment, often a small
free one — and the failure mode is silent: "Give aspirin 300 mg" is a fluent, plausible,
well-formatted string that satisfies every other check in the pipeline. Nothing between the
provider socket and the clinician's screen looked at the modality of the sentence.

So this module is the control: a pure, offline, deterministic rewrite applied at the one point
where model-written text becomes a ``ClinicalSuggestion``. It runs with no network and no LLM,
which Rule #8 requires of anything the deterministic path depends on, and it is a pure function
of its input so it can be tested exhaustively.

**What it changes is modality, never content.** Every rule below rewrites the verb phrase that
asserts or commands and leaves the clinical substance — the drug, the dose, the diagnosis —
untouched and in place. "Give aspirin 300 mg" becomes "Guidelines support considering aspirin
300 mg"; "The patient has sepsis" becomes "Findings are consistent with sepsis". A rewrite that
dropped or reworded the clinical noun phrase would be a far worse bug than the one being fixed,
so the patterns are anchored, narrow, and only ever replace a leading verb phrase.

**Scope: titles and bodies, not evidence.** Applied to the text that carries a *conclusion or
recommendation* and deliberately not to evidence lines, where "the patient has crushing chest
pain" is an accurate record of a symptom rather than a certainty claim about a diagnosis.
Reframing a finding into a hedge would make the record less true, not more careful. See
``app.agents.synthesis`` for the call sites.

**Not an escalation.** When a rewrite fires, the suggestion records that it did (visible in the
Theatre and on the audit trail) but keeps its autonomy tier. Rule #2's "conservative wins" is
about agents disagreeing on clinical risk; how a model phrased a sentence is not evidence about
the stakes of the case, and escalating on it would fill the flag-for-review tier — the tier that
demands active clinician engagement — with phrasing defects until it stopped being read.
"""

from __future__ import annotations

import re

# Each rule is (pattern, replacement). Applied in order, once per rule, to the whole string.
#
# Every pattern is anchored to a sentence boundary — the start of the string or after
# ``.``/``;``/``:``/newline — because the target is the *modality of a clause*, and an
# unanchored match would rewrite the middle of a sentence that was already correctly hedged
# ("evidence suggests we should give aspirin" must not become two hedges deep). ``\b`` alone is
# not enough for that: it matches inside a clause perfectly happily.
_SENTENCE_START = r"(?:(?<=^)|(?<=[.;:!?]\s)|(?<=\n))"

# The leading capital is preserved by rewriting to a capitalised replacement and letting the
# sentence-start anchor guarantee that is where we are.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # --- Imperative prescribing. The single most dangerous form: it reads as an order. ---
    (
        re.compile(
            _SENTENCE_START + r"(?:give|administer|prescribe|dispense|initiate)\s+", re.IGNORECASE
        ),
        "Guidelines support considering ",
    ),
    (
        re.compile(
            _SENTENCE_START + r"start\s+(?:the\s+)?(?:patient\s+)?(?:on\s+)?", re.IGNORECASE
        ),
        "Guidelines support considering ",
    ),
    (
        re.compile(_SENTENCE_START + r"stop\s+(?!considering)", re.IGNORECASE),
        "Guidelines support considering stopping ",
    ),
    # --- Imperative diagnosis. ---
    (
        re.compile(
            _SENTENCE_START + r"diagnose\s+(?:the\s+patient\s+)?(?:with\s+)?", re.IGNORECASE
        ),
        "Findings are consistent with ",
    ),
    # --- Certainty about the patient. ---
    (
        re.compile(
            _SENTENCE_START + r"(?:the\s+)?patient\s+(?:has|is\s+suffering\s+from)\s+",
            re.IGNORECASE,
        ),
        "Findings are consistent with ",
    ),
    (
        re.compile(_SENTENCE_START + r"(?:the\s+)?diagnosis\s+is\s+(?!consistent)", re.IGNORECASE),
        "Findings are consistent with ",
    ),
    (
        re.compile(
            _SENTENCE_START + r"this\s+is\s+(?:definitely|certainly|clearly)\s+", re.IGNORECASE
        ),
        "Findings are consistent with ",
    ),
)

# Certainty adverbs, removed wherever they appear rather than only at a sentence start: unlike
# the clause rewrites above these carry no clinical content at all, so deleting one cannot lose
# anything. "This is definitely a STEMI" is handled by a rule above; "it is definitely a STEMI"
# is not, and the adverb is what makes either sentence a certainty claim.
_CERTAINTY_ADVERBS = re.compile(
    r"\b(?:definitely|certainly|undoubtedly|unquestionably|without\s+doubt|"
    r"beyond\s+doubt|conclusively)\s+",
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r"[ \t]{2,}")


def prescriber_framed(text: str) -> tuple[str, bool]:
    """``(rewritten, changed)`` — ``text`` with imperative/certainty modality removed.

    ``changed`` is what the caller records; it is the signal that a model ignored its framing
    instructions on this particular output, which is worth knowing about a provider even though
    the text itself has already been corrected.

    Idempotent: running the result back through this function changes nothing, which is what
    makes it safe to apply at more than one layer as the pipeline grows.
    """
    if not text:
        return text, False
    result = text
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)
    result = _CERTAINTY_ADVERBS.sub("", result)
    result = _WHITESPACE.sub(" ", result).strip()
    return result, result != text


def has_certainty_language(text: str) -> bool:
    """Whether ``text`` asserts or commands in a way Rule #4 forbids.

    The predicate half of :func:`prescriber_framed`, for tests and for callers that want to
    report rather than rewrite.
    """
    _, changed = prescriber_framed(text)
    return changed
