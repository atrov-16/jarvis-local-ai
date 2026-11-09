"""Regression tests: long-term memory search must match natural-language queries.

It used to wrap the whole query in quotes, making it an FTS5 phrase query, so a request like
"how do I deploy the api" matched nothing unless that exact phrase was stored.
"""

import pytest

from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.repositories import build_fts_query
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
async def uow(tmp_path):
    db_path = tmp_path / "test.sqlite"
    async with sqlite_connection(db_path) as conn:
        await run_migrations(conn)
    return UnitOfWork(db_path)


async def _seed(unit) -> None:
    memory = unit.repositories.memory
    await memory.insert_long_term(
        memory_type="fact",
        title="Deploying the API",
        content="Deploy the FastAPI service with uv run jarvis daemon behind loopback.",
        source="manual",
    )
    await memory.insert_long_term(
        memory_type="preference",
        title="Editor",
        content="The user prefers VS Code with the Ruff extension.",
        source="manual",
    )
    await memory.insert_long_term(
        memory_type="note",
        title="Groceries",
        content="Buy milk, eggs and bread.",
        source="manual",
    )


async def test_natural_language_query_matches_without_exact_phrase(uow):
    async with uow.begin() as unit:
        await _seed(unit)
        results = await unit.repositories.memory.search_long_term("how do I deploy the api?")
    assert [r["title"] for r in results][:1] == ["Deploying the API"]


async def test_any_word_matches_and_more_matches_rank_first(uow):
    async with uow.begin() as unit:
        await _seed(unit)
        results = await unit.repositories.memory.search_long_term("ruff deploy loopback")
    titles = [r["title"] for r in results]
    assert set(titles) == {"Deploying the API", "Editor"}
    # "deploy" and "loopback" both hit the deploy note; only "ruff" hits the editor note.
    assert titles[0] == "Deploying the API"


async def test_word_order_and_case_do_not_matter(uow):
    async with uow.begin() as unit:
        await _seed(unit)
        results = await unit.repositories.memory.search_long_term("BREAD Milk")
    assert [r["title"] for r in results] == ["Groceries"]


async def test_fts_operators_in_input_are_treated_as_text(uow):
    async with uow.begin() as unit:
        await _seed(unit)
        # Unquoted, these would be FTS5 syntax (NOT, NEAR, column filter, parentheses).
        results = await unit.repositories.memory.search_long_term(
            'milk NOT bread) NEAR( title:"eggs'
        )
    assert [r["title"] for r in results] == ["Groceries"]


async def test_query_without_words_returns_nothing(uow):
    async with uow.begin() as unit:
        await _seed(unit)
        assert await unit.repositories.memory.search_long_term("?!  --") == []
        assert await unit.repositories.memory.search_long_term("") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("FastAPI", '"fastapi"'),
        ("deploy the API", '"deploy" OR "api"'),
        ("api* deploy", '"api"* OR "deploy"'),
        ("milk milk MILK", '"milk"'),
        # Only stopwords: keep them rather than search for nothing.
        ("what is it", '"what" OR "is" OR "it"'),
        ('say "hi" -- ok', '"say" OR "hi" OR "ok"'),
        ("", None),
        ("!!!", None),
    ],
)
def test_build_fts_query(query, expected):
    assert build_fts_query(query) == expected


def test_build_fts_query_caps_term_count():
    query = " ".join(f"word{i}" for i in range(100))
    built = build_fts_query(query)
    assert built is not None
    assert built.count(" OR ") == 31
