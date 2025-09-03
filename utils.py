# utils.py — helpers & règles métier
from dataclasses import dataclass
from datetime import datetime, date, time
from typing import Optional, Dict, List, Tuple

from sqlalchemy import func
from models import db, Client, Product, Variant, Movement, Inventory, ReorderRule

# --- Constantes partagées ---
DEFAULT_DEPOSIT = 30.0
MOV_TYPES = {"OUT", "IN", "DEFECT", "FULL"}  # FULL = retour plein non percuté


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
    last_out: Optional[datetime]
    last_in: Optional[datetime]
    equipment: Equipment


# --- Fonctions génériques ---
def now_utc() -> datetime:
    return datetime.utcnow()


def parse_equipment(notes: Optional[str]) -> Equipment:
    """
    Extrait le matériel depuis notes, ex :
    'tireuse=1;co2=0;comptoir=0;tonnelle=0' (insensible à la casse).
    Supporte un préfixe éventuel 'EQ|'.
    """
    eq = Equipment()
    if not notes:
        return eq
    part = notes.split("EQ|", 1)[-1] if "EQ|" in notes else notes
    for item in part.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            key = k.strip().lower()
            try:
                val = int(str(v).strip())
            except Exception:
                digits = "".join(ch for ch in str(v) if ch.isdigit())
                val = int(digits) if digits else 0
            if key in ("tireuse", "co2", "comptoir", "tonnelle"):
                setattr(eq, key, getattr(eq, key) + val)
    return eq


def effective_price(m: Movement, v: Variant) -> Optional[float]:
    """Prix effectif du mouvement : priorité à la saisie, sinon prix variante."""
    return m.unit_price_ttc if m.unit_price_ttc is not None else v.price_ttc


def effective_deposit(m: Movement) -> float:
    """Consigne effective : valeur saisie sinon 0 (le défaut est fixé à l’enregistrement si OUT)."""
    return m.deposit_per_keg if m.deposit_per_keg is not None else 0.0


def get_or_create_inventory(variant_id: int) -> Inventory:
    inv = Inventory.query.filter_by(variant_id=variant_id).first()
    if not inv:
        inv = Inventory(variant_id=variant_id, qty=0)
        db.session.add(inv)
        db.session.flush()
    return inv


def combine_equipment(acc: Equipment, part: Equipment, sign: int):
    acc.tireuse += sign * part.tireuse
    acc.co2 += sign * part.co2
    acc.comptoir += sign * part.comptoir
    acc.tonnelle += sign * part.tonnelle


def compute_reorder_alerts():
    alerts = []
    rules: Dict[int, ReorderRule] = {r.variant_id: r for r in ReorderRule.query.all()}
    for v in Variant.query.all():
        inv = get_or_create_inventory(v.id)
        rule = rules.get(v.id)
        if rule and rule.min_qty > 0 and (inv.qty or 0) < rule.min_qty:
            alerts.append({
                "variant": v,
                "product": v.product,
                "qty": inv.qty or 0,
                "min_qty": rule.min_qty,
                "need": rule.min_qty - (inv.qty or 0),
            })
    return alerts


# --- Résumés client (accueil + fiche) ---
def client_movements_full(client_id: int):
    return (
        db.session.query(Movement, Variant, Product)
        .join(Variant, Movement.variant_id == Variant.id)
        .join(Product, Variant.product_id == Product.id)
        .filter(Movement.client_id == client_id)
        .order_by(Movement.created_at.desc(), Movement.id.desc())
        .all()
    )


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

    for m, v in db.session.query(Movement, Variant)\
                          .join(Variant, Movement.variant_id == Variant.id)\
                          .filter(Movement.client_id == c.id).all():
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
            if m.type in {"DEFECT", "FULL"}:
                beer_eur -= (m.qty or 0) * price

    last_out = db.session.query(func.max(Movement.created_at))\
        .filter(Movement.client_id == c.id, Movement.type == "OUT").scalar()
    last_in = db.session.query(func.max(Movement.created_at))\
        .filter(Movement.client_id == c.id, Movement.type == "IN").scalar()

    return Card(
        id=c.id, name=c.name, kegs=kegs, beer_eur=beer_eur, deposit_eur=deposit_eur,
        last_out=last_out, last_in=last_in, equipment=equipment
    )


def summarize_client_for_detail(client_id: int):
    delivered_qty_cum = (
        db.session.query(func.coalesce(func.sum(Movement.qty * Variant.size_l), 0))
        .join(Variant, Movement.variant_id == Variant.id)
        .filter(Movement.client_id == client_id, Movement.type == "OUT")
        .scalar()
    )

    beer_billed_cum = 0.0
    deposit_in_play = 0.0
    equipment_totals = Equipment()
    movements_view = []

    for m, v, p in client_movements_full(client_id):
        price = effective_price(m, v) or 0.0
        dep = effective_deposit(m)
        eq = parse_equipment(m.notes)

        if m.type == "OUT":
            beer_billed_cum += (m.qty or 0) * price
            deposit_in_play += (m.qty or 0) * dep
            combine_equipment(equipment_totals, eq, +1)
        else:
            deposit_in_play -= (m.qty or 0) * dep
            combine_equipment(equipment_totals, eq, -1)
            if m.type in {"DEFECT", "FULL"}:
                beer_billed_cum -= (m.qty or 0) * price

        movements_view.append(type("MV", (), dict(
            id=m.id,
            created_at=(m.created_at.date().isoformat() if m.created_at else ""),
            type=m.type,
            qty=m.qty,
            unit_price_ttc=m.unit_price_ttc,
            deposit_per_keg=m.deposit_per_keg,
            notes=m.notes,
            variant=v,
            product=p,
        )))

    return delivered_qty_cum or 0, beer_billed_cum or 0.0, deposit_in_play or 0.0, equipment_totals, movements_view


def summarize_all_clients() -> List[Card]:
    return [summarize_client_for_index(c) for c in Client.query.order_by(Client.name).all()]


# --- Stock bar ---
def get_stock_items():
    variants = (
        db.session.query(Variant)
        .join(Product, Variant.product_id == Product.id)
        .order_by(Product.name, Variant.size_l)
        .all()
    )
    rules_by_vid = {r.variant_id: r for r in ReorderRule.query.all()}
    items = [{"variant": v, "inventory": get_or_create_inventory(v.id), "rule": rules_by_vid.get(v.id)} for v in variants]
    return items


def apply_inventory_effect(mtype: str, variant_id: int, qty: int):
    if mtype == "OUT":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) - qty
    elif mtype == "FULL":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) + qty


def revert_inventory_effect(mtype: str, variant_id: int, qty: int):
    if mtype == "OUT":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) + qty
    elif mtype == "FULL":
        inv = get_or_create_inventory(variant_id)
        inv.qty = (inv.qty or 0) - qty
