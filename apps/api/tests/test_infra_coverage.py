"""Infrastructure and API-boundary branches the suite never reached.

Three clusters, all listed as missing in the full-suite coverage report:

* **PostgreSQL-only code paths.** The suite runs on SQLite, so every ``if dialect.name ==
  "postgresql"`` branch in ``app/db/`` and the ``pg_advisory_xact_lock`` in the audit chain are
  dead in tests but are the *only* paths that run in production. They are exercised here against
  a stub dialect rather than a live server.
* **Provider request shapes.** ``app/agents/llm.py`` builds each SDK request; the existing
  robustness tests stop at client construction, so the request body itself was unasserted —
  including ``temperature=0.0``, which is what makes clinical output reproducible.
* **Optional-dependency fallbacks.** Guideline embedding and Qdrant indexing must degrade to a
  lexical corpus when those packages are absent, which is the normal state in CI and offline.
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents import demo_data, llm
from app.config import Settings, settings
from app.db import session as db_session
from app.db.types import GUID, INETType, dumps
from app.exceptions import TokenError
from app.models.user import Account, Session
from app.services import guideline_ingest
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.document_service import _band as doc_confidence_band
from app.services.extraction.pipeline import _confidence_band as pipeline_confidence_band
from app.services.extraction.pipeline import _pdf_text
from tests.conftest import create_patient

PRESCRIPTION = (
    b"%PDF-1.4\n"
    b"MEDICATIONS:\n"
    b"Glycomet 500mg BD\n"
    b"LABS:\n"
    b"Creatinine: 3.0 mg/dL (0.6-1.2)\n"
    b"CONDITIONS:\n"
    b"Type 2 Diabetes Mellitus\n"
)


class _PGDialect:
    """The bare minimum of the SQLAlchemy dialect protocol these type decorators touch."""

    name = "postgresql"

    @staticmethod
    def type_descriptor(impl):
        return impl


class _SQLiteDialect(_PGDialect):
    name = "sqlite"


# --------------------------------------------------------------- portable column types


def test_guid_emits_native_uuid_on_postgres_and_hex_char_on_sqlite():
    """The same ORM model must map to PG ``UUID`` and SQLite ``CHAR(32)``.

    A regression here is invisible in tests (SQLite keeps working) and corrupts every id
    column in production.
    """
    pg_impl = GUID().load_dialect_impl(_PGDialect())
    sqlite_impl = GUID().load_dialect_impl(_SQLiteDialect())

    assert pg_impl.__class__.__name__ == "UUID"
    assert getattr(pg_impl, "as_uuid", None) is True
    assert sqlite_impl.__class__.__name__ == "CHAR"
    assert sqlite_impl.length == 32


@pytest.mark.parametrize("as_string", [False, True])
def test_guid_binds_a_real_uuid_object_on_postgres(as_string):
    """asyncpg needs a ``uuid.UUID``, not the hex string SQLite stores."""
    value = uuid.uuid4()
    bound = GUID().process_bind_param(str(value) if as_string else value, _PGDialect())

    assert isinstance(bound, uuid.UUID)
    assert bound == value


def test_guid_binds_hex_on_sqlite_and_passes_none_through():
    value = uuid.uuid4()
    assert GUID().process_bind_param(value, _SQLiteDialect()) == value.hex
    assert GUID().process_bind_param(str(value), _SQLiteDialect()) == value.hex
    assert GUID().process_bind_param(None, _PGDialect()) is None
    assert GUID().process_bind_param(None, _SQLiteDialect()) is None


def test_guid_reads_back_a_uuid_from_either_dialect_representation():
    value = uuid.uuid4()
    # PostgreSQL hands back a UUID object already — it must not be re-parsed.
    assert GUID().process_result_value(value, _PGDialect()) is value
    # SQLite hands back the hex string.
    assert GUID().process_result_value(value.hex, _SQLiteDialect()) == value
    assert GUID().process_result_value(None, _PGDialect()) is None


def test_inet_type_emits_inet_on_postgres_and_varchar_on_sqlite():
    assert INETType().load_dialect_impl(_PGDialect()).__class__.__name__ == "INET"
    sqlite_impl = INETType().load_dialect_impl(_SQLiteDialect())
    assert sqlite_impl.__class__.__name__ == "String"
    assert sqlite_impl.length == 64


def test_dumps_serialises_values_json_cannot_handle_natively():
    """Audit payloads carry UUIDs and datetimes; ``dumps`` must not raise on them."""
    ident = uuid.uuid4()
    stamp = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    out = json.loads(dumps({"id": ident, "at": stamp, "n": 1}))

    assert out["id"] == str(ident)
    assert out["at"].startswith("2026-08-10")
    assert out["n"] == 1


# --------------------------------------------------------------- engine / session factory


def test_postgres_urls_get_pool_settings_and_sqlite_urls_do_not(monkeypatch):
    """Pool sizing is only meaningful (and only legal) on the PostgreSQL driver."""
    recorded: list[tuple[str, dict]] = []

    def _fake_create_async_engine(url, **kwargs):
        recorded.append((url, kwargs))
        return object()

    monkeypatch.setattr(db_session, "create_async_engine", _fake_create_async_engine)

    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@localhost:5432/aether")
    monkeypatch.setattr(settings, "database_pool_size", 7)
    monkeypatch.setattr(settings, "database_max_overflow", 3)
    db_session._make_engine()

    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite://")
    db_session._make_engine()

    pg_kwargs, sqlite_kwargs = recorded[0][1], recorded[1][1]
    assert pg_kwargs["pool_size"] == 7
    assert pg_kwargs["max_overflow"] == 3
    assert pg_kwargs["pool_pre_ping"] is True
    assert "pool_size" not in sqlite_kwargs


async def test_get_db_yields_a_session_and_rolls_back_when_the_handler_raises(monkeypatch):
    """A failing request must not leave a half-applied transaction on the pooled connection."""
    rolled_back: list[bool] = []

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def rollback(self):
            rolled_back.append(True)

    monkeypatch.setattr(db_session, "_sessionmaker", lambda: _FakeSession())

    agen = db_session.get_db()
    session = await agen.__anext__()
    assert isinstance(session, _FakeSession)

    with pytest.raises(RuntimeError):
        await agen.athrow(RuntimeError("handler blew up"))
    assert rolled_back == [True]


async def test_get_db_does_not_roll_back_a_successful_request(monkeypatch):
    rolled_back: list[bool] = []

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def rollback(self):  # pragma: no cover — asserted not to run
            rolled_back.append(True)

    monkeypatch.setattr(db_session, "_sessionmaker", lambda: _FakeSession())

    agen = db_session.get_db()
    await agen.__anext__()
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    assert rolled_back == []


async def test_dispose_engine_clears_the_cached_engine_and_sessionmaker(monkeypatch):
    """Shutdown must release pooled connections and reset the module globals."""
    disposed: list[bool] = []

    class _FakeEngine:
        async def dispose(self):
            disposed.append(True)

    monkeypatch.setattr(db_session, "_engine", _FakeEngine())
    monkeypatch.setattr(db_session, "_sessionmaker", object())

    await db_session.dispose_engine()

    assert disposed == [True]
    assert db_session._engine is None
    assert db_session._sessionmaker is None


async def test_dispose_engine_is_a_no_op_when_no_engine_was_created(monkeypatch):
    monkeypatch.setattr(db_session, "_engine", None)
    await db_session.dispose_engine()  # must not raise


# --------------------------------------------------------------- audit chain locking


async def test_audit_takes_a_transaction_advisory_lock_only_on_postgres(db, monkeypatch):
    """The hash chain needs serialised appends; on PostgreSQL that is an advisory lock.

    Without the lock two concurrent appends can read the same ``prev_hash`` and fork the chain,
    which the integrity verifier then reports as tampering.
    """
    executed: list[tuple[str, dict | None]] = []

    class _FakeDB:
        """Only the two attributes ``AuditService._lock`` touches."""

        def __init__(self, dialect_name: str) -> None:
            self.bind = types.SimpleNamespace(dialect=types.SimpleNamespace(name=dialect_name))

        async def execute(self, statement, params=None):
            executed.append((str(statement), params))
            return None

    # The real test bind is SQLite: no advisory lock is attempted (it would error).
    await AuditService(_FakeDB("sqlite"))._lock()
    assert executed == []

    await AuditService(_FakeDB("postgresql"))._lock()
    assert len(executed) == 1
    statement, params = executed[0]
    assert "pg_advisory_xact_lock" in statement
    assert params is not None and "k" in params

    # An unbound session must not attempt the lock either.
    unbound = _FakeDB("postgresql")
    unbound.bind = None
    await AuditService(unbound)._lock()
    assert len(executed) == 1


# --------------------------------------------------------------- refresh-token expiry


async def test_an_absolutely_expired_refresh_token_is_rejected(db):
    """Past its absolute TTL a refresh token must fail even if it was never idle or revoked."""
    account = Account(email=f"exp-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()

    service = AuthService(db)
    tokens = await service._issue_tokens(account)
    stored = (
        (await db.execute(select(Session).where(Session.account_id == account.id)))
        .scalars()
        .first()
    )
    assert stored is not None

    now = datetime.now(UTC)
    stored.expires_at = now - timedelta(minutes=1)
    stored.last_used_at = now  # not idle — only the absolute TTL has passed
    await db.flush()

    with pytest.raises(TokenError):
        await service.refresh(tokens.refresh_token)


# --------------------------------------------------------------- confidence banding


@pytest.mark.parametrize(
    "band_fn", [doc_confidence_band, pipeline_confidence_band], ids=["document", "pipeline"]
)
def test_confidence_bands_split_high_medium_and_low(band_fn, monkeypatch):
    """Both copies of the banding rule must agree — the UI gates confirmation on the band."""
    monkeypatch.setattr(settings, "confirmation_confidence_threshold", 0.9)
    monkeypatch.setattr(settings, "ocr_fallback_threshold", 0.6)

    assert band_fn(0.95) == "high"
    assert band_fn(0.9) == "high"
    assert band_fn(0.7) == "medium"
    assert band_fn(0.6) == "medium"
    assert band_fn(0.59) == "low"
    assert band_fn(0.0) == "low"


def test_pdf_text_returns_empty_string_for_bytes_that_are_not_a_pdf():
    """A mis-typed or truncated upload must degrade to "" so OCR fallback can take over."""
    assert _pdf_text(b"not a pdf at all") == ""


def test_pdf_text_extracts_text_from_a_stubbed_reader(monkeypatch):
    import pypdf

    class _Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _Reader:
        def __init__(self, _stream):
            self.pages = [_Page("MEDICATIONS:"), _Page(None), _Page("Glycomet 500mg BD")]

    monkeypatch.setattr(pypdf, "PdfReader", _Reader)

    # A page yielding None must contribute an empty line, not crash the join.
    assert _pdf_text(b"%PDF-1.4") == "MEDICATIONS:\n\nGlycomet 500mg BD"


# --------------------------------------------------------------- LLM request shapes


def test_openai_request_pins_temperature_to_zero_and_splits_system_from_user(monkeypatch):
    """Clinical output must be reproducible, which requires temperature 0.0."""
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    captured: dict = {}

    class _Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            message = types.SimpleNamespace(content='{"ok": true}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class _FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = types.SimpleNamespace(completions=_Completions())

    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)

    out = llm._complete_openai("SYSTEM", "USER", "gpt-4o", 512)

    assert out == '{"ok": true}'
    assert captured["temperature"] == 0.0
    assert captured["model"] == "gpt-4o"
    assert captured["max_tokens"] == 512
    assert captured["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]


def test_openai_request_tolerates_a_null_message_content(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")

    class _Completions:
        @staticmethod
        def create(**_kwargs):
            message = types.SimpleNamespace(content=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class _FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = types.SimpleNamespace(completions=_Completions())

    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    assert llm._complete_openai("s", "u", "gpt-4o", 10) == ""


def test_anthropic_request_pins_temperature_and_keeps_only_text_blocks(monkeypatch):
    """A thinking/tool block in the response must not be concatenated into the JSON payload."""
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    captured: dict = {}

    class _Messages:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(type="text", text='{"ok":'),
                    types.SimpleNamespace(type="thinking", text="IGNORE ME"),
                    types.SimpleNamespace(type="text", text=" true}"),
                ]
            )

    class _FakeAnthropic:
        def __init__(self, **_kwargs):
            self.messages = _Messages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    out = llm._complete_anthropic("SYSTEM", "USER", "claude-sonnet-4", 256)

    assert out == '{"ok": true}'
    assert "IGNORE ME" not in out
    assert captured["temperature"] == 0.0
    assert captured["system"] == "SYSTEM"
    assert captured["max_tokens"] == 256
    assert captured["messages"] == [{"role": "user", "content": "USER"}]


# --------------------------------------------------------------- provider configuration


def test_llm_configured_accepts_any_single_enabled_provider_key():
    assert Settings(openai_api_key="k").llm_configured is True
    assert (
        Settings(openai_api_key="", anthropic_api_key="k", llm_fallback_enabled=True).llm_configured
        is True
    )
    assert (
        Settings(
            openai_api_key="",
            openrouter_api_key="k",
            llm_openrouter_fallback=True,
        ).llm_configured
        is True
    )


def test_llm_configured_ignores_a_key_whose_fallback_tier_is_switched_off():
    """A disabled tier's key must not count as configured — it will never be called."""
    anthropic_only = Settings(
        openai_api_key="",
        anthropic_api_key="k",
        openrouter_api_key="",
        llm_fallback_enabled=False,
    )
    assert anthropic_only.llm_configured is False

    openrouter_only = Settings(
        openai_api_key="",
        anthropic_api_key="",
        openrouter_api_key="k",
        llm_openrouter_fallback=False,
    )
    assert openrouter_only.llm_configured is False

    no_keys = Settings(openai_api_key="", anthropic_api_key="", openrouter_api_key="")
    assert no_keys.llm_configured is False


