'use client';

import { useState } from 'react';
import { ShareButtons } from '@/components/ShareButtons';

export interface Question {
  id: string;
  text: string;
  options: { label: string; score: number }[];
}

export const QUESTIONS: Question[] = [
  {
    id: 'severity',
    text: 'How severe are your symptoms?',
    options: [
      { label: 'Mild — barely noticeable', score: 1 },
      { label: 'Moderate — uncomfortable but manageable', score: 2 },
      { label: 'Severe — significantly affecting daily activities', score: 3 },
      { label: 'Very severe — unbearable', score: 4 },
    ],
  },
  {
    id: 'duration',
    text: 'How long have you had these symptoms?',
    options: [
      { label: 'Less than 24 hours', score: 1 },
      { label: '1–3 days', score: 2 },
      { label: '3–7 days', score: 3 },
      { label: 'More than a week', score: 4 },
    ],
  },
  {
    id: 'breathing',
    text: 'Are you experiencing difficulty breathing?',
    options: [
      { label: 'No', score: 0 },
      { label: 'Slight shortness of breath', score: 2 },
      { label: 'Moderate difficulty', score: 3 },
      { label: 'Severe — struggling to breathe', score: 5 },
    ],
  },
  {
    id: 'fever',
    text: 'Do you have a fever?',
    options: [
      { label: 'No fever', score: 0 },
      { label: 'Low-grade (37.5–38.0°C / 99.5–100.4°F)', score: 1 },
      { label: 'Moderate (38.0–39.0°C / 100.4–102.2°F)', score: 2 },
      { label: 'High (above 39.0°C / 102.2°F)', score: 4 },
    ],
  },
  {
    id: 'pain',
    text: 'Are you experiencing chest pain or pressure?',
    options: [
      { label: 'No', score: 0 },
      { label: 'Mild discomfort', score: 2 },
      { label: 'Moderate pain', score: 4 },
      { label: 'Severe crushing pain', score: 5 },
    ],
  },
];

export type TriageLevel = 'emergency' | 'urgent' | 'soon' | 'self-care';

export function triageLevel(score: number): { level: TriageLevel; label: string; color: string; advice: string } {
  if (score >= 15) return { level: 'emergency', label: 'Emergency', color: 'var(--doaide-error)', advice: 'Call emergency services or go to the nearest emergency department immediately.' };
  if (score >= 10) return { level: 'urgent', label: 'Urgent Care', color: 'var(--doaide-warning)', advice: 'See a doctor within the next few hours. Visit urgent care or your physician today.' };
  if (score >= 5) return { level: 'soon', label: 'Schedule Appointment', color: 'var(--doaide-info)', advice: 'Schedule an appointment with your doctor within the next 1–2 days.' };
  return { level: 'self-care', label: 'Self-Care', color: 'var(--doaide-success)', advice: 'Monitor your symptoms at home. Rest, stay hydrated, and seek care if symptoms worsen.' };
}

export function SymptomTriage() {
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [step, setStep] = useState(0);

  const answered = Object.keys(answers).length;
  const done = answered === QUESTIONS.length;
  const totalScore = Object.values(answers).reduce((s, v) => s + v, 0);
  const result = done ? triageLevel(totalScore) : null;
  const current = QUESTIONS[step];

  const select = (questionId: string, score: number) => {
    setAnswers({ ...answers, [questionId]: score });
    if (step < QUESTIONS.length - 1) setStep(step + 1);
  };

  const reset = () => { setAnswers({}); setStep(0); };

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>Symptom Triage</h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">Answer a few questions to help assess when to seek medical care.</p>
      <div className="space-y-6">
        {!done && current && (
          <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <div className="flex items-center justify-between mb-4">
              <p className="text-xs text-[var(--doaide-text-muted)]">Question {step + 1} of {QUESTIONS.length}</p>
              <div className="w-32 h-1.5 rounded bg-[var(--doaide-bg)] overflow-hidden">
                <div className="h-full rounded bg-[var(--doaide-gold)]" style={{ width: `${((step + 1) / QUESTIONS.length) * 100}%` }} />
              </div>
            </div>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-4">{current.text}</h2>
            <div className="space-y-2">
              {current.options.map((opt) => (
                <button key={opt.label} onClick={() => select(current.id, opt.score)} className={`w-full text-left px-4 py-3 rounded-lg text-sm transition-colors border ${answers[current.id] === opt.score ? 'border-[var(--doaide-gold)] bg-[var(--doaide-gold-bg)] text-[var(--doaide-gold)]' : 'border-[var(--doaide-border)] bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] hover:border-[var(--doaide-gold)]'}`}>
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {done && result && (
          <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] text-center">
            <p className="text-sm text-[var(--doaide-text-muted)] mb-2">Recommended Action</p>
            <p className="text-2xl font-bold mb-2" style={{ color: result.color }}>{result.label}</p>
            <p className="text-sm text-[var(--doaide-text-secondary)] mb-4">{result.advice}</p>
            <button onClick={reset} className="px-4 py-2 rounded-md text-sm font-medium bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)]">Start Over</button>
          </div>
        )}

        <p className="text-xs text-[var(--doaide-text-muted)] p-4 rounded-lg bg-[var(--doaide-surface)] border border-[var(--doaide-border)]">
          This tool is for informational purposes only and is NOT a substitute for professional medical advice. If you are experiencing a medical emergency, call your local emergency number immediately. Always consult a qualified healthcare provider for diagnosis and treatment.
        </p>

        <ShareButtons url="https://med.doaide.com/tools/symptom-triage" title="Symptom Triage — DoAide Med" />
      </div>
    </div>
  );
}
