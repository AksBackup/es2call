"""
RAG Service — ChromaDB vector store for company knowledge base.
Each company gets its own collection.
"""
import logging
import os
from typing import List

# Startup-noise fixes (set BEFORE chromadb / sentence-transformers import):
#  - Transformers tries to load TensorFlow when it is installed; Keras 3 is not
#    supported by it ("pip install tf-keras" error). Embeddings only need
#    PyTorch, so tell Transformers to ignore TensorFlow entirely.
#  - Silence TF's oneDNN banner.
#  - Chroma's telemetry client is incompatible with newer `posthog` versions
#    ("capture() takes 1 positional argument but 3 were given") and is useless
#    here — switch it off.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

logger = logging.getLogger(__name__)


class RAGService:
    def __init__(self):
        self._client = None
        self._embedding_fn = None
        # company_id -> chromadb Collection. Search is on the hot per-turn
        # path during a live call, and was paying for list_collections() +
        # get_collection() every single turn even though the collection
        # object for a given company doesn't change turn-to-turn within a
        # call. Cache it and skip straight to .query() on repeat hits.
        self._collection_cache = {}

    def _get_client(self):
        if self._client is None:
            from app.core.config import settings
            import chromadb

            path = getattr(settings, "CHROMADB_LOCAL_PATH", "./chroma_data")
            host = getattr(settings, "CHROMADB_HOST", "localhost")
            port = getattr(settings, "CHROMADB_PORT", 8001)

            if host in ("localhost", "127.0.0.1"):
                self._client = chromadb.PersistentClient(
                    path=path,
                    settings=chromadb.config.Settings(anonymized_telemetry=False),
                )
                logger.info(f"ChromaDB using local persistent store at {path}")
            else:
                self._client = chromadb.HttpClient(
                    host=host, port=port,
                    settings=chromadb.config.Settings(anonymized_telemetry=False),
                )
                logger.info(f"ChromaDB connected to {host}:{port}")

        return self._client

    def _get_embedding_fn(self):
        if self._embedding_fn is None:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            from app.core.config import settings
            model = getattr(settings, "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
            self._embedding_fn = SentenceTransformerEmbeddingFunction(model_name=model)
        return self._embedding_fn

    def warmup(self):
        """
        Initialize ChromaDB client and embedding model at startup.
        Without this, the first search during a live call triggers a 2-3s
        cold-start delay (loading sentence-transformers model + opening DB).
        Call this once from app startup before any calls come in.
        """
        try:
            _ = self._get_client()
            _ = self._get_embedding_fn()
            logger.info("RAG service warmed up — ChromaDB and embeddings ready")
        except Exception as e:
            logger.warning(f"RAG warmup error (non-fatal): {e}")

    def _collection_name(self, company_id: str) -> str:
        return f"company_{company_id.replace('-', '_')}"

    def _get_cached_collection(self, company_id: str):
        """Returns the cached Collection handle for company_id, or None if
        it isn't cached yet / doesn't exist. Resolves + caches on miss."""
        if company_id in self._collection_cache:
            return self._collection_cache[company_id]

        client = self._get_client()
        col_name = self._collection_name(company_id)
        existing = [c.name for c in client.list_collections()]
        if col_name not in existing:
            return None

        collection = client.get_collection(
            name=col_name,
            embedding_function=self._get_embedding_fn(),
        )
        self._collection_cache[company_id] = collection
        return collection

    def _invalidate_collection_cache(self, company_id: str):
        self._collection_cache.pop(company_id, None)

    async def ingest_text(self, company_id: str, doc_id: str, text: str, metadata: dict = None) -> int:
        """Chunk and embed text into ChromaDB. Returns number of chunks ingested."""
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, self._sync_ingest, company_id, doc_id, text, metadata or {}
        )

    def _sync_ingest(self, company_id: str, doc_id: str, text: str, metadata: dict) -> int:
        try:
            client = self._get_client()
            collection = client.get_or_create_collection(
                name=self._collection_name(company_id),
                embedding_function=self._get_embedding_fn(),
            )

            chunks = self._chunk_text(text, chunk_size=500, overlap=50)
            if not chunks:
                return 0

            ids   = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
            metas = [{**metadata, "doc_id": doc_id, "chunk": i} for i in range(len(chunks))]

            collection.upsert(documents=chunks, ids=ids, metadatas=metas)
            # A collection may not have existed (or been cached) before this
            # ingest — drop any stale/absent cache entry so the next search
            # picks up the collection we just created/updated.
            self._invalidate_collection_cache(company_id)
            logger.info(f"Ingested {len(chunks)} chunks | company={company_id} | doc={doc_id}")
            return len(chunks)
        except Exception as e:
            logger.error(f"RAG ingest error: {e}")
            return 0

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
        text = text.strip()
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            start += chunk_size - overlap
        return chunks

    async def search(self, company_id: str, query: str, n_results: int = 3) -> str:
        """Search KB for relevant context. Returns formatted string, empty string if nothing found."""
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, self._sync_search, company_id, query, n_results
        )

    def _sync_search(self, company_id: str, query: str, n_results: int) -> str:
        try:
            collection = self._get_cached_collection(company_id)
            if collection is None:
                logger.debug(f"RAG: collection not found | company={company_id[:8]}")
                return ""

            count = collection.count()
            if count == 0:
                logger.debug(f"RAG: collection for company={company_id[:8]} is empty")
                return ""

            results = collection.query(
                query_texts=[query],
                n_results=min(n_results, count),
            )
            docs = results.get("documents", [[]])[0]
            if docs:
                logger.info(f"RAG hit: {len(docs)} chunks | query='{query[:50]}' | company={company_id[:8]}")
            else:
                logger.debug(f"RAG: no results | query='{query[:50]}' | chunks_available={count}")
            return "\n\n".join(docs) if docs else ""

        except Exception as e:
            logger.warning(f"RAG search error: {e}")
            return ""

    def debug_collection(self, company_id: str) -> dict:
        """
        Inspect what's stored in ChromaDB for a company.
        Call from a debug endpoint or startup to verify PDF was ingested.
        """
        try:
            client = self._get_client()
            col_name = self._collection_name(company_id)
            existing = [c.name for c in client.list_collections()]
            if col_name not in existing:
                return {"status": "collection_not_found", "available": existing}
            col = client.get_collection(
                name=col_name,
                embedding_function=self._get_embedding_fn(),
            )
            count = col.count()
            sample = col.get(limit=2)
            return {
                "status": "ok",
                "collection": col_name,
                "total_chunks": count,
                "sample": sample["documents"][:2] if sample["documents"] else [],
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def delete_document(self, company_id: str, doc_id: str):
        """Remove all chunks for a document."""
        import asyncio
        await asyncio.get_event_loop().run_in_executor(
            None, self._sync_delete, company_id, doc_id
        )

    def _sync_delete(self, company_id: str, doc_id: str):
        try:
            client = self._get_client()
            col_name = self._collection_name(company_id)
            existing = [c.name for c in client.list_collections()]
            if col_name not in existing:
                return
            collection = client.get_collection(
                name=col_name,
                embedding_function=self._get_embedding_fn(),
            )
            chunks = collection.get(where={"doc_id": doc_id})
            if chunks["ids"]:
                collection.delete(ids=chunks["ids"])
                self._invalidate_collection_cache(company_id)
                logger.info(f"Deleted {len(chunks['ids'])} chunks | doc={doc_id}")
        except Exception as e:
            logger.error(f"RAG delete error: {e}")

    async def list_documents(self, company_id: str) -> List[str]:
        """Return unique doc_ids ingested for a company."""
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, self._sync_list, company_id
        )

    def _sync_list(self, company_id: str) -> List[str]:
        try:
            client = self._get_client()
            col_name = self._collection_name(company_id)
            existing = [c.name for c in client.list_collections()]
            if col_name not in existing:
                return []
            collection = client.get_collection(
                name=col_name,
                embedding_function=self._get_embedding_fn(),
            )
            result = collection.get()
            doc_ids = list({m.get("doc_id") for m in result.get("metadatas", []) if m.get("doc_id")})
            return doc_ids
        except Exception as e:
            logger.error(f"RAG list error: {e}")
            return []


rag_service = RAGService()