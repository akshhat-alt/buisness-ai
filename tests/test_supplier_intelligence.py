"""Tests for Phase 23's supplier intelligence — pure derived computation
over SupplierStore + PurchaseStore data, matching scorecard.py/
revenue_radar.py's own testing shape.
"""

from __future__ import annotations

from business_ai.purchases import PurchaseStore
from business_ai.supplier_intelligence import build_supplier_intelligence_report
from business_ai.suppliers import SupplierStore

TENANT = "trattoria-a"


def test_supplier_spend_aggregates_across_purchases(tmp_path):
    suppliers = SupplierStore(tmp_path / "suppliers.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    supplier = suppliers.create(tenant_id=TENANT, name="Ramesh Traders")
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4000, supplier_id=supplier.supplier_id)
    purchases.record(tenant_id=TENANT, ingredient_name="Butter", quantity=2, unit="kg", amount_inr=800, supplier_id=supplier.supplier_id)

    report = build_supplier_intelligence_report(TENANT, supplier_store=suppliers, purchase_store=purchases)
    assert len(report.suppliers) == 1
    s = report.suppliers[0]
    assert s.name == "Ramesh Traders"
    assert s.total_spend_inr == 4800
    assert s.purchase_count == 2
    assert s.distinct_ingredients == 2


def test_purchases_with_no_supplier_are_unattributed(tmp_path):
    suppliers = SupplierStore(tmp_path / "suppliers.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    purchases.record(tenant_id=TENANT, ingredient_name="Rice", quantity=5, unit="kg", amount_inr=500, supplier_id=None)

    report = build_supplier_intelligence_report(TENANT, supplier_store=suppliers, purchase_store=purchases)
    assert report.suppliers == []
    assert report.unattributed_spend_inr == 500
    assert report.unattributed_purchase_count == 1


def test_purchases_with_a_deleted_suppliers_id_are_unattributed(tmp_path):
    """A supplier_id that no longer resolves to a real Supplier row
    (deleted supplier) must fall back to unattributed, not crash."""
    suppliers = SupplierStore(tmp_path / "suppliers.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    purchases.record(tenant_id=TENANT, ingredient_name="Rice", quantity=5, unit="kg", amount_inr=500, supplier_id="supplier_ghost")

    report = build_supplier_intelligence_report(TENANT, supplier_store=suppliers, purchase_store=purchases)
    assert report.suppliers == []
    assert report.unattributed_spend_inr == 500


def test_suppliers_sorted_by_total_spend_descending(tmp_path):
    suppliers = SupplierStore(tmp_path / "suppliers.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    small = suppliers.create(tenant_id=TENANT, name="Small Supplier")
    big = suppliers.create(tenant_id=TENANT, name="Big Supplier")
    purchases.record(tenant_id=TENANT, ingredient_name="X", quantity=1, unit="kg", amount_inr=100, supplier_id=small.supplier_id)
    purchases.record(tenant_id=TENANT, ingredient_name="Y", quantity=1, unit="kg", amount_inr=5000, supplier_id=big.supplier_id)

    report = build_supplier_intelligence_report(TENANT, supplier_store=suppliers, purchase_store=purchases)
    assert [s.name for s in report.suppliers] == ["Big Supplier", "Small Supplier"]


def test_report_is_empty_for_a_tenant_with_no_purchases(tmp_path):
    suppliers = SupplierStore(tmp_path / "suppliers.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    report = build_supplier_intelligence_report(TENANT, supplier_store=suppliers, purchase_store=purchases)
    assert report.suppliers == []
    assert report.unattributed_spend_inr == 0
    assert report.unattributed_purchase_count == 0
