"""Knowledge ingestion (website/PDF/text), source listing, and the
knowledge-gap draft/publish flow (Phase 9 extraction from app.py).
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, File, Header, HTTPException, UploadFile

from business_ai.ingestion import IngestionError, extract_pdf_text, fetch_website_text, ingest_text
from business_ai.schemas import GapPublishRequest, TextIngestRequest, WebsiteIngestRequest
from business_ai.security import UnsafeUrlError
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_knowledge(app: FastAPI, svc, ctx) -> None:
    # -------------------------------------------------------------- knowledge
    @app.post("/api/knowledge/website")
    def ingest_website(request: WebsiteIngestRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            text = fetch_website_text(request.url)
        except UnsafeUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        source_id = f"web_{secrets.token_hex(6)}"
        label = request.label or request.url
        try:
            result = ingest_text(
                text=text, tenant_id=tenant_id, source_id=source_id, source_label=label, source_url=request.url,
                embeddings=svc.embeddings(), store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(tenant_id=tenant_id, source_id=source_id, label=label, source_type="website", chunks_indexed=result.chunks_indexed)
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}

    @app.post("/api/knowledge/upload")
    async def upload_knowledge(
        tenant_id: str,
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        content = await file.read()
        from business_ai.security import MAX_UPLOAD_BYTES

        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="File exceeds the maximum allowed upload size.")

        filename = file.filename or "upload"
        try:
            if filename.lower().endswith(".pdf"):
                text = extract_pdf_text(content)
                source_type = "pdf"
            else:
                text = content.decode("utf-8", errors="replace")
                source_type = "text"
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        source_id = f"file_{secrets.token_hex(6)}"
        try:
            result = ingest_text(
                text=text, tenant_id=tenant_id, source_id=source_id, source_label=filename, source_url=None,
                embeddings=svc.embeddings(), store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(tenant_id=tenant_id, source_id=source_id, label=filename, source_type=source_type, chunks_indexed=result.chunks_indexed)
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}

    @app.post("/api/knowledge/text")
    def ingest_text_knowledge(
        request: TextIngestRequest,
        tenant_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        raw_text = request.text.strip()
        if not raw_text:
            raise HTTPException(status_code=400, detail="Text cannot be empty.")

        from business_ai.security import MAX_UPLOAD_BYTES

        if len(request.text.encode("utf-8")) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="Text exceeds the maximum allowed size.")

        raw_title = (request.title or "").strip()
        clean_title = " ".join(raw_title.split()) if raw_title else "Pasted Notes"
        if not clean_title:
            clean_title = "Pasted Notes"

        source_id = f"text_{secrets.token_hex(6)}"
        try:
            result = ingest_text(
                text=raw_text,
                tenant_id=tenant_id,
                source_id=source_id,
                source_label=clean_title,
                source_url=None,
                embeddings=svc.embeddings(),
                store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(
            tenant_id=tenant_id,
            source_id=source_id,
            label=clean_title,
            source_type="text",
            chunks_indexed=result.chunks_indexed,
        )
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}

    @app.get("/api/knowledge/sources")
    def list_sources(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"sources": [s.model_dump() for s in svc.source_store.list_for_tenant(tenant_id)]}

    def _load_gap_or_404(tenant_id: str, turn_id: str):
        gap = svc.analytics_store.get_gap(tenant_id, turn_id)
        if gap is None:
            raise HTTPException(status_code=404, detail=f"No open knowledge gap '{turn_id}' for this business.")
        return gap

    @app.post("/api/knowledge/gaps/{turn_id}/draft")
    def draft_gap_answer(turn_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        gap = _load_gap_or_404(tenant_id, turn_id)
        try:
            draft = svc.generator().draft_faq_answer(
                business_name=tenant.business_name, assistant_name=tenant.assistant_name, question=gap.query
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not generate a draft: {exc}") from exc
        return {"turn_id": turn_id, "question": gap.query, "draft_answer": draft}

    @app.post("/api/knowledge/gaps/{turn_id}/publish")
    def publish_gap_answer(
        turn_id: str, request: GapPublishRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        gap = _load_gap_or_404(tenant_id, turn_id)
        text = f"Q: {gap.query}\nA: {request.answer_text}"
        source_id = f"gap_{turn_id}"
        try:
            result = ingest_text(
                text=text, tenant_id=tenant_id, source_id=source_id, source_label=f"FAQ: {gap.query[:60]}",
                source_url=None, embeddings=svc.embeddings(), store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(
            tenant_id=tenant_id, source_id=source_id, label=f"FAQ: {gap.query[:60]}",
            source_type="faq", chunks_indexed=result.chunks_indexed,
        )
        svc.analytics_store.mark_gap_resolved(tenant_id, query=gap.query)
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}