# --------------------------------------------------------------- optional-dependency fallbacks


def test_guideline_embedding_falls_back_to_lexical_when_sentence_transformers_is_absent(
    monkeypatch,
):
    """Missing the embedding package must yield ``None`` (lexical search), never an exception."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert guideline_ingest._maybe_embed(["hydration and platelet monitoring"]) is None


def test_qdrant_push_is_skipped_when_the_client_package_is_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "qdrant_client", None)
    # Returns without raising; ingestion still completes and rows land in PostgreSQL.
    assert guideline_ingest._push_qdrant("icmr-2024.1", [{"section_id": "S1"}], [[0.1]]) is None


def test_qdrant_push_creates_the_versioned_collection_and_upserts_every_record(monkeypatch):
    """Corpus version must be namespaced into the collection so versions cannot collide."""
    monkeypatch.setattr(settings, "qdrant_collection", "guidelines")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")
    calls: dict = {}

    class _FakeClient:
        def __init__(self, url):
            calls["url"] = url

        def recreate_collection(self, collection_name, vectors_config):
            calls["collection"] = collection_name
            calls["size"] = vectors_config.size

        def upsert(self, collection_name, points):
            calls["points"] = len(points)

    fake_models = types.SimpleNamespace(
        Distance=types.SimpleNamespace(COSINE="Cosine"),
        PointStruct=lambda id, vector, payload: (id, vector, payload),
        VectorParams=lambda size, distance: types.SimpleNamespace(size=size, distance=distance),
    )
    monkeypatch.setitem(
        sys.modules, "qdrant_client", types.SimpleNamespace(QdrantClient=_FakeClient)
    )
    monkeypatch.setitem(sys.modules, "qdrant_client.models", fake_models)

    guideline_ingest._push_qdrant(
        "icmr-2024.1",
        [{"section_id": "S1"}, {"section_id": "S2"}],
        [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
    )

    assert calls["url"] == "http://qdrant:6333"
    assert calls["collection"] == "guidelines_icmr-2024_1"  # dots are not legal in a name
    assert calls["size"] == 3
    assert calls["points"] == 2


# --------------------------------------------------------------- simulated demo payloads


def test_demo_detect_specialty_returns_none_for_a_prompt_naming_no_specialty():
    assert demo_data._detect_specialty("You are the INDEPENDENT verifier.") is None


def test_demo_hypothesis_payload_unions_every_specialty_when_none_is_named():
    """A specialty-less prompt must still yield a differential, not an empty list."""
    scn = demo_data.select_scenario("fever and cough")
    payload = demo_data._hypothesis_payload(scn, "no specialty named here")

    expected = sum(len(v) for v in scn.hypotheses_by_specialty.values())
    assert len(payload["hypotheses"]) == expected
    assert expected > 0


def test_demo_leading_hypothesis_is_read_back_from_the_user_prompt():
    """The devil's-advocate demo payload must attack the hypothesis actually being led."""
    user = "Leading hypothesis: Dengue fever\nPresenting complaint: fever"
    assert demo_data._leading_from_user(user, "fallback") == "Dengue fever"
    # A blank value or a missing line falls back rather than returning "".
    assert demo_data._leading_from_user("Leading hypothesis:   ", "fallback") == "fallback"
    assert demo_data._leading_from_user("no such line", "fallback") == "fallback"


