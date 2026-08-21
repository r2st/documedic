"""Curated clinical protocol templates — reusable order sets, as data.

A protocol template is the workup, the initial therapy and the follow-up interval for one
condition, written down once so it is not re-typed from memory at every consultation. The
content here is the same curated, guideline-grounded material ``app.core.pathways`` holds, and
it is separate from it for one reason: a pathway is *read*, and an order set is *applied*. The
first is reference text a clinician looks at; the second turns into medication events on a real
chart and a booking in a real diary.

That difference is the whole design.

**Every item is proposed, and every item can be deselected.** ``OrderSetService.apply`` takes
the keys the clinician chose. An order set that cannot be unticked is a checkbox that trains
people to tick, and this is the single highest-volume prescribing affordance in the product —
one click can chart five drugs. A template is a starting point for a decision, not the
decision.

**Nothing here is phrased as an instruction** (Critical Safety Rule #4). The labels read
"guidelines support considering" and never "start", for the same reason the pathway text does:
this is decision support, and the clinician prescribes.

**Doses are the *typical starting* dose and are marked as such.** They exist because a template
with no dose sends the clinician to a second screen to look one up, which is where the template
stops being used. They are not a recommendation for this patient: the applied medication passes
through the same deterministic dose-range check every other prescription does, against this
patient's age, weight and renal function, and the check is what decides whether the number is
usable. See ``OrderSetService.preview``.

**Versioning is a property of the record, not of this file.** ``VERSION`` moves whenever the
content changes, and every application stores the version it applied. A curated correction made
next month must not rewrite what a clinician ordered last month.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bumped whenever any template below changes: an item added or removed, a dose corrected, a
# follow-up interval altered. Stored on every ``protocol_applications`` row, so a later reader
# can tell what the clinician was actually offered rather than what the file says today.
#
# One version for the whole file rather than one per template. Per-template versions look
# tidier and are worse in practice: nothing would keep them moving, and a version that is
# usually right is a provenance record nobody can rely on.
VERSION = "2026.08.1"


@dataclass(frozen=True)
class InvestigationItem:
    """One investigation the template proposes.

    ``key`` is the stable identifier a clinician's selection names; ``marker_name`` is the lab
    marker it corresponds to where there is a single one, so that applying the set can be
    reconciled later against results that arrive. A panel with no single marker leaves it None
    rather than inventing one.
    """

    key: str
    label: str
    marker_name: str | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class MedicationItem:
    """One medication the template proposes, by INN generic name.

    Generic and never a brand: the vocabulary resolves Indian brands *to* this, and a curated
    template naming "Glycomet" would tie clinical content to one manufacturer's product.

    ``typical_dose``/``dose_unit``/``frequency`` are a common starting point and are checked
    like any other proposed dose when the set is applied — see the module docstring.
    """

    key: str
    generic_name: str
    typical_dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None
    route: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class FollowUpItem:
    """The review interval the template proposes, in days.

    Days rather than "6 weeks" because it is arithmetic the moment it is applied, and a
    human-readable interval parsed at apply time is a parser nobody needs.
    """

    key: str
    label: str
    interval_days: int
    appointment_type: str | None = None


@dataclass(frozen=True)
class OrderSet:
    """One curated protocol template."""

    key: str
    title: str
    condition_name: str
    # "icmr" where the content maps onto the real ICMR STW corpus, "curated" otherwise. Callers
    # must not present curated-only content as guideline-cited — the same rule
    # ``app.core.pathways`` states.
    source: str
    indication: str
    investigations: tuple[InvestigationItem, ...] = ()
    medications: tuple[MedicationItem, ...] = ()
    follow_ups: tuple[FollowUpItem, ...] = ()
    guideline_section_ids: tuple[str, ...] = ()

    def item_keys(self) -> tuple[str, ...]:
        """Every selectable key, in the order the template lists them."""
        return (
            tuple(item.key for item in self.investigations)
            + tuple(item.key for item in self.medications)
            + tuple(item.key for item in self.follow_ups)
        )


_ORDER_SETS: dict[str, OrderSet] = {
    "t2dm_initial_workup": OrderSet(
        key="t2dm_initial_workup",
        title="Type 2 diabetes — initial workup and first-line therapy",
        condition_name="Type 2 Diabetes Mellitus",
        source="icmr",
        indication=(
            "Guidelines support this baseline set where type 2 diabetes has been newly "
            "diagnosed or where a patient already carrying the diagnosis has had no recorded "
            "review within the year."
        ),
        investigations=(
            InvestigationItem(
                key="hba1c",
                label="HbA1c",
                marker_name="HbA1c",
                rationale=(
                    "Baseline glycaemic control, and the measure subsequent targets read against."
                ),
            ),
            InvestigationItem(
                key="creatinine",
                label="Serum creatinine with eGFR",
                marker_name="Creatinine",
                rationale=(
                    "Renal function bears directly on which agents are usable — metformin in "
                    "particular — and on the dose ceilings applied to them."
                ),
            ),
            InvestigationItem(
                key="lipid_profile",
                label="Fasting lipid profile",
                marker_name="LDL Cholesterol",
                rationale="Cardiovascular risk assessment alongside glycaemic control.",
            ),
            InvestigationItem(
                key="urine_acr",
                label="Urine albumin-to-creatinine ratio",
                rationale="Earliest marker of diabetic kidney disease.",
            ),
            InvestigationItem(
                key="fundus",
                label="Dilated fundus examination",
                rationale="Retinopathy screening from diagnosis in type 2 diabetes.",
            ),
        ),
        medications=(
            MedicationItem(
                key="metformin",
                generic_name="Metformin",
                typical_dose="500",
                dose_unit="mg",
                frequency="BD",
                route="oral",
                note=(
                    "Guidelines support considering metformin as first-line where renal "
                    "function permits. The renal contraindication check runs on application."
                ),
            ),
        ),
        follow_ups=(
            FollowUpItem(
                key="review_12_weeks",
                label="Review with repeat HbA1c",
                interval_days=84,
                appointment_type="diabetes_review",
            ),
        ),
        guideline_section_ids=("ICMR-DM2-DX", "ICMR-DM2-MGMT"),
    ),
    "hypertension_initial_workup": OrderSet(
        key="hypertension_initial_workup",
        title="Hypertension — baseline assessment and first-line therapy",
        condition_name="Hypertension",
        source="icmr",
        indication=(
            "Guidelines support this baseline set once a diagnosis of hypertension has been "
            "confirmed on readings taken on separate occasions."
        ),
        investigations=(
            InvestigationItem(
                key="creatinine",
                label="Serum creatinine with eGFR",
                marker_name="Creatinine",
                rationale="Target-organ assessment, and the basis of ACE-inhibitor monitoring.",
            ),
            InvestigationItem(
                key="potassium",
                label="Serum potassium",
                marker_name="Potassium",
                rationale=(
                    "Baseline before any renin-angiotensin agent or potassium-sparing diuretic."
                ),
            ),
            InvestigationItem(
                key="lipid_profile",
                label="Fasting lipid profile",
                marker_name="LDL Cholesterol",
                rationale="Cardiovascular risk assessment.",
            ),
            InvestigationItem(
                key="ecg",
                label="12-lead ECG",
                rationale="Left ventricular hypertrophy and rhythm as target-organ evidence.",
            ),
            InvestigationItem(
                key="urinalysis",
                label="Urinalysis",
                rationale="Proteinuria and haematuria as evidence of renal involvement.",
            ),
        ),
        medications=(
            MedicationItem(
                key="amlodipine",
                generic_name="Amlodipine",
                typical_dose="5",
                dose_unit="mg",
                frequency="OD",
                route="oral",
                note=(
                    "Guidelines support considering a calcium-channel blocker as one of the "
                    "first-line options; the choice between classes turns on age, comorbidity "
                    "and tolerance."
                ),
            ),
        ),
        follow_ups=(
            FollowUpItem(
                key="review_4_weeks",
                label="Review blood pressure and tolerance",
                interval_days=28,
                appointment_type="bp_review",
            ),
        ),
        guideline_section_ids=("ICMR-HTN-DX", "ICMR-HTN-MGMT"),
    ),
    "anaemia_workup": OrderSet(
        key="anaemia_workup",
        title="Anaemia — cause-finding workup",
        condition_name="Anaemia",
        source="curated",
        indication=(
            "Guidelines support establishing the cause before treating: iron supplementation "
            "given on a low haemoglobin alone will correct the number in several conditions "
            "whose cause needs finding, including occult gastrointestinal blood loss."
        ),
        investigations=(
            InvestigationItem(
                key="cbc",
                label="Complete blood count with indices",
                marker_name="Hemoglobin",
                rationale="Severity, and the red-cell indices that separate the broad causes.",
            ),
            InvestigationItem(
                key="ferritin",
                label="Serum ferritin",
                rationale=(
                    "Iron stores. Consider reading alongside an inflammatory marker — ferritin "
                    "is an acute-phase reactant and can read normal in inflammation with iron "
                    "deficiency present."
                ),
            ),
            InvestigationItem(
                key="b12_folate",
                label="Vitamin B12 and folate",
                rationale="Macrocytic causes.",
            ),
            InvestigationItem(
                key="reticulocyte",
                label="Reticulocyte count",
                rationale="Separates a marrow that is responding from one that is not.",
            ),
        ),
        # No medications. An anaemia set that proposed iron would be proposing treatment before
        # the cause is known, which is precisely what the indication above warns against.
        medications=(),
        follow_ups=(
            FollowUpItem(
                key="review_2_weeks",
                label="Review with results",
                interval_days=14,
                appointment_type="results_review",
            ),
        ),
    ),
    "thyroid_workup": OrderSet(
        key="thyroid_workup",
        title="Suspected thyroid dysfunction — workup",
        condition_name="Hypothyroidism",
        source="curated",
        indication=(
            "Guidelines support confirming biochemical thyroid status before considering "
            "replacement, and repeating an abnormal TSH before treating on a single result."
        ),
        investigations=(
            InvestigationItem(
                key="tsh",
                label="TSH",
                marker_name="TSH",
                rationale=(
                    "The first-line test; an isolated abnormal result is repeated before treating."
                ),
            ),
            InvestigationItem(
                key="free_t4",
                label="Free T4",
                rationale="Separates overt from subclinical disease where TSH is abnormal.",
            ),
            InvestigationItem(
                key="tpo_antibodies",
                label="Anti-TPO antibodies",
                rationale="Autoimmune aetiology, where the answer would change management.",
            ),
        ),
        medications=(),
        follow_ups=(
            FollowUpItem(
                key="review_6_weeks",
                label="Review with results",
                interval_days=42,
                appointment_type="results_review",
            ),
        ),
    ),
}


def all_order_sets() -> tuple[OrderSet, ...]:
    """Every template, in a stable order so a list read renders the same way twice."""
    return tuple(_ORDER_SETS[key] for key in sorted(_ORDER_SETS))


def get_order_set(key: str) -> OrderSet | None:
    """One template by key, or None. Case- and whitespace-insensitive on the key."""
    return _ORDER_SETS.get(key.strip().lower())


def order_sets_for_condition(condition_name: str) -> tuple[OrderSet, ...]:
    """Templates whose condition matches, folded the way condition names are matched elsewhere.

    Substring rather than equality in one direction only: a chart carrying "Type 2 Diabetes
    Mellitus (on metformin)" should match the diabetes template, while a template must never
    match a condition merely because the template's name is long. See
    ``app.core.safety``'s condition matching for the same asymmetry and why it exists.
    """
    folded = " ".join(condition_name.split()).casefold()
    if not folded:
        return ()
    return tuple(
        order_set for order_set in all_order_sets() if order_set.condition_name.casefold() in folded
    )
