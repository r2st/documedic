"""Prescribing rules that depend on the *format* of the consultation. No LLM, no I/O.

A teleconsultation is not an in-person consultation conducted at a distance. Some of what a
prescriber ordinarily has is simply absent: they cannot lay hands on the patient, they cannot
take a blood sample there and then, and on an audio-only call they cannot see them at all. Every
rule here is a consequence of one of those three absences, and none of them is a rule about the
drug in isolation — the same drug, prescribed in clinic, raises nothing.

India's Telemedicine Practice Guidelines (2020, appended to the Indian Medical Council
regulations) draw the same line and are the reason this module exists as a distinct check rather
than as a footnote in ``app.core.safety``. They tier medicines by what may be prescribed at
what kind of consultation, and they make the first-consultation/follow-up distinction that
everything below turns on.

**Initiation, not continuation.** Every check here fires only when the proposed drug is *not*
already on the patient's chart. Continuing warfarin for a patient who has been on it for three
years is ordinary remote care and flagging it would be noise on every follow-up call in the
country; *starting* warfarin on a video call, for a patient whose baseline INR nobody has, is
the act these rules are about. This distinction is the whole design, and it is why the checks
take a ``SafetyContext`` rather than just a drug.

**Nothing here is a hard block.** A hard block is a refusal, and refusing to let a clinician
prescribe over a teleconsultation strands a patient who may have no other access to a
prescriber — which in this product's market is the ordinary case rather than the exception, and
is the reason teleconsultation is regulated *into* existence rather than out of it. These are
flags a clinician weighs. Hard blocks remain what they have always been: a documented allergy
and an absolute contraindication (Critical Safety Rule #3).

The wording follows Rule #4 throughout: these say what this consultation format cannot provide,
never what the clinician should do about it.
"""

from __future__ import annotations

from typing import Literal

from app.core.safety import DrugRef, SafetyContext, SafetyFlag

# How the consultation is being conducted. ``in_person`` is here so that callers can pass the
# encounter's actual modality without branching first, and so that "this was checked and the
# format imposed nothing" is representable — distinct from "nobody said what format this was".
TeleconsultationModality = Literal["video", "audio", "in_person"]

# Drugs whose administration is a physical act nobody can perform down a telephone line. Not a
# judgement about the drug — ceftriaxone is a first-line antibiotic — but about what a remote
# consultation can and cannot conclude with. Keyed on reference id because that is what the
# vocabulary carries and what a combination product's ingredients resolve to.
_REQUIRES_IN_PERSON: dict[str, str] = {
    "CTX-1G": "given by injection, which needs someone present to administer it",
    "CONTRAST-IODINE": "given during an imaging procedure, in a department",
}

# Drugs whose safe *initiation* rests on a measurement taken before the first dose. A remote
# consultation can order the test; what it cannot do is have the result in hand at the moment
# the prescription is written, which is when these particular drugs need it.
#
# All five are narrow-therapeutic-index or actively monitored agents where the first dose is
# chosen from a baseline the record may not hold: warfarin from an INR, lithium from renal
# function and thyroid, digoxin from potassium and renal function, phenytoin from liver
# function, methotrexate from a full blood count and liver function.
_REQUIRES_BASELINE_MONITORING: dict[str, str] = {
    "WARF-5": "dosing starts from a baseline INR",
    "LIT-400": "dosing starts from baseline renal and thyroid function, and needs level checks",
    "DIG-0.25": "dosing starts from baseline potassium and renal function",
    "PHT-100": "a narrow therapeutic index; dosing starts from baseline liver function",
    "MTX-7.5": "dosing starts from a baseline full blood count and liver function",
}

TelehealthCheckType = Literal[
    "telehealth_in_person_required",
    "telehealth_baseline_monitoring_required",
    "telehealth_audio_only_initiation",
]


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _identities(drug: DrugRef) -> tuple[DrugRef, ...]:
    """The product and each ingredient it contains.

    Combinations are matched by their components for the same reason every other check in this
    codebase is: a rule keyed on a molecule and a product row keyed on a formulation are not the
    same key, and comparing them directly is how a curated rule finds nothing.
    """
    return (drug, *drug.components)


