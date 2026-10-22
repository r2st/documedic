'use client';

import { useState } from 'react';
import { ShareButtons } from '@/components/ShareButtons';

export function calculateCrCl(age: number, weightKg: number, serumCreatinine: number, sex: 'male' | 'female'): number {
  if (age <= 0 || weightKg <= 0 || serumCreatinine <= 0) return 0;
  const base = ((140 - age) * weightKg) / (72 * serumCreatinine);
  return sex === 'female' ? base * 0.85 : base;
}

export function renalCategory(crcl: number): { label: string; note: string } {
  if (crcl >= 90) return { label: 'Normal renal function', note: 'Standard dosing for most renally cleared drugs.' };
  if (crcl >= 60) return { label: 'Mildly reduced', note: 'Most drugs need no adjustment; review nephrotoxic agents.' };
  if (crcl >= 30) return { label: 'Moderately reduced', note: 'Dose-adjust renally cleared drugs (e.g. metformin, gabapentin, many antibiotics).' };
  if (crcl >= 15) return { label: 'Severely reduced', note: 'Significant dose reductions required; avoid nephrotoxic drugs.' };
  return { label: 'Kidney failure range', note: 'Specialist dosing required; many drugs contraindicated.' };
}

export function CreatinineClearanceCalculator() {
  const [age, setAge] = useState('60');
  const [weight, setWeight] = useState('70');
  const [creatinine, setCreatinine] = useState('1.0');
  const [sex, setSex] = useState<'male' | 'female'>('male');

  const crcl = calculateCrCl(parseFloat(age) || 0, parseFloat(weight) || 0, parseFloat(creatinine) || 0, sex);
  const category = renalCategory(crcl);

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>
        Creatinine Clearance Calculator
      </h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">
        Estimate creatinine clearance with the Cockcroft-Gault equation for renal drug dosing.
      </p>

      <div className="space-y-6">
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)] mb-4">Patient Parameters</h2>
          <div className="space-y-4">
            <div className="flex items-center gap-4">
              <label className="text-sm text-[var(--doaide-text)] w-52">Age (years)</label>
              <input type="number" min={1} max={120} value={age} onChange={(e) => setAge(e.target.value)} className="w-28 px-3 py-2 rounded-md text-sm text-center" />
            </div>
            <div className="flex items-center gap-4">
              <label className="text-sm text-[var(--doaide-text)] w-52">Weight (kg)</label>
              <input type="number" min={1} value={weight} onChange={(e) => setWeight(e.target.value)} className="w-28 px-3 py-2 rounded-md text-sm text-center" />
            </div>
            <div className="flex items-center gap-4">
              <label className="text-sm text-[var(--doaide-text)] w-52">Serum Creatinine (mg/dL)</label>
              <input type="number" min={0.1} step={0.1} value={creatinine} onChange={(e) => setCreatinine(e.target.value)} className="w-28 px-3 py-2 rounded-md text-sm text-center" />
            </div>
            <div className="flex items-center gap-4">
              <label className="text-sm text-[var(--doaide-text)] w-52">Sex</label>
              <div className="flex gap-2">
                {(['male', 'female'] as const).map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setSex(s)}
                    className={`px-4 py-2 rounded-md text-sm capitalize transition-colors ${sex === s ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)] hover:border-[var(--doaide-gold)]'}`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="p-4 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <p className="text-xs text-[var(--doaide-text-muted)] mb-1">Estimated Creatinine Clearance</p>
            <p className="text-2xl font-bold text-[var(--doaide-gold)]">{crcl.toFixed(1)} <span className="text-sm font-normal">mL/min</span></p>
          </div>
          <div className="p-4 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <p className="text-xs text-[var(--doaide-text-muted)] mb-1">Category</p>
            <p className="text-lg font-bold text-[var(--doaide-text)]">{category.label}</p>
            <p className="text-xs text-[var(--doaide-text-secondary)] mt-1">{category.note}</p>
          </div>
        </div>

        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)] mb-3">Cockcroft-Gault Equation</h2>
          <div className="text-sm text-[var(--doaide-text-secondary)] space-y-2">
            <p className="font-mono text-[var(--doaide-text)]">CrCl = [(140 − age) × weight] ÷ (72 × SCr) × 0.85 if female</p>
            <p>Cockcroft-Gault remains the standard for renal drug dosing because most drug dosing studies used it. For CKD staging, laboratories report eGFR (CKD-EPI), which is indexed to body surface area and can differ from CrCl.</p>
            <p>In obese patients, consider using ideal or adjusted body weight — total body weight overestimates clearance.</p>
          </div>
        </div>

        <p className="text-xs text-[var(--doaide-text-muted)] p-4 rounded-lg bg-[var(--doaide-surface)] border border-[var(--doaide-border)]">
          This calculator is for educational purposes only and does not replace professional clinical judgment. Verify dosing decisions against an authoritative reference and the patient&apos;s full clinical picture.
        </p>

        <ShareButtons url="https://med.doaide.com/tools/creatinine-clearance-calculator" title="Creatinine Clearance Calculator — DoAide Med" />
      </div>
    </div>
  );
}
