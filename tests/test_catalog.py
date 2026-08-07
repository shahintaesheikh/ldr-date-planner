"""Tests for the catalog service, schemas, and seed data.

Uses a mock for the embedding service to avoid real API calls in tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.couple import DateActivity, ActivitySource
from app.schemas.catalog import (
    ActivitySource as ActivitySourceEnum,
    DateActivityCreate,
    DateActivityDedupResult,
    DateActivityRead,
    DateActivitySearchResult,
    DateActivitySearchQuery,
)
from app.services.catalog import CatalogService
from app.services.catalog_seed import SEED_ACTIVITIES
from app.services.embedding import EmbeddingService


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def catalog_service() -> CatalogService:
    """Return a fresh CatalogService instance for each test."""
    return CatalogService()


@pytest.fixture
def mock_embedding_service() -> None:
    """Patch the embedding_service singleton to return dummy vectors.

    The mock returns a 1536-dimensional vector of all 0.5s for any input.
    """
    dummy_vector = [0.5] * 1536

    with patch("app.services.catalog.embedding_service") as mock:
        mock.embed_text = AsyncMock(return_value=dummy_vector)
        mock.embed_activity = AsyncMock(return_value=dummy_vector)
        mock.embed_many = AsyncMock(return_value=[dummy_vector] * 30)
        mock.model_version = "test-model-v1"
        mock.dimensions = 1536
        mock._build_input_text = lambda name, description, tags: EmbeddingService._build_input_text(mock, name, description, tags)  # type: ignore[method-assign]
        yield


# ── Schema tests ──────────────────────────────────────────────────────────

class TestDateActivityCreate:
    def test_minimal(self) -> None:
        activity = DateActivityCreate(name="Test Activity")
        assert activity.name == "Test Activity"
        assert activity.description is None
        assert activity.est_duration_min == 60
        assert activity.cost_tag is None
        assert activity.source == ActivitySourceEnum.seed
        assert activity.tags == []

    def test_full(self) -> None:
        activity = DateActivityCreate(
            name="Cooking Class",
            description="Cook together online",
            est_duration_min=90,
            cost_tag="free",
            source=ActivitySourceEnum.user,
            tags=["cooking", "interactive"],
        )
        assert activity.name == "Cooking Class"
        assert activity.est_duration_min == 90
        assert activity.source == ActivitySourceEnum.user

    def test_name_required(self) -> None:
        with pytest.raises(ValueError, match="Field required"):
            DateActivityCreate()  # type: ignore[call-arg]

    def test_est_duration_min_bounds(self) -> None:
        with pytest.raises(ValueError, match="Input should be greater than or equal to 15"):
            DateActivityCreate(name="x", est_duration_min=10)


class TestDateActivityRead:
    def test_from_attributes(self) -> None:
        data = {
            "id": 1,
            "name": "Test",
            "description": "Desc",
            "est_duration_min": 60,
            "cost_tag": None,
            "source": "seed",
            "tags": ["a", "b"],
            "embedding_model_version": "v1",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
        read = DateActivityRead.model_validate(data)
        assert read.id == 1
        assert read.name == "Test"
        assert read.source == ActivitySourceEnum.seed


class TestDateActivitySearchResult:
    def test_basic(self) -> None:
        result = DateActivitySearchResult(
            id=1,
            name="Test",
            description="Desc",
            est_duration_min=60,
            cost_tag=None,
            source=ActivitySourceEnum.seed,
            tags=["a"],
            similarity=0.85,
        )
        assert result.similarity == 0.85
        assert result.source == ActivitySourceEnum.seed


class TestDateActivitySearchQuery:
    def test_minimal(self) -> None:
        q = DateActivitySearchQuery(query_text="cooking")
        assert q.query_text == "cooking"
        assert q.max_results == 10
        assert q.min_similarity is None
        assert q.duration_max_min is None

    def test_full(self) -> None:
        q = DateActivitySearchQuery(
            query_text="romantic dinner",
            max_results=5,
            min_similarity=0.8,
            duration_max_min=120,
        )
        assert q.max_results == 5
        assert q.min_similarity == 0.8
        assert q.duration_max_min == 120


class TestDateActivityDedupResult:
    def test_no_duplicate(self) -> None:
        r = DateActivityDedupResult(is_duplicate=False, threshold=0.92)
        assert not r.is_duplicate
        assert r.existing_id is None

    def test_duplicate_found(self) -> None:
        r = DateActivityDedupResult(
            is_duplicate=True,
            existing_id=5,
            existing_name="Movie Night",
            similarity=0.95,
            threshold=0.92,
        )
        assert r.is_duplicate
        assert r.existing_id == 5


# ── Seed data tests ───────────────────────────────────────────────────────

class TestSeedData:
    def test_has_expected_count(self) -> None:
        """There should be between 15 and 25 seed activities."""
        assert 15 <= len(SEED_ACTIVITIES) <= 25, (
            f"Expected 15-25 seed activities, got {len(SEED_ACTIVITIES)}"
        )

    def test_all_have_names(self) -> None:
        for a in SEED_ACTIVITIES:
            assert a.name, f"Seed activity missing name"

    def test_all_have_descriptions(self) -> None:
        for a in SEED_ACTIVITIES:
            assert a.description, f"Seed activity '{a.name}' missing description"

    def test_all_have_tags(self) -> None:
        for a in SEED_ACTIVITIES:
            assert len(a.tags) >= 2, f"Seed activity '{a.name}' has fewer than 2 tags"

    def test_all_source_is_seed(self) -> None:
        for a in SEED_ACTIVITIES:
            assert a.source == ActivitySourceEnum.seed, (
                f"Seed activity '{a.name}' has source={a.source}"
            )

    def test_est_duration_min_is_reasonable(self) -> None:
        for a in SEED_ACTIVITIES:
            assert 15 <= a.est_duration_min <= 240, (
                f"Seed activity '{a.name}' has unreasonable duration {a.est_duration_min}"
            )

    def test_covers_diverse_categories(self) -> None:
        """Verify seed data covers the expected categories."""
        all_tags = [tag for a in SEED_ACTIVITIES for tag in a.tags]
        expected_categories = {"co-watch", "cook-along", "game", "virtual-tour"}
        for cat in expected_categories:
            assert cat in all_tags, (
                f"Seed data missing category '{cat}'"
            )

    def test_names_are_unique(self) -> None:
        names = [a.name for a in SEED_ACTIVITIES]
        assert len(names) == len(set(names)), "Duplicate seed activity names found"


# ── CatalogService tests ─────────────────────────────────────────────────

class TestCatalogService:
    """Tests for CatalogService using a mock embedding service.

    These tests verify the orchestration logic (calling embed, building SQL,
    dedup checks) without needing a real database or OpenAI API.
    """

    @pytest.mark.asyncio
    async def test_create_calls_embed_and_adds_model_version(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """Creating an activity generates an embedding and sets model_version."""
        mock_db = AsyncMock(spec=AsyncSession)

        activity_in = DateActivityCreate(
            name="Test Activity",
            description="A test",
            tags=["test"],
        )

        result = await catalog_service.create(mock_db, activity_in)

        assert result.name == "Test Activity"
        assert result.embedding == [0.5] * 1536
        assert result.embedding_model_version == "test-model-v1"
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_duplicate_no_results(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """When the catalog is empty, check_duplicate returns is_duplicate=False."""
        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        mock_db.execute.return_value = mock_result

        result = await catalog_service.check_duplicate(
            mock_db, name="New", description="Desc", tags=["a"]
        )

        assert not result.is_duplicate
        assert result.existing_id is None

    @pytest.mark.asyncio
    async def test_check_duplicate_below_threshold(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """When similarity is below threshold, is_duplicate=False."""
        mock_db = AsyncMock(spec=AsyncSession)

        class MockRow:
            id = 1
            name = "Existing"
            similarity = 0.5  # Below 0.92 threshold

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MockRow()
        mock_db.execute.return_value = mock_result

        result = await catalog_service.check_duplicate(
            mock_db, name="New", description="Desc", tags=["a"]
        )

        assert not result.is_duplicate
        assert result.similarity == 0.5

    @pytest.mark.asyncio
    async def test_check_duplicate_above_threshold(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """When similarity exceeds threshold, is_duplicate=True."""
        mock_db = AsyncMock(spec=AsyncSession)

        class MockRow:
            id = 5
            name = "Movie Night"
            similarity = 0.95  # Above 0.92 threshold

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MockRow()
        mock_db.execute.return_value = mock_result

        result = await catalog_service.check_duplicate(
            mock_db, name="Movie Night Clone", description="Desc", tags=["a"]
        )

        assert result.is_duplicate
        assert result.existing_id == 5
        assert result.existing_name == "Movie Night"

    @pytest.mark.asyncio
    async def test_create_with_dedup_existing(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """create_with_dedup returns existing entry when duplicate detected."""
        mock_db = AsyncMock(spec=AsyncSession)

        class MockRow:
            id = 5
            name = "Movie Night"
            similarity = 0.95

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MockRow()
        mock_db.execute.return_value = mock_result

        # Simulate get_by_id returning the existing activity
        existing = DateActivity(
            id=5, name="Movie Night", description="Original",
            est_duration_min=120, source=ActivitySource.seed,
            tags=["movie"], embedding=[0.5] * 1536,
            embedding_model_version="v1",
        )
        catalog_service.get_by_id = AsyncMock(return_value=existing)  # type: ignore[method-assign]

        activity_in = DateActivityCreate(
            name="Movie Night Clone",
            description="Original",
            tags=["movie"],
        )

        result, was_created = await catalog_service.create_with_dedup(mock_db, activity_in)

        assert not was_created
        assert result.id == 5
        assert result.name == "Movie Night"

    @pytest.mark.asyncio
    async def test_create_with_dedup_new(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """create_with_dedup creates a new entry when no duplicate."""
        mock_db = AsyncMock(spec=AsyncSession)

        class MockRow:
            id = 1
            name = "Unrelated"
            similarity = 0.3

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MockRow()
        mock_db.execute.return_value = mock_result

        # Patch create to return a new DateActivity
        async def fake_create(db, activity_in):
            return DateActivity(
                id=99,
                name=activity_in.name,
                description=activity_in.description,
                est_duration_min=activity_in.est_duration_min,
                source=activity_in.source,
                tags=activity_in.tags,
                embedding=[0.5] * 1536,
                embedding_model_version="v1",
            )

        catalog_service.create = fake_create  # type: ignore[method-assign]

        activity_in = DateActivityCreate(
            name="Brand New Activity",
            description="Fresh",
            tags=["new"],
        )

        result, was_created = await catalog_service.create_with_dedup(mock_db, activity_in)

        assert was_created
        assert result.id == 99
        assert result.name == "Brand New Activity"

    @pytest.mark.asyncio
    async def test_search_semantic_calls_embed_and_executes_sql(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """search_semantic generates a query embedding and runs SQL."""
        mock_db = AsyncMock(spec=AsyncSession)

        class MockRow:
            def __init__(self, id, name, similarity):
                self.id = id
                self.name = name
                self.description = "Desc"
                self.est_duration_min = 60
                self.cost_tag = None
                self.source = "seed"
                self.tags = ["a"]
                self.similarity = similarity

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            MockRow(1, "Movie Night", 0.85),
            MockRow(2, "Cooking Class", 0.72),
        ]
        mock_db.execute.return_value = mock_result

        results = await catalog_service.search_semantic(
            mock_db, "romantic movie night",
            max_results=5,
            min_similarity=0.7,
            duration_max_min=120,
        )

        assert len(results) == 2
        assert results[0].name == "Movie Night"
        assert results[0].similarity == 0.85
        assert results[1].similarity == 0.72

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_found(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """delete returns True when the activity exists."""
        mock_db = AsyncMock(spec=AsyncSession)
        existing = DateActivity(
            id=1, name="Test", source=ActivitySource.seed,
            tags=[], embedding=[0.5] * 1536,
        )
        catalog_service.get_by_id = AsyncMock(return_value=existing)  # type: ignore[method-assign]

        result = await catalog_service.delete(mock_db, 1)
        assert result is True
        mock_db.delete.assert_awaited_once_with(existing)

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """delete returns False when the activity does not exist."""
        mock_db = AsyncMock(spec=AsyncSession)
        catalog_service.get_by_id = AsyncMock(return_value=None)  # type: ignore[method-assign]

        result = await catalog_service.delete(mock_db, 999)
        assert result is False
        mock_db.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_seed_if_empty_inserts_all(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """seed_if_empty inserts all seed activities when catalog is empty."""
        mock_db = AsyncMock(spec=AsyncSession)
        catalog_service.count = AsyncMock(return_value=0)  # type: ignore[method-assign]

        inserted = await catalog_service.seed_if_empty(mock_db)

        assert inserted == len(SEED_ACTIVITIES)
        mock_db.add_all.assert_called_once()
        mock_db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_seed_if_empty_skips_when_not_empty(
        self, catalog_service: CatalogService, mock_embedding_service: Any
    ) -> None:
        """seed_if_empty returns 0 when catalog already has entries."""
        mock_db = AsyncMock(spec=AsyncSession)
        catalog_service.count = AsyncMock(return_value=5)  # type: ignore[method-assign]

        inserted = await catalog_service.seed_if_empty(mock_db)

        assert inserted == 0
        mock_db.add_all.assert_not_called()


# Helper import for type annotation
from typing import Any