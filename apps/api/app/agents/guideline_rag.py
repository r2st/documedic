"""Agent 6: Guideline-RAG — guideline-grounded, cited management options.

Retrieves chunks via the injected retriever (Qdrant in production, deterministic lexical
fallback otherwise — Phase 3). Management options are STRICTLY grounded in retrieved chunks;
when retrieval confidence is below threshold the agent emits an explicit "insufficient
guideline support" notice rather than inventing guidance. Citation faithfulness (the share of
options whose cited section_ids were actually retrieved) is computed and stored.
"""

from __future__ import annotations

from app.agents import demo_data
from app.agents.context import ReasoningContext
from app.agents.llm import using_simulated_llm
from app.agents.prompts import GUIDELINE_RAG
from app.agents.state import CaseState, GuidelineChunkRef, ManagementOption
from app.agents.util import call_llm

AGENT = "guideline_rag"


def _query(state: CaseState) -> str:
    leaders = state.leading_hypotheses(3)
    dx = ", ".join(h.diagnosis_name for h in leaders)
    return f"management of {dx} | {state.presenting_complaint}"


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Guideline-RAG"})
    chunks_raw = ctx.retrieve(_query(state), 6)
    retrieved = [c for c in chunks_raw if float(c.get("score", 0)) >= ctx.retrieval_threshold]
    retrieved_ids = {c["section_id"] for c in retrieved}

    refs = {
        c["section_id"]: GuidelineChunkRef(
            section_id=c["section_id"],
            source=c.get("source", "icmr"),
            document_title=c.get("document_title", ""),
            heading=c.get("heading"),
            snippet=(c.get("content", "")[:280]),
            score=float(c.get("score", 0)),
            corpus_version=c.get("corpus_version", ""),
            page_range=c.get("page_range"),
        )
        for c in retrieved
    }

    options: list[ManagementOption] = []
    insufficient = not retrieved

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
            grounded_by_model = True
            insufficient = bool(result.get("insufficient_support", False))
            for opt in result.get("options", []):
                text = (opt.get("text") or "").strip()
                if not text:
                    continue
                cited = [refs[sid] for sid in opt.get("citation_section_ids", []) if sid in refs]
                options.append(
                    ManagementOption(
                        text=text,
                        citations=cited,
                        sufficient_support=bool(opt.get("sufficient_support", bool(cited))),
                    )
                )
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
    state.citation_faithfulness = _faithfulness(options, retrieved_ids)

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


def _faithfulness(options: list[ManagementOption], retrieved_ids: set[str]) -> float:
    """Share of cited section_ids that were actually retrieved (target ≥0.95)."""
    cited = [c.section_id for o in options for c in o.citations]
    if not cited:
        return 1.0
    grounded = sum(1 for sid in cited if sid in retrieved_ids)
    return round(grounded / len(cited), 4)
