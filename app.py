# app.py — version SANS ecocup/gobelets + auto-fix schéma (volume_l, deposit_eur)

import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import func, case, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ProgrammingError, OperationalError
from flask_migrate import Migrate

from models import db, Client, Product, Variant, Movement

# --------- Données par défaut ----------
DEFAULT_CLIENTS = [
    "Landerneau Football Club",
    "Maison Michel",
    "Ploudiry / Kermoysan",
    "Association Fest Noz",
    "Comité des Fêtes",
]

DEFAULT_PRODUCTS = [
    ("Coreff Blonde", [20, 30]),
    ("Coreff Ambrée", [20, 30]),
    ("Coreff Blanche", [20]),
    ("Cidre Brut", [20]),
]


def ensure_schema(engine: Engine):
    """
    Corrige automatiquement le schéma BDD si besoin.
    - Ajoute variant.volume_l (INTEGER) si manquant
    - Ajoute variant.deposit_eur (INTEGER DEFAULT 0) si manquant
    Fonctionne pour PostgreSQL et SQLite.
    """
    with engine.connect() as conn:
        dialect = engine.dialect.name  # "postgresql" | "sqlite"
        # Récupérer les colonnes existantes de "variant"
        existing_cols = set()
        if dialect == "sqlite":
            res = conn.execute(text("PRAGMA table_info(variant)"))
            for row in res:
                existing_cols.add(row[1])  # name
        else:
            # PostgreSQL et co : information_schema
