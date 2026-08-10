// URL/method/body contract for the typed API client. api.test.ts covers the token
// plumbing; this file covers the request surface every page depends on.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, tokenStore } from './api';

const PREFIX = 'http://localhost:8000/api/v1';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** URL, method, headers, and parsed JSON body of the nth fetch call. */
function callAt(n = 0) {
  const [url, init] = vi.mocked(fetch).mock.calls[n];
  const headers = new Headers(init?.headers);
  return {
    url: String(url),
    method: init?.method ?? 'GET',
    headers,
    body: init?.body,
    json: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
  };
}

function seedTokens(access = 'a1', refresh = 'r1') {
  tokenStore.set({
    access_token: access,
    refresh_token: refresh,
    token_type: 'bearer',
    expires_in: 900,
  });
}

describe('request plumbing', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  it('omits the Authorization header when no session exists', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ items: [] }));
    await api.listPatients();
    expect(callAt().headers.has('Authorization')).toBe(false);
  });

  it('attaches the bearer token when a session exists', async () => {
    seedTokens('access-token');
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ items: [] }));
    await api.listPatients();
    expect(callAt().headers.get('Authorization')).toBe('Bearer access-token');
  });

  it('does not force a JSON content type on multipart uploads', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ id: 'doc-1' }));
    await api.uploadDocument('pat-1', new File(['x'], 'scan.png', { type: 'image/png' }));

    const call = callAt();
    expect(call.headers.has('Content-Type')).toBe(false);
    expect(call.body).toBeInstanceOf(FormData);
    expect((call.body as FormData).get('file')).toBeInstanceOf(File);
  });

  it('returns undefined for a 204 instead of trying to parse an empty body', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api.revokeSession('sess-1')).resolves.toBeUndefined();
  });

  it('falls back to the status text when the error body is not JSON', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response('<html>502</html>', { status: 502, statusText: 'Bad Gateway' }),
    );
    await expect(api.listPatients()).rejects.toMatchObject({
      status: 502,
      code: 'error',
      message: 'Bad Gateway',
    });
  });

  it('accepts FastAPI\'s `detail` shape as the error message', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ detail: 'Not authenticated' }, 403));
    await expect(api.listPatients()).rejects.toMatchObject({ message: 'Not authenticated' });
  });

  it('does not attempt a refresh on a 401 when there is no refresh token', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ code: 'invalid_token' }, 401));
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('treats a network failure during refresh as a failed refresh, not a crash', async () => {
    seedTokens('stale', 'r1');
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ code: 'invalid_token' }, 401))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(await api.refreshAccessToken()).toBe(false);
  });
});

