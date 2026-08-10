// Typed API client with bearer-token auth and transparent refresh.

import type {
  Account,
  AuditEntry,
  Citation,
  ClinicalSuggestion,
  DocumentResponse,
  ExtractionResult,
  IntakeState,
  LongitudinalRecord,
  Paginated,
  Patient,
  PatientSummary,
  PerformanceMetrics,
  ReasoningResult,
  ReasoningSession,
  SafetyCheckResponse,
  SafetyReport,
  TokenResponse,
  ValidationRun,
} from './types';

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
const PREFIX = `${API_URL}/api/v1`;

const ACCESS_KEY = 'aether_access';
const REFRESH_KEY = 'aether_refresh';
const EXPIRES_KEY = 'aether_access_expires_at';

export const tokenStore = {
  get access() {
    return typeof window === 'undefined' ? null : localStorage.getItem(ACCESS_KEY);
  },
  get refresh() {
    return typeof window === 'undefined' ? null : localStorage.getItem(REFRESH_KEY);
  },
  /** Epoch-ms the current access token expires at, or null if unknown/absent. */
  get accessExpiresAt(): number | null {
    if (typeof window === 'undefined') return null;
    const raw = localStorage.getItem(EXPIRES_KEY);
    return raw ? Number(raw) : null;
  },
  set(tokens: TokenResponse) {
    localStorage.setItem(ACCESS_KEY, tokens.access_token);
    localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    localStorage.setItem(EXPIRES_KEY, String(Date.now() + tokens.expires_in * 1000));
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(EXPIRES_KEY);
  },
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  retry = true,
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  const access = tokenStore.access;
  if (access) headers.set('Authorization', `Bearer ${access}`);

  const resp = await fetch(`${PREFIX}${path}`, { ...options, headers });

  if (resp.status === 401 && retry && tokenStore.refresh) {
    const refreshed = await tryRefresh();
    if (refreshed) return request<T>(path, options, false);
  }

  if (!resp.ok) {
    let code = 'error';
    let message = resp.statusText;
    try {
      const body = await resp.json();
      code = body.code ?? code;
      message = body.message ?? body.detail ?? message;
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(resp.status, code, message);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

async function tryRefresh(): Promise<boolean> {
  try {
    const resp = await fetch(`${PREFIX}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: tokenStore.refresh }),
    });
    if (!resp.ok) {
      tokenStore.clear();
      return false;
    }
    tokenStore.set(await resp.json());
    return true;
  } catch {
    return false;
  }
}

export const api = {
  async signup(email: string, password: string, displayName?: string) {
    const tokens = await request<TokenResponse>('/auth/signup', {
      method: 'POST',
      body: JSON.stringify({ email, password, display_name: displayName }),
    });
    tokenStore.set(tokens);
    return tokens;
  },
  async login(email: string, password: string) {
    const tokens = await request<TokenResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    tokenStore.set(tokens);
    return tokens;
  },
  async logout() {
    const refresh = tokenStore.refresh;
    if (refresh) {
      try {
        await request('/auth/logout', {
          method: 'POST',
          body: JSON.stringify({ refresh_token: refresh }),
        });
      } catch {
        /* ignore */
      }
    }
    tokenStore.clear();
  },
  me: () => request<Account>('/auth/me'),
  /** Proactively rotate the access/refresh pair (used for scheduled, idle-free refresh). */
  refreshAccessToken: () => tryRefresh(),

  // --- Session management ---
  listSessions: () =>
    request<
      Array<{
        id: string;
        ip_address: string | null;
        user_agent: string | null;
        created_at: string;
        last_used_at: string;
        expires_at: string;
      }>
    >('/auth/sessions'),
  revokeSession: (sessionId: string) =>
    request<{ message: string }>(`/auth/sessions/${sessionId}`, { method: 'DELETE' }),
  logoutAllOtherSessions: () =>
    request<{ message: string }>('/auth/logout-all', {
      method: 'POST',
      body: JSON.stringify({ keep_current_refresh_token: tokenStore.refresh }),
    }),

  listPatients: (search?: string) =>
    request<Paginated<PatientSummary>>(
      `/patients${search ? `?search=${encodeURIComponent(search)}` : ''}`,
    ),
  getPatient: (id: string) => request<Patient>(`/patients/${id}`),
  createPatient: (data: Record<string, unknown>) =>
    request<Patient>('/patients', { method: 'POST', body: JSON.stringify(data) }),

  getRecord: (id: string) => request<LongitudinalRecord>(`/patients/${id}/record`),

  listDocuments: (id: string) =>
    request<DocumentResponse[]>(`/patients/${id}/documents`),
  uploadDocument: (id: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<DocumentResponse>(`/patients/${id}/documents`, {
      method: 'POST',
      body: form,
    });
  },
  getExtraction: (id: string, docId: string) =>
    request<ExtractionResult>(`/patients/${id}/documents/${docId}/extraction`),
  approveExtraction: (id: string, docId: string, rejected: number[] = []) =>
    request<{ merged: Record<string, number> }>(
      `/patients/${id}/documents/${docId}/approve`,
      {
        method: 'POST',
        body: JSON.stringify({ corrections: [], rejected_entity_indexes: rejected }),
      },
    ),

  checkDrugSafety: (id: string, body: { drug_reference_id?: string; drug_name?: string }) =>
    request<SafetyCheckResponse>(`/patients/${id}/drug-safety/check`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  auditTrail: (id: string) =>
    request<Paginated<AuditEntry>>(`/patients/${id}/audit`),
  verifyAudit: (id: string) =>
    request<{ entries_checked: number; chain_valid: boolean }>(
      `/patients/${id}/audit/verify`,
    ),

  // --- Reasoning engine (Phase 2/3) ---
  startReasoning: (patientId: string, presentingComplaint: string) =>
    request<IntakeState>(`/patients/${patientId}/reasoning`, {
      method: 'POST',
      body: JSON.stringify({ presenting_complaint: presentingComplaint }),
    }),
  getSession: (sessionId: string) =>
    request<ReasoningSession>(`/reasoning/${sessionId}`),
  submitIntakeAnswers: (
    sessionId: string,
    answers: Array<{ question_id: string; answer_text: string }>,
  ) =>
    request<IntakeState>(`/reasoning/${sessionId}/intake/answers`, {
      method: 'POST',
      body: JSON.stringify({ answers }),
    }),
  runReasoning: (sessionId: string) =>
    request<ReasoningResult>(`/reasoning/${sessionId}/run`, { method: 'POST' }),
  listSuggestions: (sessionId: string) =>
    request<ClinicalSuggestion[]>(`/reasoning/${sessionId}/suggestions`),
  recordDecision: (
    sessionId: string,
    suggestionId: string,
    decision: string,
    reason?: string,
  ) =>
    request<{ id: string; decision: string }>(
      `/reasoning/${sessionId}/suggestions/${suggestionId}/decision`,
      { method: 'POST', body: JSON.stringify({ decision, reason: reason ?? null }) },
    ),
  /**
   * Server-Sent Events URL for the live Reasoning Theatre.
   *
   * EventSource cannot set an Authorization header, so the credential has to travel in the
   * query string — where proxy access logs and browser history keep it. The access token
   * therefore never goes in the URL: this mints a session-scoped token that expires in
   * about a minute and is rejected by every other endpoint.
   */
  async reasoningStreamUrl(sessionId: string): Promise<string> {
    const { token } = await request<{ token: string; expires_in: number }>(
      `/reasoning/${sessionId}/stream-token`,
      { method: 'POST' },
    );
    return `${PREFIX}/reasoning/${sessionId}/stream?token=${encodeURIComponent(token)}`;
  },

  // --- Guidelines (Phase 3) ---
  searchGuidelines: (q: string) =>
    request<Citation[]>(`/guidelines/search?q=${encodeURIComponent(q)}`),
  corpusInfo: () =>
    request<{
      corpus_version: string;
      chunk_count: number;
      retrieval_threshold: number;
      citation_faithfulness_target: number;
    }>(`/guidelines/corpus`),
  managementOptions: (sessionId: string) =>
    request<ClinicalSuggestion[]>(`/reasoning/${sessionId}/management-options`),

  // --- Validation / regulatory / safety (Phase 4) ---
  performanceMetrics: () =>
    request<PerformanceMetrics>(`/metrics/performance`),
  runValidation: () => request<ValidationRun>(`/validation/run`, { method: 'POST' }),
  listValidationRuns: () =>
    request<Array<{ id: string; vignette_count: number; metrics: Record<string, unknown>; created_at: string }>>(
      `/validation/runs`,
    ),
  fileSafetyReport: (body: {
    category: string;
    severity: string;
    description: string;
    patient_id?: string;
    session_id?: string;
  }) =>
    request<SafetyReport>(`/safety-reports`, { method: 'POST', body: JSON.stringify(body) }),
  listSafetyReports: () => request<SafetyReport[]>(`/safety-reports`),
  pilotStatus: () => request<{ pilot_mode: boolean; message: string }>(`/pilot/status`),
  samdDossierUrl: (format: 'json' | 'markdown' = 'markdown') =>
    `${PREFIX}/regulatory/samd-dossier?format=${format}`,

  async downloadSamdDossier(format: 'json' | 'markdown' = 'markdown'): Promise<void> {
    const headers: Record<string, string> = {};
    const access = tokenStore.access;
    if (access) headers['Authorization'] = `Bearer ${access}`;

    const resp = await fetch(
      `${PREFIX}/regulatory/samd-dossier?format=${format}`,
      { headers },
    );

    if (resp.status === 401 && tokenStore.refresh) {
      const refreshed = await tryRefresh();
      if (refreshed) return this.downloadSamdDossier(format);
    }

    if (!resp.ok) {
      throw new ApiError(resp.status, 'download_error', resp.statusText);
    }

    const blob = await resp.blob();
    const ext = format === 'json' ? 'json' : 'md';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `cdsco-samd-dossier.${ext}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },
};
