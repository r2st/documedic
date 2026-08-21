"""SQLAlchemy ORM models. Importing this package registers every table on Base.metadata."""

from app.models.allergy import Allergy
from app.models.appointment import Appointment, ProviderAvailability
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.clinical_suggestion import ClinicalSuggestion, ClinicianDecisionRecord
from app.models.condition import Condition
from app.models.critical_lab_acknowledgement import CriticalLabAcknowledgement
from app.models.derived_marker import DerivedMarker
from app.models.discharge_summary import DischargeSummary
from app.models.document import Document
from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_safety_override import DrugSafetyOverride
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary
from app.models.encounter import Encounter
from app.models.encounter_participant import EncounterParticipant
from app.models.guideline import GuidelineChunk
from app.models.handoff import PatientHandoff
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.patient_portal_grant import PatientPortalGrant
from app.models.protocol_application import ProtocolApplication
from app.models.reasoning_session import ReasoningSession
from app.models.user import Account, PasswordResetToken, Session
from app.models.validation import SafetyReport, ValidationRun

__all__ = [
    "Base",
    "Account",
    "PasswordResetToken",
    "Session",
    "Patient",
    "Document",
    "Encounter",
    "MedicationEvent",
    "LabResult",
    "CriticalLabAcknowledgement",
    "Condition",
    "Allergy",
    "DerivedMarker",
    "DischargeSummary",
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
    "PatientHandoff",
    "Appointment",
    "ProviderAvailability",
    "EncounterParticipant",
    "PatientPortalGrant",
    "ProtocolApplication",
    "ValidationRun",
    "SafetyReport",
]
