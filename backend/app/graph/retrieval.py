"""Bounded graph paths validated against authoritative relational sources."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.graph.extraction import SCHEMA_VERSION, digest, normalize_name, direction_valid
from app.graph.store import get_graph_store
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.graph import GraphState


def valid_paths(db: Session, paths: list[dict], knowledge_base: str) -> list[dict]:
    assertions = [a for p in paths for a in p['assertions']]
    doc_ids = list({a['document_id'] for a in assertions})
    chunk_ids = list({a['chunk_id'] for a in assertions})
    if not chunk_ids:
        return []
    states = {s.document_id: s for s in db.scalars(select(GraphState).where(GraphState.document_id.in_(doc_ids)))}
    sources = {c.id: (c, d) for c, d in db.execute(select(Chunk, Document).join(Document).where(
        Chunk.id.in_(chunk_ids), Chunk.enabled.is_(True), Document.knowledge_base == knowledge_base))}
    output = []
    for path in paths:
        if not 1 <= len(path['assertions']) <= 2 or len(path['entities']) != len(path['assertions']) + 1:
            continue
        if len({e['id'] for e in path['entities']}) != len(path['entities']):
            continue
        valid = True
        entity_types = {e['id']: e.get('type', '') for e in path['entities']}
        for a in path['assertions']:
            if a.get('schema_version') != SCHEMA_VERSION or not direction_valid(a['predicate'],
                    entity_types.get(a.get('subject_entity_id'), ''), entity_types.get(a.get('object_entity_id'), '')):
                valid = False
                break
            state, source = states.get(a['document_id']), sources.get(a['chunk_id'])
            if (not state or not source or state.knowledge_base != knowledge_base
                or state.desired_version != a['version'] or state.published_version != a['version']
                or state.published_token != a['token'] or source[0].document_id != a['document_id']
                or digest(source[0].content) != a['content_hash'] or a['evidence'] not in source[0].content):
                valid = False
                break
        if valid:
            output.append(path)
    return output


def graph_paths(db: Session, *, query: str, chunk_ids: list[str], knowledge_base: str,
                document_id: str | None = None, entity: str | None = None) -> list[dict]:
    paths = get_graph_store().paths(query=query, chunk_ids=chunk_ids[:10], knowledge_base=knowledge_base,
                                   document_id=document_id, entity=entity)
    return valid_paths(db, paths, knowledge_base)


def graph_candidates(db: Session, *, query: str, chunk_ids: list[str], knowledge_base: str) -> list[dict]:
    paths = graph_paths(db, query=query, chunk_ids=chunk_ids, knowledge_base=knowledge_base)
    candidates: dict[str, dict] = {}
    for path in paths:
        matches = sum(normalize_name(e['name']) in normalize_name(query) or
                      any(normalize_name(a) in normalize_name(query) for a in e.get('aliases', []))
                      for e in path['entities'])
        for assertion in path['assertions']:
            item = candidates.setdefault(assertion['chunk_id'], {
                'chunk_id': assertion['chunk_id'], 'graph_paths': [], 'graph_entities': [],
                'graph_score': 0.0, 'retrieval_sources': ['graph'], 'retrieval_source': 'graph', 'score': 0.0,
            })
            item['graph_paths'].append(path)
            for entity in path['entities']:
                if entity not in item['graph_entities']:
                    item['graph_entities'].append(entity)
            item['graph_score'] = max(item['graph_score'], matches)
    ordered = sorted(candidates.values(), key=lambda x: (
        min(len(p['assertions']) for p in x['graph_paths']), -x['graph_score'],
        -len({a['id'] for p in x['graph_paths'] for a in p['assertions']}), x['chunk_id']))[:20]
    for i, item in enumerate(ordered, 1):
        item['rank_graph'] = i
        item['graph_paths'] = sorted(item['graph_paths'], key=lambda p: len(p['assertions']))[:5]
    return ordered
