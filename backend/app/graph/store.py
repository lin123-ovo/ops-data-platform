"""Neo4j projection. All user values are bound parameters; labels are fixed."""
from __future__ import annotations

from functools import lru_cache
from app.config import get_settings


class GraphStore:
    def __init__(self) -> None:
        from neo4j import GraphDatabase
        settings = get_settings()
        self.timeout = settings.graph_query_timeout_seconds
        self._initialized = False
        self.driver = GraphDatabase.driver(settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=self.timeout, connection_acquisition_timeout=self.timeout,
            max_connection_pool_size=5, max_transaction_retry_time=0)

    def run(self, cypher: str, **params: object) -> list[dict]:
        from neo4j import Query
        with self.driver.session(database=get_settings().neo4j_database) as session:
            return session.run(Query(cypher, timeout=self.timeout), parameters=params).data()

    def initialize(self) -> None:
        if self._initialized:
            return
        for label in ('Entity', 'Assertion'):
            self.run(f'CREATE CONSTRAINT graph_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE')
        self.run('CREATE INDEX graph_entity_kb IF NOT EXISTS FOR (n:Entity) ON (n.knowledge_base)')
        self.run('CREATE INDEX graph_assertion_doc IF NOT EXISTS FOR (n:Assertion) ON (n.document_id)')
        self._initialized = True

    def write(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.run('''UNWIND $rows AS r
        MERGE (s:Entity {id:r.subject_id})
        SET s.name=r.subject.name, s.type=r.subject.type, s.qualifier=r.subject.qualifier,
            s.knowledge_base=r.knowledge_base, s.aliases=reduce(acc=coalesce(s.aliases,[]), alias IN r.subject.aliases | CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
        MERGE (o:Entity {id:r.object_id})
        SET o.name=r.object.name, o.type=r.object.type, o.qualifier=r.object.qualifier,
            o.knowledge_base=r.knowledge_base, o.aliases=reduce(acc=coalesce(o.aliases,[]), alias IN r.object.aliases | CASE WHEN alias IN acc THEN acc ELSE acc + alias END)
        MERGE (a:Assertion {id:r.id})
        SET a += {predicate:r.predicate, evidence:r.evidence, chunk_id:r.chunk_id,
          document_id:r.document_id, knowledge_base:r.knowledge_base, version:r.version,
          token:r.token, content_hash:r.content_hash, schema_version:r.schema_version, subject_entity_id:r.subject_id, object_entity_id:r.object_id}
        MERGE (a)-[:SUBJECT]->(s) MERGE (a)-[:OBJECT]->(o)''', rows=rows)

    def cleanup(self, document_id: str, keep_token: str = '') -> None:
        # Bounded transactions prevent large document cleanup from holding unbounded locks.
        while self.run('''MATCH (a:Assertion {document_id:$doc}) WHERE a.token <> $keep
            WITH a LIMIT 500 DETACH DELETE a RETURN count(*) AS count''', doc=document_id, keep=keep_token)[0]['count']:
            pass

    def cleanup_obsolete(self, document_id: str, protected_tokens: list[str]) -> None:
        tokens = self.run('MATCH (a:Assertion {document_id:$doc}) RETURN DISTINCT a.token AS token', doc=document_id)
        for row in tokens:
            if row['token'] in protected_tokens:
                continue
            while self.run('MATCH (a:Assertion {document_id:$doc, token:$token}) WITH a LIMIT 500 '
                          'DETACH DELETE a RETURN count(*) AS count', doc=document_id, token=row['token'])[0]['count']:
                pass

    def paths(self, *, query: str, chunk_ids: list[str], knowledge_base: str,
              document_id: str | None = None, entity: str | None = None) -> list[dict]:
        return self.run('''MATCH (seed:Entity {knowledge_base:$kb})
          WHERE ($entity IS NOT NULL AND (seed.name=$entity OR $entity IN seed.aliases)) OR
            ($entity IS NULL AND (
              (size($query)>0 AND (toLower($query) CONTAINS toLower(seed.name) OR
                any(alias IN seed.aliases WHERE toLower($query) CONTAINS toLower(alias)))) OR
              EXISTS { MATCH (a:Assertion)-[:SUBJECT|OBJECT]->(seed) WHERE a.chunk_id IN $chunks }))
          WITH seed ORDER BY
            CASE WHEN toLower($query) CONTAINS toLower(seed.name) THEN 0 ELSE 1 END, seed.id LIMIT 5
          MATCH p=(seed)-[:SUBJECT|OBJECT*2..4]-(target:Entity)
          WHERE target.knowledge_base=$kb AND
            all(n IN nodes(p) WHERE n.knowledge_base=$kb) AND
            all(n IN nodes(p) WHERE NOT n:Assertion OR $doc IS NULL OR n.document_id=$doc)
          WITH nodes(p) AS ns, length(p) AS distance
          RETURN [n IN ns WHERE n:Entity | properties(n)] AS entities,
                 [n IN ns WHERE n:Assertion | properties(n)] AS assertions
          ORDER BY distance LIMIT 300''', kb=knowledge_base, query=query, chunks=chunk_ids,
          doc=document_id, entity=entity)

    def close(self) -> None:
        self.driver.close()


@lru_cache(maxsize=1)
def get_graph_store() -> GraphStore:
    return GraphStore()
