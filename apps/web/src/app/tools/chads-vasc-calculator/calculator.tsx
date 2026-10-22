'use client';

import { useState } from 'react';
import { ShareButtons } from '@/components/ShareButtons';

export interface ChadsVascInputs {
  chf: boolean;
  hypertension: boolean;
  ageBand: 'under65' | '65to74' | '75plus';
  diabetes: boolean;
  strokeTia: boolean;
  vascular: boolean;
  female: boolean;
}

export function chadsVascScore(i: ChadsVascInputs): number {
  let score = 0;
  if (i.chf) score += 1;
  if (i.hypertension) score += 1;
  if (i.ageBand === '65to74') score += 1;
  if (i.ageBand === '75plus') score += 2;
  if (i.diabetes) score += 1;
  if (i.strokeTia) score += 2;
  if (i.vascular) score += 1;
  if (i.female) score += 1;
  return score;
}

// Approximate adjusted annual stroke rate (%) by score (Friberg et al. / ESC guidance)
export const STROKE_RISK: Record<number, string> = {
  0: '0.2%',
  1: '0.6%',
  2: '2.2%',
  3: '3.2%',
  4: '4.8%',
  5: '7.2%',
  6: '9.7%',
  7: '11.2%',
  8: '10.8%',
  9: '12.2%',
};

export function recommendation(score: number, female: boolean): string {
  const effective = female ? score - 1 : score;
  if (effective <= 0) return 'No antithrombotic therapy recommended.';
  if (effective === 1) return 'Consider oral anticoagulation based on individual risk and patient preference.';
  return 'Oral anticoagulation recommended unless contraindicated.';
}

const RISK_FACTORS: Array<{ key: keyof Omit<ChadsVascInputs, 'ageBand'>; label: string; points: string }> = [
  { key: 'chf', label: 'Congestive heart failure / LV dysfunction', points: '+1' },
  { key: 'hypertension', label: 'Hypertension', points: '+1' },
  { key: 'diabetes', label: 'Diabetes mellitus', points: '+1' },
  { key: 'strokeTia', label: 'Prior stroke, TIA, or thromboembolism', points: '+2' },
  { key: 'vascular', label: 'Vascular disease (MI, PAD, aortic plaque)', points: '+1' },
  { key: 'female', label: 'Female sex', points: '+1' },
];

export function ChadsVascCalculator() {
  const [inputs, setInputs] = useState<ChadsVascInputs>({
    chf: false,
    hypertension: false,
    ageBand: 'under65',
    diabetes: false,
    strokeTia: false,
    vascular: false,
    female: false,
  });

  const score = chadsVascScore(inputs);
  const risk = STROKE_RISK[score] ?? '—';

  const toggle = (key: keyof Omit<ChadsVascInputs, 'ageBand'>) =>
    setInputs((prev) => ({ ...prev, [key]: !prev[key] }));

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>
        CHA₂DS₂-VASc Calculator
      </h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">
        Estimate stroke risk in non-valvular atrial fibrillation to guide anticoagulation decisions.
      </p>

      <div className="space-y-6">
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)] mb-4">Risk Factors</h2>
          <div className="space-y-2">
            <div className="flex items-center gap-3 flex-wrap mb-3">
              <span className="text-sm text-[var(--doaide-text)] w-full sm:w-auto">Age:</span>
              {([
                ['under65', 'Under 65 (0)'],
                ['65to74', '65–74 (+1)'],
                ['75plus', '75 or older (+2)'],
              ] as const).map(([band, label]) => (
                <button
                  key={band}
                  type="button"
                  onClick={() => setInputs((prev) => ({ ...prev, ageBand: band }))}
                  className={`px-3 py-1.5 rounded-md text-sm transition-colors ${inputs.ageBand === band ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)] hover:border-[var(--doaide-gold)]'}`}
                >
                  {label}
                </button>
              ))}
            </div>
            {RISK_FACTORS.map((f) => (
              <label key={f.key} className="flex items-center gap-3 p-2 rounded-lg hover:bg-[var(--doaide-bg)] cursor-pointer">
                <input
                  type="checkbox"
                  checked={inputs[f.key]}
                  onChange={() => toggle(f.key)}
                  className="w-4 h-4 accent-[var(--doaide-gold)]"
                />
                <span className="text-sm text-[var(--doaide-text)] flex-1">{f.label}</span>
                <span className="text-xs font-mono text-[var(--doaide-text-muted)]">{f.points}</span>
              </label>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="p-4 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <p className="text-xs text-[var(--doaide-text-muted)] mb-1">CHA₂DS₂-VASc Score</p>
            <p className="text-2xl font-bold text-[var(--doaide-gold)]">{score} / 9</p>
          </div>
          <div className="p-4 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <p className="text-xs text-[var(--doaide-text-muted)] mb-1">Approx. Annual Stroke Risk</p>
            <p className="text-2xl font-bold text-[var(--doaide-text)]">{risk}</p>
          </div>
          <div className="p-4 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <p className="text-xs text-[var(--doaide-text-muted)] mb-1">Guideline Suggestion</p>
            <p className="text-sm font-medium text-[var(--doaide-text)]">{recommendation(score, inputs.female)}</p>
          </div>
        </div>

        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)] mb-3">About CHA₂DS₂-VASc</h2>
          <div className="text-sm text-[var(--doaide-text-secondary)] space-y-2">
            <p>CHA₂DS₂-VASc estimates annual thromboembolic stroke risk in patients with non-valvular atrial fibrillation. It refines the older CHADS₂ score by adding vascular disease, the 65–74 age band, and sex category.</p>
            <p>Female sex is a risk modifier rather than an independent risk factor: guidelines base the anticoagulation threshold on the score excluding the sex point.</p>
          </div>
        </div>

        <p className="text-xs text-[var(--doaide-text-muted)] p-4 rounded-lg bg-[var(--doaide-surface)] border border-[var(--doaide-border)]">
          This tool is for educational purposes only and does not replace professional clinical judgment. Anticoagulation decisions must weigh bleeding risk (e.g. HAS-BLED) and individual patient factors.
        </p>

        <ShareButtons url="https://med.doaide.com/tools/chads-vasc-calculator" title="CHA₂DS₂-VASc Calculator — DoAide Med" />
      </div>
    </div>
  );
}
