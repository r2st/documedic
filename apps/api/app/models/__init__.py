"""SQLAlchemy ORM models. Importing this package registers every table on Base.metadata."""

from app.models.allergy import Allergy
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.clinical_suggestion import ClinicalSuggestion, ClinicianDecisionRecord
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.document import Document
from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_safety_override import DrugSafetyOverride
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary
from app.models.encounter import Encounter
from app.models.guideline import GuidelineChunk
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.reasoning_session import ReasoningSession
from app.models.user import Account, Session
from app.models.validation import SafetyReport, ValidationRun

__all__ = [
    "Base",
    "Account",
    "Session",
    "Patient",
    "Document",
    "Encounter",
    "MedicationEvent",
    "LabResult",
    "Condition",
    "Allergy",
    "DerivedMarker",
    "DrugVocabulary",
    "DrugInteraction",
    "Contraindication",
    "DrugSafetyCheck",
    "DrugSafetyOverride",
    "AuditLog",
    "ReasoningSession",
    "IntakeQuestion",
    "IntakeAnswer",
    "ClinicalSuggestion",
    "ClinicianDecisionRecord",
    "GuidelineChunk",
    "ValidationRun",
    "SafetyReport",
]