def _is_already_charted(drug: DrugRef, ctx: SafetyContext) -> bool:
    """Whether this patient is already on this drug — the initiation/continuation test.

    Matched on reference id *and* normalised generic name, because one molecule has several
    vocabulary rows (warfarin at two strengths is two rows). Keyed on the id alone, a patient
    stable on one strength would have a strength change read as an initiation, which is the
    exact false positive that makes a format-dependent flag stop being read.
    """
    charted_ids = {_norm(m.reference_id) for m in ctx.current_meds if m.reference_id}
    charted_names = {_norm(m.generic_name) for m in ctx.current_meds if m.generic_name}
    for part in _identities(drug):
        if part.reference_id and _norm(part.reference_id) in charted_ids:
            return True
        if _norm(part.generic_name) and _norm(part.generic_name) in charted_names:
            return True
    return False


def check_teleconsultation_prescribing(
    proposed: DrugRef,
    ctx: SafetyContext,
    modality: TeleconsultationModality | None,
) -> list[SafetyFlag]:
    """What this consultation format cannot supply for this particular prescription.

    Returns nothing at all for an in-person consultation, for a caller that did not say what
    format this is, and — importantly — for any drug the patient is already on. The last is not
    a leniency: continuing established therapy remotely is the ordinary, intended use of a
    teleconsultation, and a check that flagged it would fire on every follow-up call.

    ``None`` and ``"in_person"`` are treated the same, deliberately. A caller who did not say
    is not asserting that the consultation was remote, and inventing a restriction from silence
    would put format-dependent flags on every in-clinic prescription in the system.
    """
    if modality not in ("video", "audio"):
        return []
    if _is_already_charted(proposed, ctx):
        return []

    flags: list[SafetyFlag] = []

    for part in _identities(proposed):
        reason = _REQUIRES_IN_PERSON.get(part.reference_id)
        if reason is None:
            continue
        flags.append(
            SafetyFlag(
                check_type="telehealth_in_person_required",
                severity="critical",
                is_hard_block=False,
                summary=(
                    f"{_label(proposed, part)} is {reason} — this consultation is remote, so "
                    "the first dose cannot be given as part of it."
                ),
                details=_details(proposed, part, modality),
            )
        )
        break

    for part in _identities(proposed):
        reason = _REQUIRES_BASELINE_MONITORING.get(part.reference_id)
        if reason is None:
            continue
        flags.append(
            SafetyFlag(
                check_type="telehealth_baseline_monitoring_required",
                severity="critical",
                is_hard_block=False,
                summary=(
                    f"Starting {_label(proposed, part)} remotely: {reason}, which a remote "
                    "consultation cannot obtain before the prescription is written."
                ),
                details=_details(proposed, part, modality),
            )
        )
        break

    if modality == "audio":
        # The one rule that is about the modality rather than the drug. India's Telemedicine
        # Practice Guidelines allow a first prescription of a new medicine at a *video*
        # consultation and not at an audio-only one, and the reasoning is plain: on a telephone
        # call the prescriber has neither examined nor seen the patient.
        #
        # Deliberately raised for *every* initiation rather than for a curated list. A curated
        # list would be a claim about which new drugs are safe to start sight-unseen, and this
        # module is not in a position to make that claim; what it can say accurately is that
        # nothing was seen.
        flags.append(
            SafetyFlag(
                check_type="telehealth_audio_only_initiation",
                severity="warning",
                is_hard_block=False,
                summary=(
                    f"{proposed.generic_name} is not among this patient's current medications, "
                    "and this consultation is audio-only — the patient has not been seen. "
                    "Guidelines support starting a new medicine at a video consultation rather "
                    "than a telephone one."
                ),
                details={"proposed_drug": proposed.generic_name, "modality": "audio"},
            )
        )

    return flags


def _label(product: DrugRef, part: DrugRef) -> str:
    """Name the product the clinician is prescribing, and the component if a rule matched one.

    A warning about "Warfarin" on a prescription that says something else reads as being about
    a different drug — the same reason ``app.core.safety._Ingredient.label`` exists.
    """
    if part is product:
        return product.generic_name
    return f"{product.generic_name} (via its {part.generic_name} component)"


def _details(product: DrugRef, part: DrugRef, modality: str) -> dict:
    details: dict = {"proposed_drug": product.generic_name, "modality": modality}
    if part is not product:
        details["component"] = part.generic_name
        details["component_reference_id"] = part.reference_id
    return details
