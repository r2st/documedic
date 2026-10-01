"""CDSCO SaMD regulatory-artifact generation (Phase 4).

Assembles a Software-as-a-Medical-Device technical dossier from live system metadata: intended
use, risk-management controls (the non-negotiable safety rules and how each is enforced),
audit-trail integrity (recomputed hash chain), the latest clinical-validation metrics, and data
governance (DPDP Act). Returned as structured JSON and rendered to Markdown for submission.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.validation import ValidationRun
from app.services.audit_service import AuditService

# Each non-negotiable safety rule mapped to the artefact that enforces it.
RISK_CONTROLS = [
    {
        "rule": "Verifier is mandatory and cannot be bypassed",
        "control": "Verifier node sits on every path to synthesis in app/agents/graph.py; "
        "no edge reaches output without it. Test: clinical suggestions always carry a verdict.",
    },
    {
        "rule": "Conservative output wins on disagreement",
        "control": "more_conservative_tier() + conservative_resolution node downgrade bands and "
        "escalate the autonomy tier whenever the verifier disagrees.",
    },
    {
        "rule": "Allergy/contraindication conflicts are deterministic hard blocks",
        "control": "app/core/safety.py runs offline; hard blocks are surfaced as immutable "
        "safety suggestions and require documented reasoning to override.",
    },
    {
        "rule": "No certainty language in clinical output",
        "control": "app/core/clinical_language.py rewrites imperative and certainty phrasing "
        "out of every model-written title and body at the synthesis chokepoint, offline and "
        "deterministically; agent prompts ask for the same framing and qualitative probability "
        "bands (HIGH/MODERATE/LOW/VERY_LOW) rather than numeric probabilities.",
    },
    {
        "rule": "Devil's-advocate dissent is always shown",
        "control": "Critique is generated every run and rendered non-collapsibly in the UI.",
    },
    {
        "rule": "Evidence before conclusion (anti-automation-bias)",
        "control": "Suggestion payloads order evidence-for/against ahead of the assessment; the "
        "UI renders evidence first and gates flag-for-review outputs behind active engagement.",
    },
    {
        "rule": "ClinicalSuggestion records are immutable",
        "control": "clinical_suggestions has a Postgres BEFORE UPDATE/DELETE trigger; "
        "corrections are new rows; clinician decisions are append-only.",
    },
    {
        "rule": "Offline safety checks work without the LLM",
        "control": "Drug-safety engine and can't-miss rule table are deterministic; reasoning "
        "degrades to an explicit degraded mode rather than producing silent output.",
    },
]


class RegulatoryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def build_dossier(self, account_id: uuid.UUID) -> dict:
        chain_len, chain_valid = await self.audit.verify_full_chain()
        latest = (
            await self.db.execute(
                select(ValidationRun)
                .where(ValidationRun.account_id == account_id)
                .order_by(ValidationRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        return {
            "document": "CDSCO SaMD Technical Dossier (auto-generated)",
            "generated_at": datetime.now(UTC).isoformat(),
            "product": {
                "name": "DoAide Med",
                "intended_use": (
                    "Clinician-facing diagnostic and management decision-support for primary "
                    "care in India. Supports, never replaces, clinician judgement; issues no "
                    "autonomous prescriptions."
                ),
                "samd_classification": "Class B/C (SaMD; drives clinical management decisions)",
                "target_users": "Registered primary-care clinicians",
                "operating_context": (
                    "Intermittent connectivity; deterministic safety checks remain available "
                    "offline."
                ),
            },
            "risk_management": {
                "framework": "ISO 14971-aligned; non-negotiable safety rules as risk controls",
                "controls": RISK_CONTROLS,
            },
            "audit_integrity": {
                "scheme": "SHA-256 hash-chained, append-only audit_logs (tamper-evident)",
                "entries_verified": chain_len,
                "chain_valid": chain_valid,
            },
            "clinical_validation": {
                "method": "Automated vignette harness through the full reasoning pipeline",
                "latest_run_id": str(latest.id) if latest else None,
                "vignette_count": latest.vignette_count if latest else 0,
                "metrics": latest.metrics if latest else None,
                "citation_faithfulness_target": settings.citation_faithfulness_target,
            },
            "data_governance": {
                "regulation": "Digital Personal Data Protection (DPDP) Act 2023",
                "controls": [
                    "Explicit consent captured before clinical data is stored",
                    "Data minimisation and purpose limitation",
                    "Data residency in India for production",
                    "Soft-delete with audit-trail retention (right to erasure)",
                ],
            },
            "known_limitations": [
                "Guideline corpus is a curated subset; absence of a citation is not absence of "
                "evidence.",
                "Degraded (offline) mode uses deterministic reasoning and is flagged for review.",
                "Validation vignettes are synthetic and not a substitute for a clinical study.",
            ],
        }

    async def render_markdown(self, account_id: uuid.UUID) -> str:
        d = await self.build_dossier(account_id)
        p = d["product"]
        lines = [
            f"# {d['document']}",
            "",
            f"_Generated: {d['generated_at']}_",
            "",
            "## 1. Product and Intended Use",
            f"- **Name:** {p['name']}",
            f"- **Intended use:** {p['intended_use']}",
            f"- **SaMD classification:** {p['samd_classification']}",
            f"- **Target users:** {p['target_users']}",
            f"- **Operating context:** {p['operating_context']}",
            "",
            "## 2. Risk Management Controls",
        ]
        for c in d["risk_management"]["controls"]:
            lines.append(f"- **{c['rule']}** — {c['control']}")
        ai = d["audit_integrity"]
        lines += [
            "",
            "## 3. Audit-Trail Integrity",
            f"- Scheme: {ai['scheme']}",
            f"- Entries verified: {ai['entries_verified']}",
            f"- Chain valid: {ai['chain_valid']}",
            "",
            "## 4. Clinical Validation",
        ]
        cv = d["clinical_validation"]
        lines.append(f"- Method: {cv['method']}")
        lines.append(f"- Vignettes: {cv['vignette_count']}")
        if cv["metrics"]:
            for k, v in cv["metrics"].items():
                lines.append(f"  - {k}: {v}")
        else:
            lines.append("  - No validation run on record yet — run POST /validation/run.")
        lines += ["", "## 5. Data Governance (DPDP Act)"]
        lines += [f"- {c}" for c in d["data_governance"]["controls"]]
        lines += ["", "## 6. Known Limitations"]
        lines += [f"- {limitation}" for limitation in d["known_limitations"]]
        return "\n".join(lines)

    async def record_generation(self, account_id: uuid.UUID) -> None:
        await self.audit.record(
            action="regulatory_dossier_generated",
            account_id=account_id,
            entity_type="regulatory_dossier",
            payload={"generated_at": datetime.now(UTC).isoformat()},
        )
        await self.db.commit()