describe('endpoint contracts', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  it('sends the patient search term in a POST body, never in the URL', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ items: [] }));
    await api.listPatients('Asha & Co / #1');
    const { url, method, json } = callAt();
    // The term is a direct identifier; a query string would put it in every access log.
    expect(url).toBe(`${PREFIX}/patients/search`);
    expect(url).not.toContain('Asha');
    expect(method).toBe('POST');
    expect(json).toEqual({ search: 'Asha & Co / #1' });
  });

  it('lists with a plain GET and no search parameter when no term is given', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ items: [] }));
    await api.listPatients();
    const { url, method } = callAt();
    expect(url).toBe(`${PREFIX}/patients`);
    expect(method).toBe('GET');
  });

  it('treats an empty search term as a plain list rather than an empty-term search', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ items: [] }));
    await api.listPatients('');
    expect(callAt().url).toBe(`${PREFIX}/patients`);
  });

  it('percent-encodes the guideline query', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse([]));
    await api.searchGuidelines('dengue & shock');
    expect(callAt().url).toBe(`${PREFIX}/guidelines/search?q=dengue%20%26%20shock`);
  });

  it.each([
    ['getPatient', () => api.getPatient('pat-1'), `${PREFIX}/patients/pat-1`, 'GET'],
    ['getRecord', () => api.getRecord('pat-1'), `${PREFIX}/patients/pat-1/record`, 'GET'],
    ['listDocuments', () => api.listDocuments('pat-1'), `${PREFIX}/patients/pat-1/documents`, 'GET'],
    [
      'getExtraction',
      () => api.getExtraction('pat-1', 'doc-1'),
      `${PREFIX}/patients/pat-1/documents/doc-1/extraction`,
      'GET',
    ],
    ['auditTrail', () => api.auditTrail('pat-1'), `${PREFIX}/patients/pat-1/audit`, 'GET'],
    ['verifyAudit', () => api.verifyAudit('pat-1'), `${PREFIX}/patients/pat-1/audit/verify`, 'GET'],
    ['getSession', () => api.getSession('sess-1'), `${PREFIX}/reasoning/sess-1`, 'GET'],
    [
      'listSuggestions',
      () => api.listSuggestions('sess-1'),
      `${PREFIX}/reasoning/sess-1/suggestions`,
      'GET',
    ],
    [
      'managementOptions',
      () => api.managementOptions('sess-1'),
      `${PREFIX}/reasoning/sess-1/management-options`,
      'GET',
    ],
    ['corpusInfo', () => api.corpusInfo(), `${PREFIX}/guidelines/corpus`, 'GET'],
    ['performanceMetrics', () => api.performanceMetrics(), `${PREFIX}/metrics/performance`, 'GET'],
    ['listValidationRuns', () => api.listValidationRuns(), `${PREFIX}/validation/runs`, 'GET'],
    ['listSafetyReports', () => api.listSafetyReports(), `${PREFIX}/safety-reports`, 'GET'],
    ['pilotStatus', () => api.pilotStatus(), `${PREFIX}/pilot/status`, 'GET'],
    ['listSessions', () => api.listSessions(), `${PREFIX}/auth/sessions`, 'GET'],
    ['runValidation', () => api.runValidation(), `${PREFIX}/validation/run`, 'POST'],
    [
      'runReasoning',
      () => api.runReasoning('sess-1'),
      `${PREFIX}/reasoning/sess-1/run`,
      'POST',
    ],
    [
      'revokeSession',
      () => api.revokeSession('sess-9'),
      `${PREFIX}/auth/sessions/sess-9`,
      'DELETE',
    ],
  ])('%s targets the right endpoint', async (_name, invoke, url, method) => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await invoke();
    expect(callAt().url).toBe(url);
    expect(callAt().method).toBe(method);
  });

  it('signup posts the credentials and stores the returned session', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ access_token: 'a1', refresh_token: 'r1', token_type: 'bearer', expires_in: 900 }),
    );
    await api.signup('jane@clinic.in', 'longenoughpw', 'Dr. Jane Smith');

    expect(callAt().url).toBe(`${PREFIX}/auth/signup`);
    expect(callAt().json).toEqual({
      email: 'jane@clinic.in',
      password: 'longenoughpw',
      display_name: 'Dr. Jane Smith',
    });
    expect(tokenStore.access).toBe('a1');
  });

  it('createPatient posts the supplied fields', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ id: 'pat-1' }));
    await api.createPatient({ full_name: 'Asha Reddy', consent_given: true });
    expect(callAt().json).toEqual({ full_name: 'Asha Reddy', consent_given: true });
  });

  it('approveExtraction sends the rejected entity indexes', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ merged: {} }));
    await api.approveExtraction('pat-1', 'doc-1', [0, 2]);
    expect(callAt().url).toBe(`${PREFIX}/patients/pat-1/documents/doc-1/approve`);
    expect(callAt().json).toEqual({ corrections: [], rejected_entity_indexes: [0, 2] });
  });

  it('approveExtraction defaults to rejecting nothing', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ merged: {} }));
    await api.approveExtraction('pat-1', 'doc-1');
    expect(callAt().json.rejected_entity_indexes).toEqual([]);
  });

  it('checkDrugSafety posts the proposed drug for the patient', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await api.checkDrugSafety('pat-1', { drug_name: 'Brufen' });
    expect(callAt().url).toBe(`${PREFIX}/patients/pat-1/drug-safety/check`);
    expect(callAt().json).toEqual({ drug_name: 'Brufen' });
  });

  it('startReasoning posts the presenting complaint', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await api.startReasoning('pat-1', 'chest pain');
    expect(callAt().url).toBe(`${PREFIX}/patients/pat-1/reasoning`);
    expect(callAt().json).toEqual({ presenting_complaint: 'chest pain' });
  });

  it('submitIntakeAnswers posts the answer set', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await api.submitIntakeAnswers('sess-1', [{ question_id: 'q1', answer_text: 'yes' }]);
    expect(callAt().url).toBe(`${PREFIX}/reasoning/sess-1/intake/answers`);
    expect(callAt().json).toEqual({ answers: [{ question_id: 'q1', answer_text: 'yes' }] });
  });

  it('recordDecision posts the decision and normalises a missing reason to null', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await api.recordDecision('sess-1', 'sug-1', 'accepted');
    expect(callAt().url).toBe(`${PREFIX}/reasoning/sess-1/suggestions/sug-1/decision`);
    expect(callAt().json).toEqual({ decision: 'accepted', reason: null });
  });

  it('recordDecision forwards an override reason when one is given', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await api.recordDecision('sess-1', 'sug-1', 'overridden', 'Allergy documented in error');
    expect(callAt().json.reason).toBe('Allergy documented in error');
  });

  it('fileSafetyReport posts the report payload', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({}));
    await api.fileSafetyReport({ category: 'usability', severity: 'near_miss', description: 'x' });
    expect(callAt().url).toBe(`${PREFIX}/safety-reports`);
    expect(callAt().json).toEqual({
      category: 'usability',
      severity: 'near_miss',
      description: 'x',
    });
  });

  it('mints a session-scoped stream token rather than putting the access token in the URL', async () => {
    seedTokens('real-access-token', 'r1');
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ token: 'scoped.stream.jwt', expires_in: 60 }),
    );

    const url = await api.reasoningStreamUrl('sess-1');

    // The mint request is authenticated with the bearer header, not the query string.
    expect(callAt().url).toBe(`${PREFIX}/reasoning/sess-1/stream-token`);
    expect(callAt().method).toBe('POST');
    expect(callAt().headers.get('Authorization')).toBe('Bearer real-access-token');

    expect(url).toBe(`${PREFIX}/reasoning/sess-1/stream?token=scoped.stream.jwt`);
    expect(url).not.toContain('real-access-token');
  });

  it('percent-encodes the minted token so an odd JWT cannot break the URL', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ token: 'a+b/c=d&e', expires_in: 60 }),
    );
    const url = await api.reasoningStreamUrl('sess-1');
    expect(url).toBe(`${PREFIX}/reasoning/sess-1/stream?token=a%2Bb%2Fc%3Dd%26e`);
  });

  it('propagates a failure to mint rather than returning an unauthenticated URL', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ code: 'not_found' }, 404));
    await expect(api.reasoningStreamUrl('sess-1')).rejects.toBeInstanceOf(ApiError);
  });

  it('builds the SaMD dossier URL for the requested format', () => {
    expect(api.samdDossierUrl()).toBe(`${PREFIX}/regulatory/samd-dossier?format=markdown`);
    expect(api.samdDossierUrl('json')).toBe(`${PREFIX}/regulatory/samd-dossier?format=json`);
  });
});

