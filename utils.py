from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, List
from types import SimpleNamespace

from sqlalchemy import func
from models import db, Client, Product, Variant, Movement, Inventory, ReorderRule

# --- Constantes ---
DEFAULT_DEPOSIT = 30.0
MOV_TYPES = {"OUT", "IN", "DEFECT", "FULL"}  # FULL = retour plein


# --- Structures utilitaires ---
@dataclass
class Equipment:
    tireuse: int = 0
    co2: int = 0
    comptoir: int = 0
    tonnelle: int = 0


@dataclass
class Card:
    id: int
    name: str
    kegs: int
    beer_eur: float
    deposit_eur: float
    equipment: Equipment
    last_out: Optional[object] = None
    last_in: Optional[object] = None


def now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# --- Notes -> matériel prêté/repris ---
def parse_equipment(notes: Optional[str]) -> Equipment:
    if not notes:
        return Equipment()
    eq = Equipment()
    try:
        parts = [p.strip() for p in notes.split(";") if p.strip()]
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                k = k.strip().lower()
                try:
                    val = int(v.strip())
                except Exception:
                    val = 0
                if k == "tireuse":
                    eq.tireuse = val
                elif k == "co2":
                    eq.co2 = val
                elif k == "comptoir":
                    eq.comptoir = val
                elif k == "tonnelle":
                    eq.tonnelle = val
    except Exception:
        pass
    return eq


def combine_equipment(dst: Equipment, src: Equipment, sign: int):
    dst.tireuse += sign * (src.tireuse or 0)
    dst.co2 += sign * (src.co2 or 0)
    dst.comptoir += sign * (src.comptoir or 0)
    dst.tonnelle += sign * (src.tonnelle or 0)


# --- Prix / Consigne effectifs ---
def effective_price(m: Movement, v: Variant) -> Optional[float]:
    return m.unit_price_ttc if m.unit_price_ttc is not None else v.price_ttc


def effective_deposit(m: Movement) -> float:
    return m.deposit_per_keg if m.deposit_per_keg is not None else DEFAULT_DEPOSIT


# --- Inventaire (bar) ---
def get_or_create_inventory(variant_id: int) -> Inventory:
    inv = Inventory.query.filter_by(variant_id=variant_id).first()
    if not inv:
        inv = Inventory(variant_id=variant_id, qty=0, min_qty=0)
        db.session.add(inv)
        db.session.flush()
    return inv


def apply_inventory_effect(mtype: str, variant_id: int, qty: int):
    if mtype not in MOV_TYPES:
        return
    if mtype == "OUT":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) - (qty or 0)
    elif mtype == "IN":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) + (qty or 0)


def apply_inventory_effect_reverse(mtype: str, variant_id: int, qty: int):
    if mtype not in MOV_TYPES:
        return
    if mtype == "OUT":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) + (qty or 0)
    elif mtype == "IN":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) - (qty or 0)


# --- Masquer écocups/gobelets ---
def is_ecocup_product(product: Product) -> bool:
    n = (product.name or "").lower()
    return ("ecocup" in n) or ("gobelet" in n) or ("eco cup" in n) or ("eco-cup" in n)


def get_stock_items():
    variants = (
        db.session.query(Variant)
        .join(Product, Variant.product_id == Product.id)
        .filter(~Product.name.ilike("%ecocup%"), ~Product.name.ilike("%gobelet%"))
        .order_by(Product.name, Variant.size_l)
        .all()
    )
    rules_by_vid = {r.variant_id: r for r in ReorderRule.query.all()}
    rows = []
    for v in variants:
        inv = get_or_create_inventory(v.id)
        rr = rules_by_vid.get(v.id)
        rows.append(dict(
            variant=v,
            qty=inv.qty or 0,
            min_qty=(rr.min_qty if rr else 0) or 0,
        ))
    return rows


def compute_reorder_alerts():
    alerts = []
    for row in get_stock_items():
        v = row["variant"]
        qty = row["qty"]
        min_qty = row["min_qty"]
        if min_qty and qty < min_qty:
            alerts.append(SimpleNamespace(
                product=v.product,
                variant=v,
                qty=qty,
                min_qty=min_qty,
                need=max(0, min_qty - qty),
            ))
    return alerts


# --- Solde de fûts chez le client, par variante ---
def get_open_kegs_by_variant(client_id: int) -> Dict[int, int]:
    """
    Retourne un dict {variant_id: kegs_chez_client}
    kegs_chez_client = OUT - (IN + DEFECT + FULL)
    """
    rows = (
        db.session.query(Movement.variant_id, Movement.type, func.coalesce(func.sum(Movement.qty), 0))
        .filter(Movement.client_id == client_id)
        .group_by(Movement.variant_id, Movement.type)
        .all()
    )
    acc: Dict[int, Dict[str, int]] = {}
    for vid, mtype, s in rows:
        acc.setdefault(vid, {"OUT": 0, "IN": 0, "DEFECT": 0, "FULL": 0})
        acc[vid][mtype] = int(s or 0)
    open_map: Dict[int, int] = {}
    for vid, d in acc.items():
        open_map[vid] = int(d.get("OUT", 0) - (d.get("IN", 0) + d.get("DEFECT", 0) + d.get("FULL", 0)))
        if open_map[vid] < 0:
            open_map[vid] = 0
    return open_map


