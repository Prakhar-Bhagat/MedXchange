import os
import ast
import re
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ============================================================
# DATABASE SETUP (Neon-compatible)
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL not set.")

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
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except:
        db.rollback()
        raise
    finally:
        db.close()


# ============================================================
# MODELS
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
    clean_name = Column(String, index=True)
    unit_size = Column(String)
    price = Column(Float)


# ============================================================
# APP + CORS
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
    if any(x in name_lower for x in ["inj", "injection", "vial"]):
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

    try:
        if comp.strip().startswith("["):
            parsed = ast.literal_eval(comp)
            return [item["name"].lower().strip() for item in parsed]
    except:
        pass

    parts = re.split(r"[+/]| and ", comp, flags=re.IGNORECASE)
    out = []
    for p in parts:
        clean = p.lower().strip()
        clean = re.sub(r"\s*\d+mg", "", clean)
        if clean and clean != "none":
            out.append(clean)
    return out


SALT_EQUIVALENTS = {
    "paracetamol": ["paracetamol", "acetaminophen"],
}


def salts_match(salts, generic_name):
    generic = generic_name.lower().replace("ip", "").replace("usp", "")
    generic = re.sub(r"\s+", " ", generic)

    for salt in salts:
        s = salt.lower().strip()

        if s in generic:
            continue

        if s in SALT_EQUIVALENTS:
            if any(alt in generic for alt in SALT_EQUIVALENTS[s]):
                continue

        return False

    return True


def extract_dosages(text: str):
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g)", text.lower())
    out = []

    for num, unit in matches:
        n = float(num)
        if unit in ("mcg", "µg"):
            n /= 1000
        elif unit == "g":
            n *= 1000
        out.append(n)

    return sorted(out)


# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/")
def root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "ok"}


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
    bn = brand_name.strip()

    brand = (
        db.query(Medicine)
        .filter(Medicine.brand_name.ilike(f"{bn}%"))
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

    salts = extract_all_salts(brand.salt_composition)
    brand_dose = extract_dosages(brand.salt_composition + " " + brand.brand_name)

    # -------- Private substitute --------
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

    # -------- Govt generic (strict salts, flexible dosage) --------
    gov_best = None
    dosage_warning = None

    for g in db.query(JanAushadhi).all():
        if not salts_match(salts, g.clean_name):
            continue

        gov_dose = extract_dosages(g.clean_name)

        if brand_dose != gov_dose:
            dosage_warning = (
                f"⚠ Dosage differs: Brand ({brand_dose}) vs Govt ({gov_dose}). "
                "Consult your doctor."
            )

        gov_best = g
        break

    # -------- Response --------
    resp = {
        "found": True,
        "searched_brand": brand.brand_name,
        "brand_price": brand.mrp,
        "brand_qty": get_quantity_label(brand.brand_name, brand.mrp),
        "manufacturer": brand.manufacturer,
        "alternatives_found": False,
        "substitute": None,
        "gov_match": None,
    }

    if substitute:
        resp["alternatives_found"] = True
        resp["substitute"] = {
            "name": substitute.brand_name,
            "price": substitute.mrp,
            "qty": get_quantity_label(substitute.brand_name, substitute.mrp),
            "manufacturer": substitute.manufacturer,
            "savings": round(brand.mrp - substitute.mrp, 2),
        }

    if gov_best:
        resp["alternatives_found"] = True
        resp["gov_match"] = {
            "name": gov_best.clean_name,
            "price": gov_best.price,
            "qty": gov_best.unit_size or "Per Pack",
            "savings": round(brand.mrp - gov_best.price, 2),
            "dosage_warning": dosage_warning,
        }

    return resp