describe('logout', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => vi.unstubAllGlobals());

  it('revokes the refresh token server-side then clears local state', async () => {
    seedTokens('a1', 'r1');
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.logout();

    expect(callAt().url).toBe(`${PREFIX}/auth/logout`);
    expect(callAt().json).toEqual({ refresh_token: 'r1' });
    expect(tokenStore.access).toBeNull();
    expect(tokenStore.refresh).toBeNull();
  });

  it('still clears local state when the server-side revoke fails', async () => {
    seedTokens('a1', 'r1');
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await expect(api.logout()).resolves.toBeUndefined();
    expect(tokenStore.access).toBeNull();
  });

  it('skips the network call entirely when there is nothing to revoke', async () => {
    await api.logout();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe('downloadSamdDossier', () => {
  let click: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
    vi.stubGlobal('URL', Object.assign(globalThis.URL, {
      createObjectURL: vi.fn(() => 'blob:dossier'),
      revokeObjectURL: vi.fn(),
    }));
    click = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(click);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('downloads the dossier as a named markdown file and releases the object URL', async () => {
    seedTokens('a1');
    vi.mocked(fetch).mockResolvedValueOnce(new Response('# Dossier', { status: 200 }));

    await api.downloadSamdDossier();

    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toBe(`${PREFIX}/regulatory/samd-dossier?format=markdown`);
    expect((init?.headers as Record<string, string>)['Authorization']).toBe('Bearer a1');
    expect(click).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dossier');
    // The anchor is removed again so repeated downloads do not litter the DOM.
    expect(document.querySelectorAll('a[download]')).toHaveLength(0);
  });

  it('refreshes once and retries when the dossier request is rejected as stale', async () => {
    seedTokens('stale', 'r1');
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(
        jsonResponse({ access_token: 'fresh', refresh_token: 'r2', token_type: 'bearer', expires_in: 900 }),
      )
      .mockResolvedValueOnce(new Response('{}', { status: 200 }));

    await api.downloadSamdDossier('json');

    expect(fetch).toHaveBeenCalledTimes(3);
    expect(tokenStore.access).toBe('fresh');
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('raises an ApiError rather than downloading an error page', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(null, { status: 500, statusText: 'Internal Server Error' }),
    );

    await expect(api.downloadSamdDossier()).rejects.toMatchObject({
      status: 500,
      code: 'download_error',
    });
    expect(click).not.toHaveBeenCalled();
  });
});