# --- Accueil (cartes) ---
def summarize_client_for_index(c: Client) -> Card:
    sums = dict(
        db.session.query(Movement.type, func.coalesce(func.sum(Movement.qty), 0))
        .filter(Movement.client_id == c.id)
        .group_by(Movement.type)
        .all()
    )
    total_out = int(sums.get("OUT", 0))
    total_in = int(sums.get("IN", 0))
    total_def = int(sums.get("DEFECT", 0))
    total_full = int(sums.get("FULL", 0))
    kegs = total_out - (total_in + total_def + total_full)

    beer_eur = 0.0
    deposit_eur = 0.0
    equipment = Equipment()
    last_out = None
    last_in = None

    for m, v in db.session.query(Movement, Variant)\
                          .join(Variant, Movement.variant_id == Variant.id)\
                          .filter(Movement.client_id == c.id)\
                          .order_by(Movement.created_at.asc()).all():
        if m.type == "OUT":
            last_out = m.created_at
        elif m.type == "IN":
            last_in = m.created_at

        price = effective_price(m, v) or 0.0
        dep = effective_deposit(m)
        eq = parse_equipment(m.notes)

        if m.type == "OUT":
            beer_eur += (m.qty or 0) * price
            deposit_eur += (m.qty or 0) * dep
            combine_equipment(equipment, eq, +1)
        elif m.type in {"IN", "DEFECT", "FULL"}:
            deposit_eur -= (m.qty or 0) * dep
            combine_equipment(equipment, eq, -1)

    return Card(
        id=c.id,
        name=c.name,
        kegs=kegs,
        beer_eur=round(beer_eur, 2),
        deposit_eur=round(deposit_eur, 2),
        equipment=equipment,
        last_out=last_out,
        last_in=last_in,
    )


def summarize_totals(cards: List[Card]) -> Dict[str, float]:
    return dict(
        kegs=sum(c.kegs for c in cards),
        beer_eur=round(sum(c.beer_eur for c in cards), 2),
        deposit_eur=round(sum(c.deposit_eur for c in cards), 2),
        tireuse=sum(c.equipment.tireuse for c in cards),
        co2=sum(c.equipment.co2 for c in cards),
        comptoir=sum(c.equipment.comptoir for c in cards),
        tonnelle=sum(c.equipment.tonnelle for c in cards),
    )


# --- Détail client ---
def client_movements_full(client_id: int):
    return db.session.query(Movement, Variant, Product)\
        .join(Variant, Movement.variant_id == Variant.id)\
        .join(Product, Variant.product_id == Product.id)\
        .filter(Movement.client_id == client_id)\
        .order_by(Movement.created_at.desc(), Movement.id.desc())\
        .all()


def summarize_client_detail(c: Client) -> Dict:
    rows = []
    beer_eur = 0.0
    deposit_eur = 0.0
    equipment = Equipment()
    liters_out_cum = 0.0

    for m, v, p in client_movements_full(c.id):
        price = effective_price(m, v) or 0.0
        dep = effective_deposit(m)
        eq = parse_equipment(m.notes)

        if m.type == "OUT":
            beer_eur += (m.qty or 0) * price
            deposit_eur += (m.qty or 0) * dep
            liters_out_cum += (m.qty or 0) * (v.size_l or 0)
            combine_equipment(equipment, eq, +1)
        elif m.type in {"IN", "DEFECT", "FULL"}:
            deposit_eur -= (m.qty or 0) * dep
            combine_equipment(equipment, eq, -1)

        rows.append(dict(
            id=m.id,
            date=m.created_at,
            type=m.type,
            product=p.name,
            size_l=v.size_l,
            qty=m.qty,
            unit_price_ttc=price,
            deposit_per_keg=dep,
            notes=m.notes,
        ))

    sums = dict(
        db.session.query(Movement.type, func.coalesce(func.sum(Movement.qty), 0))
        .filter(Movement.client_id == c.id)
        .group_by(Movement.type)
        .all()
    )
    total_out = int(sums.get("OUT", 0))
    total_in = int(sums.get("IN", 0))
    total_def = int(sums.get("DEFECT", 0))
    total_full = int(sums.get("FULL", 0))
    kegs = total_out - (total_in + total_def + total_full)

    return dict(
        rows=rows,
        kegs=kegs,
        beer_eur=round(beer_eur, 2),
        deposit_eur=round(deposit_eur, 2),
        equipment=equipment,
        liters_out_cum=round(liters_out_cum, 2),
    )
