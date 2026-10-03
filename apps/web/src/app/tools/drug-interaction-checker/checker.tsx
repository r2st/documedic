'use client';

import { useState } from 'react';
import { ShareButtons } from '@/components/ShareButtons';

export interface Interaction {
  drugs: [string, string];
  severity: 'major' | 'moderate' | 'minor';
  description: string;
}

export const INTERACTIONS: Interaction[] = [
  { drugs: ['Warfarin', 'Aspirin'], severity: 'major', description: 'Increased risk of bleeding. Monitor INR closely.' },
  { drugs: ['Warfarin', 'Ibuprofen'], severity: 'major', description: 'NSAIDs increase anticoagulant effect and bleeding risk.' },
  { drugs: ['Metformin', 'Alcohol'], severity: 'moderate', description: 'Increased risk of lactic acidosis.' },
  { drugs: ['Lisinopril', 'Potassium'], severity: 'major', description: 'Risk of hyperkalemia. Monitor potassium levels.' },
  { drugs: ['Simvastatin', 'Amiodarone'], severity: 'major', description: 'Increased risk of rhabdomyolysis. Limit simvastatin dose.' },
  { drugs: ['Ciprofloxacin', 'Antacids'], severity: 'moderate', description: 'Antacids reduce ciprofloxacin absorption. Space doses by 2 hours.' },
  { drugs: ['Methotrexate', 'Ibuprofen'], severity: 'major', description: 'NSAIDs decrease methotrexate clearance, increasing toxicity.' },
  { drugs: ['SSRIs', 'MAOIs'], severity: 'major', description: 'Risk of serotonin syndrome. Contraindicated combination.' },
  { drugs: ['Digoxin', 'Amiodarone'], severity: 'major', description: 'Amiodarone increases digoxin levels. Reduce digoxin dose by 50%.' },
  { drugs: ['ACE Inhibitors', 'Spironolactone'], severity: 'moderate', description: 'Additive hyperkalemia risk. Monitor potassium.' },
  { drugs: ['Clopidogrel', 'Omeprazole'], severity: 'moderate', description: 'Omeprazole may reduce clopidogrel activation.' },
  { drugs: ['Theophylline', 'Ciprofloxacin'], severity: 'major', description: 'Ciprofloxacin inhibits theophylline metabolism.' },
  { drugs: ['Lithium', 'Ibuprofen'], severity: 'major', description: 'NSAIDs increase lithium levels. Monitor closely.' },
  { drugs: ['Sildenafil', 'Nitrates'], severity: 'major', description: 'Severe hypotension risk. Contraindicated.' },
  { drugs: ['Phenytoin', 'Warfarin'], severity: 'major', description: 'Complex interaction. Monitor INR and phenytoin levels.' },
  { drugs: ['Clarithromycin', 'Simvastatin'], severity: 'major', description: 'Increased statin levels; rhabdomyolysis risk.' },
  { drugs: ['Fluconazole', 'Warfarin'], severity: 'major', description: 'Fluconazole inhibits warfarin metabolism.' },
  { drugs: ['Carbamazepine', 'Oral Contraceptives'], severity: 'major', description: 'Carbamazepine reduces contraceptive efficacy.' },
  { drugs: ['Erythromycin', 'Theophylline'], severity: 'moderate', description: 'Erythromycin increases theophylline levels.' },
  { drugs: ['Metronidazole', 'Alcohol'], severity: 'major', description: 'Disulfiram-like reaction: nausea, vomiting, flushing.' },
];

const ALL_DRUGS = [...new Set(INTERACTIONS.flatMap((i) => i.drugs))].sort();

export function findInteractions(selected: string[]): Interaction[] {
  if (selected.length < 2) return [];
  const set = new Set(selected.map((d) => d.toLowerCase()));
  return INTERACTIONS.filter(
    (i) => set.has(i.drugs[0].toLowerCase()) && set.has(i.drugs[1].toLowerCase()),
  );
}

const SEVERITY_COLORS: Record<string, string> = {
  major: 'var(--doaide-error)',
  moderate: 'var(--doaide-warning)',
  minor: 'var(--doaide-info)',
};

export function DrugInteractionChecker() {
  const [selected, setSelected] = useState<string[]>([]);
  const results = findInteractions(selected);

  const toggle = (drug: string) => {
    setSelected((prev) => (prev.includes(drug) ? prev.filter((d) => d !== drug) : [...prev, drug]));
  };

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>Drug Interaction Checker</h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">Select two or more medications to check for known interactions.</p>
      <div className="space-y-6">
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)] mb-3">Select Medications</h2>
          <div className="flex flex-wrap gap-2">
            {ALL_DRUGS.map((drug) => (
              <button key={drug} onClick={() => toggle(drug)} className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${selected.includes(drug) ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)] hover:border-[var(--doaide-gold)]'}`}>
                {drug}
              </button>
            ))}
          </div>
        </div>

        {selected.length >= 2 && (
          <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
            <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)] mb-3">
              {results.length > 0 ? `${results.length} interaction${results.length > 1 ? 's' : ''} found` : 'No known interactions found'}
            </h2>
            {results.length > 0 ? (
              <div className="space-y-3">
                {results.map((r, i) => (
                  <div key={i} className="p-4 rounded-lg bg-[var(--doaide-bg)] border border-[var(--doaide-border)]">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-bold uppercase px-2 py-0.5 rounded" style={{ color: SEVERITY_COLORS[r.severity], backgroundColor: `color-mix(in srgb, ${SEVERITY_COLORS[r.severity]} 15%, transparent)` }}>
                        {r.severity}
                      </span>
                      <span className="text-sm font-medium text-[var(--doaide-text)]">{r.drugs[0]} + {r.drugs[1]}</span>
                    </div>
                    <p className="text-sm text-[var(--doaide-text-secondary)]">{r.description}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-[var(--doaide-success)]">No interactions detected between the selected medications.</p>
            )}
          </div>
        )}

        <p className="text-xs text-[var(--doaide-text-muted)] p-4 rounded-lg bg-[var(--doaide-surface)] border border-[var(--doaide-border)]">
          This tool checks a limited set of common interactions and is for educational purposes only. It does not replace professional clinical judgment. Always verify interactions with an authoritative drug database and consult a pharmacist or physician.
        </p>

        <ShareButtons url="https://med.doaide.com/tools/drug-interaction-checker" title="Drug Interaction Checker — DoAide Med" />
      </div>
    </div>
  );
}
