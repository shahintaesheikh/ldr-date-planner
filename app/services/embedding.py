"""Embedding generation service for the catalog.

Generates vector embeddings for date_activity text using OpenAI's embedding API.
The catalog uses 1536-dimensional embeddings (text-embedding-3-small) for
semantic (RAG) search via pgvector cosine similarity.
"""

from typing import Sequence

from openai import AsyncOpenAI

from app import settings


class EmbeddingService:
    """Generates embeddings for catalog text using OpenAI's API."""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._model_version = settings.embedding_model_version

    @property
    def model_version(self) -> str:
        """Return the embedding model version string for storage."""
        return self._model_version

    @property
    def dimensions(self) -> int:
        """Return the embedding dimension count."""
        return self._dimensions

    async def _get_client(self) -> AsyncOpenAI:
        """Lazy-initialise the OpenAI client."""
        if self._client is None:
            if not settings.openai_api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Cannot generate embeddings."
                )
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    def _build_input_text(self, name: str, description: str | None, tags: list[str]) -> str:
        """Build the text to embed from name, description, and tags.

        The embedding is computed over the concatenation of these fields so
        that the vector captures the full semantic content of the activity.
        """
        parts = [name]
        if description:
            parts.append(description)
        if tags:
            parts.append("Tags: " + ", ".join(tags))
        return "\n\n".join(parts)

    async def embed_text(self, text: str) -> list[float]:
        """Generate an embedding vector for a single text string.

        Args:
            text: The text to embed.

        Returns:
            A list of floats representing the embedding vector.

        Raises:
            RuntimeError: If OPENAI_API_KEY is not configured.
            openai.APIError: If the API call fails.
        """
        client = await self._get_client()
        response = await client.embeddings.create(
            model=self._model,
            input=text,
            dimensions=self._dimensions,
        )
        return response.data[0].embedding

    async def embed_activity(
        self, name: str, description: str | None, tags: list[str]
    ) -> list[float]:
        """Generate an embedding vector for a date activity.

        Combines name, description, and tags into a single input text
        before embedding.

        Args:
            name: Activity name.
            description: Optional activity description.
            tags: List of tag strings.

        Returns:
            A list of floats representing the embedding vector.
        """
        input_text = self._build_input_text(name, description, tags)
        return await self.embed_text(input_text)

    async def embed_many(
        self, texts: Sequence[str]
    ) -> list[list[float]]:
        """Generate embedding vectors for multiple texts in a single batch call.

        Args:
            texts: Sequence of text strings to embed.

        Returns:
            A list of embedding vectors, one per input text.
        """
        client = await self._get_client()
        response = await client.embeddings.create(
            model=self._model,
            input=list(texts),
            dimensions=self._dimensions,
        )
        # Sort by index to preserve input order
        sorted_data = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]


# Module-level singleton for convenience
embedding_service = EmbeddingService()