def test_demo_response_for_an_unrecognised_prompt_is_empty_but_still_marked():
    """Every simulated payload must carry ``_demo`` so downstream code can label it."""
    payload = demo_data.simulated_response("an unrecognised system prompt", "user text")

    assert payload["_demo"] is True
    assert set(payload) == {"_demo"}


# --------------------------------------------------------------- API boundary


async def test_fetching_a_single_document_returns_its_metadata(auth_client):
    """The by-id document read had no test; it is what the timeline drill-down calls."""
    patient = await create_patient(auth_client)
    uploaded = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    doc_id = uploaded.json()["id"]

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == doc_id
    assert body["file_name"] == "rx.pdf"


async def test_fetching_a_document_that_belongs_to_another_patient_is_a_404(auth_client):
    patient_a = await create_patient(auth_client)
    patient_b = await create_patient(auth_client, full_name="Other Patient")
    uploaded = await auth_client.post(
        f"/api/v1/patients/{patient_a['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    doc_id = uploaded.json()["id"]

    resp = await auth_client.get(f"/api/v1/patients/{patient_b['id']}/documents/{doc_id}")

    assert resp.status_code == 404


async def test_active_flags_returns_a_flag_for_each_interacting_current_medication(auth_client):
    """The GET-flags endpoint re-derives pairwise safety across the whole current med list."""
    patient = await create_patient(auth_client)
    doc = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={
            "file": (
                "rx.pdf",
                b"%PDF-1.4\nMEDICATIONS:\nWarfarin 5mg OD\nAspirin 75mg OD\n",
                "application/pdf",
            )
        },
    )
    assert doc.status_code == 201, doc.text
    approved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approved.status_code == 200, approved.text

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/drug-safety/flags")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patient_id"] == patient["id"]
    assert isinstance(body["flags"], list)
    for flag in body["flags"]:
        assert flag["summary"]
        # Safety Rule #4: no imperative clinical language in any flag microcopy.
        assert not flag["summary"].lower().startswith(("give ", "administer ", "prescribe "))
