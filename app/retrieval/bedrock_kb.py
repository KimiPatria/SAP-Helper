"""Amazon Bedrock Knowledge Base retriever.

Alternate knowledge source behind the same retrieve() contract as the local
Retriever (app/retrieval/retriever.py): given a question, return top-k
RetrievedChunks plus deduplicated Citations. Swapping between the two is a
runtime choice (the chat UI toggle / KNOWLEDGE_BASE setting), not a code
change - the prompt assembly and generation layer downstream never know
which one answered.

Uses the bedrock-agent-runtime Retrieve API (not RetrieveAndGenerate) so
retrieved chunks flow through the same grounding prompt and citation builder
as the local knowledge base, regardless of which generation provider
(Bedrock or Groq) turns them into an answer.
"""

from __future__ import annotations

from app.retrieval.retriever import Citation, RetrievalError
from app.retrieval.store import RetrievedChunk

# Bedrock KB source locations, keyed by location "type", and the field each
# carries its uri/url under.
_LOCATION_URI_FIELD = {
    "S3": ("s3Location", "uri"),
    "WEB": ("webLocation", "url"),
    "CONFLUENCE": ("confluenceLocation", "url"),
    "SALESFORCE": ("salesforceLocation", "url"),
    "SHAREPOINT": ("sharePointLocation", "url"),
}


def _location_uri(location: dict) -> str:
    field = _LOCATION_URI_FIELD.get(location.get("type", ""))
    if not field:
        return ""
    key, uri_field = field
    return location.get(key, {}).get(uri_field, "")


class BedrockKBRetriever:
    name = "bedrock"

    def __init__(
        self,
        knowledge_base_id: str,
        aws_region: str,
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
    ):
        import boto3  # deferred: keep local-only deployments free of AWS bits
        import botocore.exceptions

        self.knowledge_base_id = knowledge_base_id
        self._botocore_exceptions = botocore.exceptions
        self._client = boto3.client(
            "bedrock-agent-runtime",
            region_name=aws_region,
            aws_access_key_id=aws_access_key_id or None,
            aws_secret_access_key=aws_secret_access_key or None,
        )

    def retrieve(self, question: str, top_k: int) -> tuple[list[RetrievedChunk], list[Citation]]:
        try:
            response = self._client.retrieve(
                knowledgeBaseId=self.knowledge_base_id,
                retrievalQuery={"text": question},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": top_k}
                },
            )
        except self._botocore_exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            raise RetrievalError(
                f"Bedrock Knowledge Base request failed ({error.get('Code', 'unknown')}): "
                f"{error.get('Message', exc)}"
            ) from exc
        except self._botocore_exceptions.BotoCoreError as exc:
            # Covers missing credentials, endpoint/connection failures, etc.
            raise RetrievalError(f"Bedrock Knowledge Base error: {exc}") from exc

        chunks = [self._to_chunk(result) for result in response.get("retrievalResults", [])]
        return chunks, self._citations(chunks)

    def _to_chunk(self, result: dict) -> RetrievedChunk:
        uri = _location_uri(result.get("location", {}))
        title = uri.rsplit("/", 1)[-1] if uri else "Bedrock Knowledge Base result"
        return RetrievedChunk(
            text=result.get("content", {}).get("text", ""),
            score=float(result.get("score", 0.0)),
            # No SAP Note numbering here - the source document name stands in
            # for note_id so citations/dedup work the same way as the local KB.
            note_id=title,
            title=title,
            section="",
            component="",
            source_file=uri,
        )

    def _citations(self, chunks: list[RetrievedChunk]) -> list[Citation]:
        by_source: dict[str, dict] = {}
        for chunk in chunks:
            if not chunk.note_id:
                continue
            entry = by_source.setdefault(
                chunk.note_id, {"title": chunk.title, "source_file": chunk.source_file, "score": chunk.score}
            )
            entry["score"] = max(entry["score"], chunk.score)
        citations = [
            Citation(
                note_id=note_id,
                title=e["title"],
                url=e["source_file"] or note_id,
                component="",
                source_file=e["source_file"],
                score=round(e["score"], 4),
                sections=(),
            )
            for note_id, e in by_source.items()
        ]
        return sorted(citations, key=lambda c: c.score, reverse=True)
