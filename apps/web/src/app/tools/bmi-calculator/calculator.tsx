'use client';

import { useState } from 'react';
import { ShareButtons } from '@/components/ShareButtons';

export function bmiCategory(bmi: number): { label: string; color: string; advice: string } {
  if (bmi < 18.5) return { label: 'Underweight', color: 'var(--doaide-info)', advice: 'Consider consulting a healthcare provider about nutrition.' };
  if (bmi < 25) return { label: 'Normal', color: 'var(--doaide-success)', advice: 'Maintain a balanced diet and regular exercise.' };
  if (bmi < 30) return { label: 'Overweight', color: 'var(--doaide-warning)', advice: 'Consider lifestyle changes — diet and exercise can help.' };
  return { label: 'Obese', color: 'var(--doaide-error)', advice: 'Consult a healthcare provider for a personalized plan.' };
}

export function BmiCalculator() {
  const [weight, setWeight] = useState(70);
  const [height, setHeight] = useState(170);
  const [unit, setUnit] = useState<'metric' | 'imperial'>('metric');

  const heightM = unit === 'metric' ? height / 100 : height * 0.0254;
  const weightKg = unit === 'metric' ? weight : weight * 0.453592;
  const bmi = heightM > 0 ? weightKg / (heightM * heightM) : 0;
  const cat = bmiCategory(bmi);

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>BMI Calculator</h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">Calculate your Body Mass Index and see health recommendations.</p>
      <div className="space-y-6">
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] space-y-4">
          <div className="flex gap-2">
            {(['metric', 'imperial'] as const).map((u) => (
              <button key={u} onClick={() => setUnit(u)} className={`px-3 py-1.5 rounded-md text-sm font-medium capitalize transition-colors ${unit === u ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)]'}`}>{u}</button>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-[var(--doaide-text-secondary)] mb-1">Weight ({unit === 'metric' ? 'kg' : 'lbs'})</label>
              <input type="number" min={1} value={weight} onChange={(e) => setWeight(Number(e.target.value) || 0)} className="w-full px-3 py-2 rounded-md text-sm" />
            </div>
            <div>
              <label className="block text-sm text-[var(--doaide-text-secondary)] mb-1">Height ({unit === 'metric' ? 'cm' : 'inches'})</label>
              <input type="number" min={1} value={height} onChange={(e) => setHeight(Number(e.target.value) || 0)} className="w-full px-3 py-2 rounded-md text-sm" />
            </div>
          </div>
        </div>

        {bmi > 0 && (
          <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] text-center">
            <p className="text-sm text-[var(--doaide-text-muted)] mb-1">Your BMI</p>
            <p className="text-4xl font-bold mb-2" style={{ color: cat.color }}>{bmi.toFixed(1)}</p>
            <p className="text-lg font-semibold mb-2" style={{ color: cat.color }}>{cat.label}</p>
            <p className="text-sm text-[var(--doaide-text-secondary)]">{cat.advice}</p>
          </div>
        )}

        <p className="text-xs text-[var(--doaide-text-muted)] p-4 rounded-lg bg-[var(--doaide-surface)] border border-[var(--doaide-border)]">
          This calculator is for informational purposes only. BMI does not account for muscle mass, bone density, or body composition. Always consult a qualified healthcare provider for medical advice.
        </p>

        <ShareButtons url="https://med.doaide.com/tools/bmi-calculator" title="BMI Calculator — DoAide Med" />
      </div>
    </div>
  );
}
