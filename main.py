import os
import ast
import re
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from sqlalchemy import create_engine, Column, Integer, String, Float, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ============================================================
# DATABASE SETUP (Neon-compatible)
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL not set — configure it in Render env vars")

# Ensure SSL for Neon
if "sslmode" not in DATABASE_URL:
    DATABASE_URL += "?sslmode=require"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Safe DB session that prevents broken transactions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================
# MODELS (MATCH YOUR REAL NEON DB)
# ============================================================

class Medicine(Base):
    __tablename__ = "medicines"

    id = Column(Integer, primary_key=True, index=True)
    brand_name = Column(String, index=True)
    salt_composition = Column(String)
    manufacturer = Column(String)
    mrp = Column(Float)


class JanAushadhi(Base):
    __tablename__ = "jan_aushadhi"

    id = Column(Integer, primary_key=True, index=True)
    clean_name = Column(String, index=True)     # ✔ matches your Neon schema
    unit_size = Column(String)                  # ✔ matches
    price = Column(Float)


# DO NOT auto-create tables on production
if os.getenv("ENV") == "local":
    Base.metadata.create_all(bind=engine)


# ============================================================
# FASTAPI APP + CORS
# ============================================================

app = FastAPI(title="MedXchange API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# HELPERS
# ============================================================

def get_quantity_label(name: str, price: float):
    name_lower = name.lower()
    if "inj" in name_lower or "injection" in name_lower or "vial" in name_lower:
        return "Per Vial"
    if any(x in name_lower for x in ["syrup", "solution", "suspension", "drop"]):
        return "Per Bottle"
    if any(x in name_lower for x in ["cream", "gel", "ointment", "tube"]):
        return "Per Tube"
    if "sachet" in name_lower:
        return "Per Sachet"
    if price < 20:
        return "Per Strip/Tab"
    return "Per Pack"


def extract_all_salts(comp: str):
    if not comp:
        return []
    salts = []

    comp = comp.replace("none", "None")

    try:
        if comp.strip().startswith("["):
            parsed = ast.literal_eval(comp)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and "name" in item:
                        s = item["name"].lower()
                        s = re.sub(r"\s*\d+mg", "", s)
                        salts.append(s.strip())
    except:
        pass

    if not salts:
        parts = re.split(r"[+/]| and ", comp, flags=re.IGNORECASE)
        for p in parts:
            clean = p.lower().strip()
            clean = re.sub(r"\s*\d+mg", "", clean)
            if clean and clean != "none":
                salts.append(clean)

    return salts


def extract_dosages(text: str):
    vals = []
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g)", text.lower())
    for num, unit in matches:
        v = float(num)
        if unit in ("mcg", "µg"):
            v = v / 1000
        elif unit == "g":
            v = v * 1000
        vals.append(f"{v}mg")
    return sorted(vals)


def salts_match(salts, generic_name):
    generic_lower = generic_name.lower()
    return all(s in generic_lower for s in salts)


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/")
def root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "ok", "message": "MedXchange API running"}


@app.get("/search-brands")
def search_brands(query: str, db: Session = Depends(get_db)):
    if not query:
        return []

    rows = (
        db.query(Medicine.brand_name)
        .filter(Medicine.brand_name.ilike(f"{query}%"))
        .order_by(Medicine.brand_name.asc())
        .limit(10)
        .all()
    )

    return [r[0] for r in rows]


@app.get("/get-generic")
def get_generic(brand_name: str, db: Session = Depends(get_db)):
    """Main logic: brand → salts → find cheaper brands + Jan Aushadhi generics."""

    bn = brand_name.strip()

    # -------- Find brand in DB --------
    brand = (
        db.query(Medicine)
        .filter(Medicine.brand_name.ilike(f"{bn}%"))
        .order_by(Medicine.brand_name.asc())
        .first()
    )

    if not brand:
        brand = (
            db.query(Medicine)
            .filter(Medicine.brand_name.ilike(f"%{bn}%"))
            .first()
        )

    if not brand:
        return {"found": False, "message": f"No brand found for '{brand_name}'"}

    # -------- Extract salts from brand --------
    salts = extract_all_salts(brand.salt_composition)
    dosages = extract_dosages(brand.salt_composition + " " + brand.brand_name)

    # -------- Find substitute in brand list --------
    substitute = (
        db.query(Medicine)
        .filter(
            Medicine.salt_composition == brand.salt_composition,
            Medicine.id != brand.id,
            Medicine.mrp < brand.mrp,
            Medicine.mrp > 10,
        )
        .order_by(Medicine.mrp.asc())
        .first()
    )

    # -------- Find Jan Aushadhi generic --------
    gov_best = None
    all_gov = db.query(JanAushadhi).all()

    for g in all_gov:
        if not salts_match(salts, g.clean_name):
            continue

        gov_dosages = extract_dosages(g.clean_name)

        # must match dosages
        if dosages and gov_dosages and sorted(dosages) != sorted(gov_dosages):
            continue

        gov_best = g
        break

    # ========================================================
    # BUILD RESPONSE
    # ========================================================
    resp = {
        "found": True,
        "searched_brand": brand.brand_name,
        "brand_price": brand.mrp,
        "brand_qty": get_quantity_label(brand.brand_name, brand.mrp),
        "manufacturer": brand.manufacturer,
        "generic_name": salts[0] if salts else brand.salt_composition,
        "alternatives_found": False,
        "substitute": None,
        "gov_match": None
    }

    if substitute:
        resp["alternatives_found"] = True
        resp["substitute"] = {
            "name": substitute.brand_name,
            "price": substitute.mrp,
            "qty": get_quantity_label(substitute.brand_name, substitute.mrp),
            "manufacturer": substitute.manufacturer,
            "savings": round(brand.mrp - substitute.mrp, 2)
        }

    if gov_best:
        resp["alternatives_found"] = True
        resp["gov_match"] = {
            "name": gov_best.clean_name,
            "price": gov_best.price,
            "qty": gov_best.unit_size or "Per Pack",
            "savings": round(brand.mrp - gov_best.price, 2),
        }

    return resp
