"""CaseState — the shared state object that flows through every agent (architecture §3.1).

Plain dataclasses (no LLM/ORM coupling) so the whole reasoning state is serialisable to JSON
for the immutable ``reasoning_sessions.case_state`` snapshot and streamable over SSE. Enums use
the canonical string values from ``app.schemas.common``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Canonical value sets (documented as Literals). Dataclass fields below are typed ``str``
# because their values are produced by normalisers (``norm_band``) and the verifier/LLM at
# runtime; the boundary code guarantees they are one of these values.
ProbabilityBand = Literal["high", "moderate", "low", "very_low", "insufficient_data"]
AutonomyTier = Literal["informational", "suggestive", "flag_for_review"]
VerifierStatus = Literal["agree", "partial_disagreement", "major_disagreement"]

# Ordering used for "conservative wins": higher index = more conservative / more cautious.
_BAND_ORDER = ["high", "moderate", "low", "very_low", "insufficient_data"]
_TIER_ORDER = ["informational", "suggestive", "flag_for_review"]


def more_conservative_tier(a: str, b: str) -> str:
    """Return the more conservative (higher-stakes) of two autonomy tiers."""
    return a if _TIER_ORDER.index(a) >= _TIER_ORDER.index(b) else b


def downgrade_band(band: str) -> str:
    """Move a probability band one step toward less certainty (conservative resolution)."""
    idx = _BAND_ORDER.index(band) if band in _BAND_ORDER else len(_BAND_ORDER) - 1
    return _BAND_ORDER[min(idx + 1, len(_BAND_ORDER) - 1)]


@dataclass
class Evidence:
    """A single piece of evidence, grounded in patient data or a guideline."""

    text: str
    supports: bool  # True = evidence-for, False = evidence-against
    source: str = "patient_data"  # patient_data | guideline | clinical_knowledge
    source_ref: str | None = None  # e.g. lab id, condition name, section_id


@dataclass
class IntakeQuestionState:
    text: str
    question_type: str = "clarifying"
    rationale: str | None = None
    info_gain_score: float = 0.5
    answer: str | None = None


@dataclass
class Hypothesis:
    diagnosis_name: str
    icd_code: str | None = None
    probability_band: str = "low"
    evidence_for: list[Evidence] = field(default_factory=list)
    evidence_against: list[Evidence] = field(default_factory=list)
    cant_miss_flag: bool = False
    source_agent: str = "primary_care"
    rationale: str | None = None
    # Devil's-advocate critique attached to leading hypotheses.
    devil_advocate: dict[str, Any] | None = None


@dataclass
class Investigation:
    name: str
    rationale: str
    expected_information_gain: str = "moderate"
    cost_estimate: str | None = None
    availability_tier: str = "phc"  # phc | chc | district_hospital | referral


@dataclass
class GuidelineChunkRef:
    section_id: str
    source: str
    document_title: str
    heading: str | None
    snippet: str
    score: float
    corpus_version: str
    page_range: str | None = None


@dataclass
class ManagementOption:
    text: str
    citations: list[GuidelineChunkRef] = field(default_factory=list)
    autonomy_tier: str = "suggestive"
    sufficient_support: bool = True
    # Deterministic drug-safety flags raised against the drugs this option *names*, filled in by
    # the drug-safety node after guideline retrieval. A guideline is written for a population;
    # the conflict is with this patient, so it can only be found once both are in hand.
    safety_flags: list[dict[str, Any]] = field(default_factory=list)

    def has_hard_block(self) -> bool:
        return any(f.get("is_hard_block") for f in self.safety_flags)


@dataclass
class VerifierVerdict:
    target: str  # what was checked (hypothesis name / "management" / "case")
    status: str = "agree"
    rationale: str = ""
    caveats: list[str] = field(default_factory=list)


@dataclass
class HardBlock:
    summary: str
    check_type: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentMessage:
    agent: str
    role: str  # status | hypothesis | critique | verdict | error
    content: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceEntry:
    agent: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseState:
    # Patient context
    patient_id: str
    presenting_complaint: str
    patient_graph_snapshot: dict[str, Any] = field(default_factory=dict)

    # Intake
    intake_questions: list[IntakeQuestionState] = field(default_factory=list)
    intake_complete: bool = False
    info_gain_score: float = 1.0
    intake_rounds: int = 0

    # Hypotheses
    hypothesis_set: list[Hypothesis] = field(default_factory=list)

    # Guideline retrieval / management
    management_options: list[ManagementOption] = field(default_factory=list)
    citation_faithfulness: float | None = None

    # Investigation
    recommended_investigations: list[Investigation] = field(default_factory=list)

    # Drug safety
    hard_blocks: list[HardBlock] = field(default_factory=list)
    drug_safety_flags: list[dict[str, Any]] = field(default_factory=list)

    # Verification
    verifier_verdicts: list[VerifierVerdict] = field(default_factory=list)
    autonomy_tier: str = "informational"
    verifier_status: str = "agree"

    # Trace
    agent_messages: list[AgentMessage] = field(default_factory=list)
    agent_trace: list[TraceEntry] = field(default_factory=list)

    # Metadata
    case_id: str = ""
    online: bool = True
    degraded: bool = False  # True when LLM unavailable and deterministic fallback used
    demo_mode: bool = False  # True when output is built from simulated "[DEMO MODE]" data
    # Agents that raised rather than answering. Named rather than counted, because "the
    # Devil's-Advocate never ran" and "the guideline retrieval never ran" are different things
    # to hand a clinician. Written by ``graph._advisory``; read by the Verifier's deterministic
    # floor, which escalates the case and says which lane is missing.
    failed_agents: list[str] = field(default_factory=list)

    def record_agent_failure(self, agent: str, reason: str) -> None:
        """Note that ``agent`` raised, and degrade the case for it.

        Three things at once because they must not come apart: the lane is named so the
        Verifier can escalate and the Theatre can show it, the case is marked ``degraded`` so
        the deterministic floor treats the run as it treats an LLM outage, and the failure is
        put on the trace and the message log so it is in the immutable ``case_state`` snapshot
        rather than only in the server's logs.
        """
        if agent not in self.failed_agents:
            self.failed_agents.append(agent)
        self.degraded = True
        self.add_trace(agent, f"Agent failed and did not contribute: {reason}", {"error": reason})
        self.add_message(agent, "error", f"This agent did not complete: {reason}")

    def add_trace(self, agent: str, summary: str, detail: dict[str, Any] | None = None) -> None:
        self.agent_trace.append(TraceEntry(agent=agent, summary=summary, detail=detail or {}))

    def add_message(
        self, agent: str, role: str, content: str, data: dict[str, Any] | None = None
    ) -> None:
        self.agent_messages.append(
            AgentMessage(agent=agent, role=role, content=content, data=data or {})
        )

    def leading_hypotheses(self, n: int = 3) -> list[Hypothesis]:
        order = {b: i for i, b in enumerate(_BAND_ORDER)}
        ranked = sorted(self.hypothesis_set, key=lambda h: order.get(h.probability_band, 99))
        return ranked[:n]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "patient_id": self.patient_id,
            "presenting_complaint": self.presenting_complaint,
            "online": self.online,
            "degraded": self.degraded,
            "demo_mode": self.demo_mode,
            "failed_agents": list(self.failed_agents),
            "intake_complete": self.intake_complete,
            "info_gain_score": self.info_gain_score,
            "intake_rounds": self.intake_rounds,
            "intake_questions": [asdict(q) for q in self.intake_questions],
            "hypothesis_set": [asdict(h) for h in self.hypothesis_set],
            "management_options": [asdict(m) for m in self.management_options],
            "citation_faithfulness": self.citation_faithfulness,
            "recommended_investigations": [asdict(i) for i in self.recommended_investigations],
            "hard_blocks": [asdict(b) for b in self.hard_blocks],
            "drug_safety_flags": self.drug_safety_flags,
            "verifier_verdicts": [asdict(v) for v in self.verifier_verdicts],
            "verifier_status": self.verifier_status,
            "autonomy_tier": self.autonomy_tier,
            "agent_trace": [asdict(t) for t in self.agent_trace],
            "agent_messages": [asdict(m) for m in self.agent_messages],
        }
