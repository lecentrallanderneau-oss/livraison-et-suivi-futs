# models.py — mêmes schémas, docstrings + annotations
from __future__ import annotations
from typing import Optional
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, Float, Text, DateTime, ForeignKey, UniqueConstraint

db = SQLAlchemy()


class Client(db.Model):
    """Client destinataire des fûts."""
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    movements = relationship("Movement", backref="client", lazy=True, cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Client {self.id} {self.name!r}>"


class Product(db.Model):
    """Bière (ex: Coreff Blonde)."""
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    variants = relationship("Variant", backref="product", lazy=True, cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Product {self.id} {self.name!r}>"


class Variant(db.Model):
    """Variante de produit (contenance, prix TTC)."""
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    size_l: Mapped[int] = mapped_column(Integer, nullable=False)  # 20, 22, 30...
    price_ttc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    movements = relationship("Movement", backref="variant", lazy=True)
    inventory = relationship("Inventory", backref="variant", uselist=False, cascade="all, delete-orphan")
    reorder_rule = relationship("ReorderRule", backref="variant", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint('product_id', 'size_l', name='uix_product_size'),
    )

    def __repr__(self) -> str:
        return f"<Variant {self.id} p={self.product_id} {self.size_l}L>"


class Movement(db.Model):
    """Mouvement client: OUT (livraison), IN (reprise), DEFECT (défectueux), FULL (retour plein)."""
    __tablename__ = "movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(10), nullable=False)  # OUT, IN, DEFECT, FULL
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False, index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), nullable=False, index=True)

    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    unit_price_ttc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    deposit_per_keg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Movement {self.id} {self.type} c={self.client_id} v={self.variant_id} qty={self.qty}>"


class Inventory(db.Model):
    """Stock bar (fûts pleins) par variante."""
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), nullable=False, unique=True, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Inventory v={self.variant_id} qty={self.qty}>"


class ReorderRule(db.Model):
    """Seuil mini à maintenir par variante (alertes réassort)."""
    __tablename__ = "reorder_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id"), nullable=False, unique=True, index=True)
    min_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<ReorderRule v={self.variant_id} min={self.min_qty}>"
