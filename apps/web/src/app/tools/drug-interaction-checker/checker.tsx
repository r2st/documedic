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
  { drugs: ['Warfarin', 'Metronidazole'], severity: 'major', description: 'Metronidazole inhibits warfarin metabolism; bleeding risk. Monitor INR.' },
  { drugs: ['Warfarin', 'Amiodarone'], severity: 'major', description: 'Amiodarone potentiates warfarin. Reduce warfarin dose and monitor INR.' },
  { drugs: ['Tramadol', 'SSRIs'], severity: 'major', description: 'Additive serotonergic effect; risk of serotonin syndrome and lowered seizure threshold.' },
  { drugs: ['Verapamil', 'Metoprolol'], severity: 'major', description: 'Additive AV-node blockade; risk of bradycardia, heart block, and hypotension.' },
  { drugs: ['Spironolactone', 'Potassium'], severity: 'major', description: 'High risk of hyperkalemia. Avoid combination or monitor potassium closely.' },
  { drugs: ['Digoxin', 'Furosemide'], severity: 'moderate', description: 'Diuretic-induced hypokalemia increases digoxin toxicity. Monitor electrolytes.' },
  { drugs: ['Allopurinol', 'Azathioprine'], severity: 'major', description: 'Allopurinol blocks azathioprine metabolism; severe myelosuppression. Reduce azathioprine to 25%.' },
  { drugs: ['Atorvastatin', 'Clarithromycin'], severity: 'major', description: 'CYP3A4 inhibition raises statin levels; myopathy and rhabdomyolysis risk.' },
  { drugs: ['Amlodipine', 'Simvastatin'], severity: 'moderate', description: 'Amlodipine raises simvastatin exposure. Limit simvastatin to 20 mg daily.' },
  { drugs: ['Levothyroxine', 'Calcium'], severity: 'moderate', description: 'Calcium reduces levothyroxine absorption. Separate doses by 4 hours.' },
  { drugs: ['Levothyroxine', 'Iron'], severity: 'moderate', description: 'Iron salts reduce levothyroxine absorption. Separate doses by 4 hours.' },
  { drugs: ['Doxycycline', 'Antacids'], severity: 'moderate', description: 'Polyvalent cations chelate doxycycline and reduce absorption. Space doses by 2-3 hours.' },
  { drugs: ['Metoprolol', 'Insulin'], severity: 'moderate', description: 'Beta-blockers can mask hypoglycemia symptoms. Counsel on glucose monitoring.' },
  { drugs: ['Prednisolone', 'Ibuprofen'], severity: 'moderate', description: 'Additive GI irritation; increased risk of peptic ulcer and GI bleeding.' },
  { drugs: ['ACE Inhibitors', 'Lithium'], severity: 'moderate', description: 'ACE inhibitors reduce lithium clearance; monitor lithium levels.' },
  { drugs: ['Insulin', 'Alcohol'], severity: 'moderate', description: 'Alcohol potentiates hypoglycemia and can delay recovery. Advise caution.' },
];

const ALL_DRUGS = [...new Set(INTERACTIONS.flatMap((i) => i.drugs))].sort();

const SEVERITY_RANK: Record<Interaction['severity'], number> = { major: 0, moderate: 1, minor: 2 };

export function findInteractions(selected: string[]): Interaction[] {
  if (selected.length < 2) return [];
  const set = new Set(selected.map((d) => d.toLowerCase()));
  return INTERACTIONS.filter(
    (i) => set.has(i.drugs[0].toLowerCase()) && set.has(i.drugs[1].toLowerCase()),
  ).sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);
}

const SEVERITY_COLORS: Record<string, string> = {
  major: 'var(--doaide-error)',
  moderate: 'var(--doaide-warning)',
  minor: 'var(--doaide-info)',
};

export function DrugInteractionChecker() {
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const results = findInteractions(selected);

  const toggle = (drug: string) => {
    setSelected((prev) => (prev.includes(drug) ? prev.filter((d) => d !== drug) : [...prev, drug]));
  };

  const visibleDrugs = ALL_DRUGS.filter((d) => d.toLowerCase().includes(query.trim().toLowerCase()));

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>Drug Interaction Checker</h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">Select two or more medications to check for known interactions.</p>
      <div className="space-y-6">
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
            <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)]">Select Medications</h2>
            {selected.length > 0 && (
              <div className="flex items-center gap-3">
                <span className="text-xs text-[var(--doaide-text-muted)]">{selected.length} selected</span>
                <button
                  onClick={() => setSelected([])}
                  className="text-xs px-2.5 py-1 rounded-md border border-[var(--doaide-border)] text-[var(--doaide-text-secondary)] hover:border-[var(--doaide-gold)] transition-colors"
                >
                  Clear all
                </button>
              </div>
            )}
          </div>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search medications…"
            aria-label="Search medications"
            className="w-full mb-4 px-3 py-2 rounded-md text-sm bg-[var(--doaide-bg)] border border-[var(--doaide-border)] text-[var(--doaide-text)] placeholder:text-[var(--doaide-text-muted)] focus:border-[var(--doaide-gold)] focus:outline-none"
          />
          <div className="flex flex-wrap gap-2">
            {visibleDrugs.map((drug) => (
              <button key={drug} onClick={() => toggle(drug)} className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${selected.includes(drug) ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)] hover:border-[var(--doaide-gold)]'}`}>
                {drug}
              </button>
            ))}
            {visibleDrugs.length === 0 && (
              <p className="text-sm text-[var(--doaide-text-muted)]">No medications match &ldquo;{query}&rdquo;.</p>
            )}
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
