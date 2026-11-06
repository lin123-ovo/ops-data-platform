from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.milvus_client import get_vector_store, normalize_vector_ids
from app.core.vector_indexing import (
    build_embedding_text, defer_vector_delete, vector_transaction, write_chunk_vectors,
)
from app.models.chunk import Chunk
from app.rag.bm25_index import get_bm25_index
from app.graph.jobs import enqueue
from app.utils.text import clean_text, estimate_token_count


class ChunkService:
    """Chunk 相关业务编排。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_chunks(self, *, document_id: str | None = None, limit: int = 100) -> list[Chunk]:
        """按文档筛选并返回 chunk 列表。"""

        statement = select(Chunk).order_by(Chunk.updated_at.desc()).limit(limit)
        if document_id:
            statement = statement.where(Chunk.document_id == document_id)
        return self.db.execute(statement).scalars().all()

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """按主键查询单个 chunk。"""

        statement = select(Chunk).where(Chunk.id == chunk_id)
        return self.db.execute(statement).scalar_one_or_none()

    @vector_transaction
    def update_chunk(
        self,
        chunk_id: str,
        *,
        content: str | None = None,
        enabled: bool | None = None,
        metadata_json: dict | None = None,
    ) -> Chunk | None:
        """Edit raw text or context; replace vectors only after successful encoding."""
        chunk = self.get_chunk(chunk_id)
        if chunk is None:
            return None
        old_content = chunk.content
        old_metadata = dict(chunk.metadata_json or {})
        new_content = clean_text(content) if content is not None else old_content
        if not new_content.strip():
            raise ValueError('Chunk content cannot be empty')
        metadata = {**old_metadata, **(metadata_json or {})}
        reembed = new_content != old_content or (
            build_embedding_text(new_content, metadata) != build_embedding_text(old_content, old_metadata)
        )
        if reembed:
            store = get_vector_store()
            metadata.update({
                'chunk_id': chunk.id, 'document_id': chunk.document_id, 'chunk_index': chunk.chunk_index,
                'start_offset': chunk.start_offset, 'end_offset': chunk.end_offset, 'manual_edited': True,
            })
            ids, encoded_metadata = write_chunk_vectors(self.db, store, texts=[new_content], metadatas=[metadata])
            if chunk.vector_id:
                defer_vector_delete(self.db, store, [chunk.vector_id])
            chunk.vector_id = str(ids[0])
            metadata = {**encoded_metadata[0], 'vector_id': chunk.vector_id}
            chunk.content = new_content
            chunk.token_count = estimate_token_count(new_content)
        if metadata_json or reembed:
            chunk.metadata_json = metadata
        if enabled is not None:
            chunk.enabled = enabled
        enqueue(self.db, chunk.document_id)
        self.db.commit()
        self.db.refresh(chunk)
        get_bm25_index().mark_dirty(f'chunk_updated:{chunk.id}')
        return chunk

    def delete_chunk(self, chunk_id: str) -> bool:
        """物理删除单个 chunk，包括从 Milvus 和数据库中删除。"""

        chunk = self.get_chunk(chunk_id)
        if chunk is None:
            return False

        if chunk.vector_id:
            vector_store = get_vector_store()
            try:
                vector_store.delete(ids=normalize_vector_ids([chunk.vector_id]))
            except Exception:  # noqa: BLE001
                # 即使向量删除失败，也继续删除数据库记录，防止产生脏数据死锁。
                pass

        self.db.delete(chunk)
        enqueue(self.db, chunk.document_id)
        self.db.commit()
        get_bm25_index().mark_dirty(f'chunk_deleted:{chunk.id}')
        return True
