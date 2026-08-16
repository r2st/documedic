"""Clinical handover-summary response shapes.

The ordering of the fields on :class:`ClinicalSummaryResponse` is deliberate and is the same
argument the Reasoning Theatre makes: the charted facts come before the paragraph written from
them, so a client that renders the model in declaration order shows the evidence first
(Critical Safety Rule #6). A client is free to lay it out differently; nothing here can stop
that, but the default shape should not be the one that leads with the conclusion.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# How the narrative was produced. ``model`` is the only value that means a provider wrote it;
# every other value means the clinician is looking at the chart restated, which is a different
# thing to trust and is why the enum is on the wire rather than only in a boolean.
SummarySource = Literal["model", "deterministic", "empty"]


class SummaryChart(BaseModel):
    """The charted facts the summary was written from — the evidence half of the response."""

    age_years: int | None = None
    sex: str | None = None
    active_conditions: list[dict[str, Any]] = Field(default_factory=list)
    current_medications: list[dict[str, Any]] = Field(default_factory=list)
    recent_labs: list[dict[str, Any]] = Field(default_factory=list)
    recent_encounters: list[dict[str, Any]] = Field(default_factory=list)
    allergies: list[dict[str, Any]] = Field(default_factory=list)


class SummaryNarrative(BaseModel):
    """The written half. Every string has been through the deterministic Rule #4 control."""

    overview: str = ""
    active_problems: list[str] = Field(default_factory=list)
    current_medications: list[str] = Field(default_factory=list)
    recent_investigations: list[str] = Field(default_factory=list)
    recent_encounters: list[str] = Field(default_factory=list)
    # What a clinician reading this chart cannot tell from it. Computed deterministically as
    # well as asked of the model, because a gap is an absence and noticing what is *not* in a
    # block of text is the least reliable thing a model can be asked for.
    record_gaps: list[str] = Field(default_factory=list)


class ClinicalSummaryResponse(BaseModel):
    patient_id: uuid.UUID
    generated_at: datetime
    chart: SummaryChart
    summary: SummaryNarrative
    source: SummarySource
    # True whenever the narrative did not come from a live provider. A clinician reading a
    # handover paragraph has to be able to tell a summary of their patient from a restatement
    # produced with reasoning offline.
    degraded: bool
    # Whether the deterministic prescriber-framing control had to rewrite what the model wrote.
    # The only place a provider ignoring its framing instructions is visible.
    prescriber_framing_applied: bool
