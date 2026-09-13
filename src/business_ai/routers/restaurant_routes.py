"""Restaurant Foundation (Phase 17) setup routes: menu items, recipes,
suppliers, and inventory. Deliberately API/dashboard-driven, not a
WhatsApp grammar — entering a whole menu with per-dish recipes is a
one-time or occasional bulk task, not the quick, repeated, on-the-floor
action WhatsApp commands are for (that's `log sale/purchase/waste`,
built in admin_bot.py). Menu/recipe/supplier changes are owner+manager
config, gated by the new MANAGE_MENU action; viewing inventory is gated
by VIEW_INVENTORY — both the same sensitivity tier as VIEW_FINANCIALS.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.inventory import InventoryStore
from business_ai.menu import MenuStore
from business_ai.schemas import (
    CreateMenuItemRequest,
    CreateSupplierRequest,
    SetInventoryParLevelRequest,
    SetRecipeRequest,
    UpdateMenuItemRequest,
)
from business_ai.supplier_intelligence import build_supplier_intelligence_report
from business_ai.suppliers import SupplierStore
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_restaurant(app: FastAPI, svc, ctx) -> None:
    # -------------------------------------------------------------- menu
    @app.get("/api/menu/items")
    def list_menu_items(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"items": [i.model_dump() for i in svc.menu_store.list_for_tenant(tenant_id)]}

    @app.post("/api/menu/items")
    def create_menu_item(request: CreateMenuItemRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_MENU, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            item = svc.menu_store.create_item(
                tenant_id=tenant_id, name=request.name, price_inr=request.price_inr, category=request.category or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="menu_item_created",
            target_type="menu_item", target_id=item.menu_item_id, metadata={"name": item.name},
        )
        return item.model_dump()

    @app.patch("/api/menu/items/{menu_item_id}")
    def update_menu_item(
        menu_item_id: str, request: UpdateMenuItemRequest, tenant_id: str, authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_MENU, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        fields = {k: v for k, v in request.model_dump().items() if v is not None}
        if not fields:
            existing = svc.menu_store.get_item(tenant_id, menu_item_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Unknown menu_item_id for this business.")
            return existing.model_dump()
        try:
            updated = svc.menu_store.update_item(tenant_id, menu_item_id, **fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown menu_item_id for this business.")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="menu_item_updated",
            target_type="menu_item", target_id=menu_item_id, metadata=fields,
        )
        return updated.model_dump()

    @app.post("/api/menu/items/{menu_item_id}/recipe")
    def set_menu_item_recipe(
        menu_item_id: str, request: SetRecipeRequest, tenant_id: str, authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_MENU, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if svc.menu_store.get_item(tenant_id, menu_item_id) is None:
            raise HTTPException(status_code=404, detail="Unknown menu_item_id for this business.")
        try:
            lines = svc.menu_store.set_recipe(tenant_id, menu_item_id, [line.model_dump() for line in request.lines])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="menu_item_recipe_set",
            target_type="menu_item", target_id=menu_item_id, metadata={"line_count": len(lines)},
        )
        return {"lines": [line.model_dump() for line in lines]}

    @app.get("/api/menu/items/{menu_item_id}/recipe")
    def get_menu_item_recipe(menu_item_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"lines": [line.model_dump() for line in svc.menu_store.get_recipe(tenant_id, menu_item_id)]}

    # -------------------------------------------------------------- suppliers
    @app.get("/api/suppliers")
    def list_suppliers(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"suppliers": [s.model_dump() for s in svc.supplier_store.list_for_tenant(tenant_id)]}

    @app.post("/api/suppliers")
    def create_supplier(request: CreateSupplierRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_MENU, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            supplier = svc.supplier_store.create(tenant_id=tenant_id, name=request.name, phone=request.phone, notes=request.notes or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="supplier_created",
            target_type="supplier", target_id=supplier.supplier_id, metadata={"name": supplier.name},
        )
        return supplier.model_dump()

    # -------------------------------------------------------------- inventory
    @app.get("/api/inventory")
    def list_inventory(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        items = svc.inventory_store.list_for_tenant(tenant_id)
        low_stock_keys = {i.ingredient_key for i in svc.inventory_store.list_low_stock(tenant_id)}
        return {
            "items": [{**i.model_dump(), "low_stock": i.ingredient_key in low_stock_keys} for i in items],
        }

    @app.post("/api/inventory/par-level")
    def set_inventory_par_level(
        request: SetInventoryParLevelRequest, tenant_id: str, authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_MENU, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        item = svc.inventory_store.set_par_level(
            tenant_id, request.ingredient_name, par_level=request.par_level, unit=request.unit,
        )
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="inventory_par_level_set",
            target_type="inventory_item", target_id=item.inventory_item_id,
            metadata={"ingredient_name": item.ingredient_name, "par_level": request.par_level},
        )
        return item.model_dump()

    # -------------------------------------------------------------- supplier intelligence (Phase 23)
    @app.get("/api/supplier-intelligence")
    def get_supplier_intelligence(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        report = build_supplier_intelligence_report(tenant_id, supplier_store=svc.supplier_store, purchase_store=svc.purchase_store)
        return report.model_dump()
