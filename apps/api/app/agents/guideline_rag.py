"""Agent 6: Guideline-RAG — guideline-grounded, cited management options.

Retrieves chunks via the injected retriever (Qdrant in production, deterministic lexical
fallback otherwise — Phase 3). Management options are STRICTLY grounded in retrieved chunks;
when retrieval confidence is below threshold the agent emits an explicit "insufficient
guideline support" notice rather than inventing guidance. Citation faithfulness (the share of
the section_ids the model CLAIMED to cite that were actually retrieved) is computed and stored;
an option left with no surviving citation cannot be presented as sufficiently supported.
"""

from __future__ import annotations

from app.agents import demo_data
from app.agents.context import ReasoningContext
from app.agents.llm import using_simulated_llm
from app.agents.prompts import GUIDELINE_RAG
from app.agents.state import CaseState, GuidelineChunkRef, ManagementOption
from app.agents.util import as_float, as_text, call_llm, objects

AGENT = "guideline_rag"


def _query(state: CaseState) -> str:
    leaders = state.leading_hypotheses(3)
    dx = ", ".join(h.diagnosis_name for h in leaders)
    return f"management of {dx} | {state.presenting_complaint}"


def _citable(chunks: object) -> list[dict]:
    """The retrieved chunks that can actually be cited, in retrieval order.

    ``ctx.retrieve`` is an injection point, not app-internal code: the lexical retriever always
    returns well-formed dicts, but the production path is dense search in Qdrant, whose payloads
    are whatever the ingestion wrote. A chunk with no ``section_id`` raised KeyError building the
    ``refs`` map, and a non-numeric ``score`` raised ValueError one line earlier -- both inside a
    node with no edge around it, so a single malformed payload failed the case rather than
    costing it one citation. A chunk that cannot be cited is dropped, which is what the
    below-threshold chunks beside it already are.
    """
    return [c for c in objects(chunks) if isinstance(c.get("section_id"), str) and c["section_id"]]


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Guideline-RAG"})
    chunks_raw = _citable(ctx.retrieve(_query(state), 6))
    retrieved = [c for c in chunks_raw if as_float(c.get("score"), 0.0) >= ctx.retrieval_threshold]
    retrieved_ids = {c["section_id"] for c in retrieved}

    refs = {
        c["section_id"]: GuidelineChunkRef(
            section_id=c["section_id"],
            source=c.get("source", "icmr"),
            document_title=c.get("document_title", ""),
            heading=c.get("heading"),
            snippet=(c.get("content", "")[:280]),
            score=as_float(c.get("score"), 0.0),
            corpus_version=c.get("corpus_version", ""),
            page_range=c.get("page_range"),
        )
        for c in retrieved
    }

    options: list[ManagementOption] = []
    insufficient = not retrieved

    # Every section_id the model said it was citing, before the ones it invented are dropped.
    # This is what ``_faithfulness`` has to be measured against; see there.
    claimed_ids: list[str] = []
    grounded_by_model = False
    if retrieved and ctx.llm_available():
        excerpts = "\n\n".join(
            f"[{c['section_id']}] {c.get('heading') or ''}: {c.get('content', '')}"
            for c in retrieved
        )
        result = await call_llm(
            ctx,
            GUIDELINE_RAG,
            f"Case: {_query(state)}\n\nRetrieved guideline excerpts:\n{excerpts}",
        )
        if result:
            raw_options = result.get("options")
            insufficient = bool(result.get("insufficient_support", False))
            for opt in objects(raw_options):
                text = as_text(opt.get("text"))
                if not text:
                    continue
                # Only string ids are looked up: an unhashable id (a list, an object) would
                # raise TypeError on the ``in`` test against a dict, in the node that grounds
                # every management option the clinician is shown.
                raw_ids = opt.get("citation_section_ids")
                ids = [
                    sid
                    for sid in (raw_ids if isinstance(raw_ids, (list | tuple)) else [])
                    if isinstance(sid, str)
                ]
                claimed_ids.extend(ids)
                cited = [refs[sid] for sid in ids if sid in refs]
                options.append(
                    ManagementOption(
                        text=text,
                        citations=cited,
                        # The model may declare its own option under-supported, but it cannot
                        # declare an *uncited* one supported. Every citation it named can be one
                        # it invented, in which case ``cited`` is empty and the option carries no
                        # grounding at all -- and ``synthesis`` reads this flag to decide whether
                        # the option is shown at the case tier or escalated to flag-for-review.
                        # An option with nothing behind it goes to the clinician marked as such
                        # (Critical Safety Rule #2: the conservative reading wins).
                        sufficient_support=bool(cited)
                        and bool(opt.get("sufficient_support", True)),
                    )
                )
            # An answer nothing survived parsing from is not an answer of "nothing". A model
            # that offered options in a shape this agent cannot read (a list of bare strings)
            # left ``options`` empty for the same reason a dead provider does, and treating
            # that as a deliberate finding suppressed the deterministic grounding below --
            # so the clinician saw no management options from a corpus that had already been
            # retrieved and scored. Only an empty (or absent) options field is taken as a
            # deliberate "nothing to offer".
            grounded_by_model = bool(options) or not raw_options
    # Gated on whether the model actually answered, not on whether a key exists: with a keyed
    # provider that was down, the deterministic grounding below was skipped and the clinician
    # got no management options at all, from a corpus that had already been retrieved. A model
    # that *did* answer and deliberately offered nothing is left alone — overriding that with
    # raw chunks would contradict its own insufficient-support finding.
    if retrieved and not grounded_by_model:
        # Deterministic grounding: present each retrieved chunk as a cited option.
        state.degraded = True
        for c in retrieved:
            options.append(
                ManagementOption(
                    text=(
                        "Guidelines support considering: "
                        f"{c.get('heading') or c.get('document_title')} — "
                        f"{c.get('content', '')[:200]}"
                    ),
                    citations=[refs[c["section_id"]]],
                    sufficient_support=True,
                )
            )

    # Demo safety net: when running on simulated data with no guideline corpus available,
    # still surface illustrative, clearly-marked management options so the UI renders fully.
    if not options and using_simulated_llm():
        state.demo_mode = True
        scn = demo_data.select_scenario(_query(state))
        demo_ref = GuidelineChunkRef(
            section_id="DEMO-STW-0",
            source="demo",
            document_title=f"{demo_data.DEMO_TAG} Illustrative guideline excerpt",
            heading="Simulated management guidance",
            snippet=(
                f"{demo_data.DEMO_TAG} Sample content shown because no guideline "
                "corpus is available."
            ),
            score=1.0,
            corpus_version="demo",
            page_range=None,
        )
        for text in scn.management:
            options.append(
                ManagementOption(text=text, citations=[demo_ref], sufficient_support=True)
            )
        insufficient = False

    state.management_options.extend(options)
    state.citation_faithfulness = _faithfulness(claimed_ids, retrieved_ids)

    if insufficient or not options:
        state.add_trace(AGENT, "Insufficient guideline support for cited management", {})
        await ctx.emit(
            "management",
            {"agent": AGENT, "insufficient_support": True, "options": [], "faithfulness": 1.0},
        )
    else:
        state.add_trace(
            AGENT,
            f"Drafted {len(options)} cited management options",
            {"citation_faithfulness": state.citation_faithfulness},
        )
        await ctx.emit(
            "management",
            {
                "agent": AGENT,
                "insufficient_support": False,
                "faithfulness": state.citation_faithfulness,
                "options": [
                    {
                        "text": o.text,
                        "sufficient_support": o.sufficient_support,
                        "citations": [
                            {
                                "section_id": c.section_id,
                                "source": c.source,
                                "document_title": c.document_title,
                                "heading": c.heading,
                                "snippet": c.snippet,
                                "score": c.score,
                            }
                            for c in o.citations
                        ],
                    }
                    for o in options
                ],
            },
        )
    await ctx.emit("agent_complete", {"agent": AGENT})


def _faithfulness(claimed_ids: list[str], retrieved_ids: set[str]) -> float:
    """Share of the section_ids the model CLAIMED that were actually retrieved (target ≥0.95).

    Measured against the model's claims, not against the citations that survived. This used to
    read ``options[].citations``, which are built as ``refs[sid] for sid in ... if sid in refs``
    -- already filtered to the retrieved set -- so the ratio was 1.0 by construction. It could
    not go down, which means it could not detect the one thing it exists to detect: a model
    citing a guideline section that was never retrieved. A run where every citation was invented
    and every one was dropped reported perfect faithfulness.

    Filtering the invented ids out of the *output* is right and stays: no clinician should ever
    be shown a citation to a section the corpus did not return. But the filter is a defence, not
    a measurement, and the metric is what tells us how often the defence had to fire.

    No claims means nothing was asserted to be faithful -- the deterministic grounding path
    cites chunks by construction, and demo output cites a clearly-marked sample -- so both
    report 1.0 rather than dividing by zero.
    """
    if not claimed_ids:
        return 1.0
    grounded = sum(1 for sid in claimed_ids if sid in retrieved_ids)
    return round(grounded / len(claimed_ids), 4)
