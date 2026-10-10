"""`_agent_md` carries a hand-written `trigger:` into `description:`.

Claude Code shows `description:` to an orchestrator choosing WHICH subagent to
invoke, so the field has to answer "when do I pick this one". Deriving it from
the body's first sentence answers "what does this one do" instead, and the two
come apart hardest on the agents where the trigger is the whole point: an
ingester's trigger is the arrival of a statement file, while its first paragraph
is a sentence about a database write.

Every agent authored in the KB to date was written body-first with no `trigger:`,
so the derivation is still the fallback and stays pinned here. The field starts
mattering with the repo-native definitions moving into the Reference Room, which
carry hand-written descriptions that the derivation would drop.
"""
from __future__ import annotations

import pathlib


def _write(tmp_path: pathlib.Path, frontmatter: str, body: str) -> pathlib.Path:
    p = tmp_path / "agent.md"
    p.write_text(f"---\n{frontmatter}\n---\n\n{body}")
    return p


def _render(knowledge_ctx, kind: str, row: dict, src: pathlib.Path) -> str:
    """The render a Library document gets: its frontmatter and body, as the
    Library serves them, through the same function a checkout file never reaches."""
    fm, body = knowledge_ctx.split_frontmatter(src.read_text())
    render = knowledge_ctx._agent_md if kind == "agent" else knowledge_ctx._skill_md
    return render(row, fm, body)


def test_trigger_becomes_the_description(knowledge_ctx, tmp_path):
    src = _write(
        tmp_path,
        "id: cfo/agents/cfo-ingest\n"
        "kind: agent\n"
        "trigger: use when the operator has a downloaded bank or card statement to load",
        "You load a single statement file into the CFO database.\n",
    )
    out = _render(knowledge_ctx, "agent", {"id": "cfo/agents/cfo-ingest",
                                         "title": "CFO Ingest"}, src)
    assert "description: use when the operator has a downloaded bank or card statement to load" in out
    assert "You load a single statement file" not in out.split("---")[1]


def test_derivation_still_applies_without_a_trigger(knowledge_ctx, tmp_path):
    """The 30 KB-native agents have no `trigger:`; none of them may change."""
    src = _write(
        tmp_path,
        "id: convivy-orient/agents/orient-coder\nkind: agent",
        "You implement features for Orient. Read `CLAUDE.md` first.\n",
    )
    out = _render(knowledge_ctx, "agent", {"id": "convivy-orient/agents/orient-coder",
                                         "title": "Orient Coder"}, src)
    assert "description: You implement features for Orient." in out


def test_an_empty_trigger_falls_back_rather_than_rendering_blank(knowledge_ctx, tmp_path):
    """`trigger:` with nothing after it parses as None, and a blank description
    would leave the orchestrator with no basis to choose the agent at all —
    strictly worse than the derived sentence."""
    src = _write(
        tmp_path,
        "id: x/agents/y\nkind: agent\ntrigger:",
        "You do the thing that needs doing.\n",
    )
    out = _render(knowledge_ctx, "agent", {"id": "x/agents/y", "title": "Y"}, src)
    assert "description: You do the thing that needs doing." in out


def test_tools_and_model_still_travel_alongside_a_trigger(knowledge_ctx, tmp_path):
    """The trigger branch must not shadow the other frontmatter fields."""
    src = _write(
        tmp_path,
        "id: x/agents/y\nkind: agent\ntrigger: when the thing happens\n"
        "tools: [Read, Grep]\nmodel: sonnet",
        "Body text.\n",
    )
    out = _render(knowledge_ctx, "agent", {"id": "x/agents/y", "title": "Y"}, src)
    assert "description: when the thing happens" in out
    assert "tools: Read, Grep" in out
    assert "model: sonnet" in out


def test_agents_and_skills_resolve_the_description_the_same_way(knowledge_ctx, tmp_path):
    """Two renderers reading one convention. If they diverge, whether a document
    keeps its hand-written trigger depends on which `kind:` it was filed under,
    which is not a distinction anyone authoring one would expect to matter."""
    fm = "id: p/agents/n\nkind: agent\ntrigger: when the condition holds"
    agent_src = _write(tmp_path, fm, "Body.\n")
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    skill_src = skill_dir / "agent.md"
    skill_src.write_text(f"---\n{fm.replace('agents', 'skills')}\n---\n\nBody.\n")

    row_a = {"id": "p/agents/n", "title": "N"}
    row_s = {"id": "p/skills/n", "title": "N"}
    a = _render(knowledge_ctx, "agent", row_a, agent_src)
    s = _render(knowledge_ctx, "skill", row_s, skill_src)
    assert "description: when the condition holds" in a
    assert "description: when the condition holds" in s
