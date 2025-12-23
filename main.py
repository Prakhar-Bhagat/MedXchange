import os
import ast
import re
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# -------------------------------------------
# DATABASE SETUP
# -------------------------------------------
# 1. Get the URL
DATABASE_URL = os.getenv("DATABASE_URL")

# 2. Fallback for local testing (Do NOT use this in production if possible)
if not DATABASE_URL:
    DATABASE_URL = "postgresql://pb:mypassw@localhost/saltswap"

# 3. Fix the URL prefix for Neon/Render compatibility
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

print("DEBUG DATABASE_URL =", DATABASE_URL)

# 4. CREATE THE ENGINE (This is the line that was missing!)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# 5. Create the Session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# -------------------------------------------
# MODELS
# -------------------------------------------
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
    generic_name = Column(String, index=True)
    price = Column(Float)

# Create tables if they don't exist
Base.metadata.create_all(bind=engine)

# -------------------------------------------
# APP & CORS
# -------------------------------------------
app = FastAPI(title="MedXchange API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------
def get_quantity_label(name: str, price: float):
    name_lower = name.lower()
    if 'injection' in name_lower or ' inj' in name_lower or 'vial' in name_lower:
        return "Per Vial"
    if any(x in name_lower for x in ['syrup', 'suspension', 'liquid', 'solution', 'drop']):
        return "Per Bottle"
    if any(x in name_lower for x in ['gel', 'cream', 'ointment', 'tube']):
        return "Per Tube"
    if 'sachet' in name_lower:
        return "Per Sachet"
    
    match = re.search(r'\((\d+)\s*(?:tabs|tablets|caps|capsules)?\)', name, re.IGNORECASE)
    if match: return f"Pack of {match.group(1)}"
    
    match_s = re.search(r'\b(\d+)s\b', name, re.IGNORECASE)
    if match_s: return f"Pack of {match_s.group(1)}"

    if price > 200: return "Per Box/Pack"
    elif price < 20: return "Per Strip/Tab"
    return "Per Pack"

BRAND_ALIASES = {
    "tylenol": "Dolo",
    "panadol": "Dolo",
    "advil": "Brufen",
    "motrin": "Brufen",
    "claritin": "Alerta"
}

def extract_all_salts(salt_composition: str):
    if not salt_composition: return []
    salts = []
    raw = salt_composition.replace("none", "None")
    
    try:
        if raw.strip().startswith("["):
            data = ast.literal_eval(raw)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and 'name' in item:
                        salt_name = item['name'].strip().lower()
                        salt_name = re.sub(r'\s*\d+\s*m?g\b', '', salt_name)
                        salt_name = re.sub(r'\s*\([^)]*\)', '', salt_name)
                        if salt_name:
                            salts.append(salt_name.strip())
    except: pass
    
    if not salts:
        parts = re.split(r'[+/,]|\band\b', raw, flags=re.IGNORECASE)
        for part in parts:
            clean = part.strip().lower()
            clean = re.sub(r'\s*\d+\s*m?g\b', '', clean)
            clean = re.sub(r'\s*\([^)]*\)', '', clean)
            if clean and clean != 'none':
                salts.append(clean.strip())
    return salts

def normalize_salt_name(salt: str):
    normalized = salt.lower().strip()
    normalized = re.sub(r'\s+(sodium|hydrochloride|hcl|sulphate|sulfate|chloride)$', '', normalized)
    return normalized

def extract_dosages(text: str):
    dosages = []
    matches = re.findall(r'(\d+(?:\.\d+)?)\s*(mg|g|mcg|µg)', text.lower())
    for value, unit in matches:
        if unit in ['mcg', 'µg']: dosages.append((float(value) / 1000, 'mg'))
        elif unit == 'g': dosages.append((float(value) * 1000, 'mg'))
        else: dosages.append((float(value), 'mg'))
    return dosages

def has_dosage_mismatch(brand_composition: str, brand_name: str, generic_name: str):
    brand_dosages = extract_dosages(brand_composition + " " + brand_name)
    generic_dosages = extract_dosages(generic_name)
    
    if not brand_dosages or not generic_dosages: return False, ""
    
    brand_set = {f"{v}{u}" for v, u in brand_dosages}
    generic_set = {f"{v}{u}" for v, u in generic_dosages}
    
    if brand_set != generic_set:
        brand_str = " + ".join(sorted(brand_set))
        generic_str = " + ".join(sorted(generic_set))
        return True, f"⚠️ Note: Brand dosage ({brand_str}) differs from generic ({generic_str}). Consult your doctor."
    
    return False, ""

def count_salts_in_generic(generic_name: str):
    parts = re.split(r'\band\b|[+/]', generic_name.lower(), flags=re.IGNORECASE)
    meaningful_parts = [p.strip() for p in parts if p.strip() and not re.match(r'^\d+\s*m?g$', p.strip())]
    return len(meaningful_parts)

def find_best_gov_match(salts: list, brand_dosages: list, db: Session):
    if not salts: return None
    all_generics = db.query(JanAushadhi).all()
    
    best_match = None
    max_match_score = 0
    target_dosage_set = {f"{v}{u}" for v, u in brand_dosages}

    for generic in all_generics:
        generic_name_lower = generic.generic_name.lower()
        if count_salts_in_generic(generic.generic_name) != len(salts): continue
        
        match_score = 0
        for salt in salts:
            normalized_salt = normalize_salt_name(salt)
            if normalized_salt in generic_name_lower or any(normalized_salt in normalize_salt_name(part) for part in generic_name_lower.split()):
                match_score += 1
        
        if match_score == len(salts):
            generic_dosages = extract_dosages(generic.generic_name)
            generic_dosage_set = {f"{v}{u}" for v, u in generic_dosages}
            if target_dosage_set and generic_dosage_set and target_dosage_set != generic_dosage_set:
                continue
            
            if match_score > max_match_score:
                max_match_score = match_score
                best_match = generic
    return best_match

# -------------------------------------------
# ENDPOINTS
# -------------------------------------------
@app.get("/")
def read_root():
    # If index.html is in the same folder, this works.
    # Otherwise, consider returning a simple JSON message for the API root.
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "MedXchange API is running"}

@app.get("/search-brands")
def search_brands(query: str, db: Session = Depends(get_db)):
    if not query: return []
    brands = db.query(Medicine.brand_name)\
        .filter(Medicine.brand_name.ilike(f"{query}%"))\
        .order_by(Medicine.brand_name.asc())\
        .distinct().limit(10).all()
    return [b[0] for b in brands]

@app.get("/get-generic")
def get_generic_name(brand_name: str, db: Session = Depends(get_db)):
    search_term = brand_name
    clean_input = brand_name.lower().strip()
    if clean_input in BRAND_ALIASES: search_term = BRAND_ALIASES[clean_input]

    brand_match = db.query(Medicine).filter(Medicine.brand_name.ilike(f"{search_term}%")).order_by(Medicine.brand_name.asc()).first()
    if not brand_match:
        brand_match = db.query(Medicine).filter(Medicine.brand_name.ilike(f"%{search_term}%")).order_by(Medicine.brand_name.asc()).first()

    if not brand_match:
        return {"found": False, "message": f"No brand found matching '{brand_name}'"}

    substitute = db.query(Medicine).filter(
        Medicine.salt_composition == brand_match.salt_composition,
        Medicine.id != brand_match.id,
        Medicine.mrp < brand_match.mrp,
        Medicine.mrp > 10
    ).order_by(Medicine.mrp.asc()).first()

    all_salts = extract_all_salts(brand_match.salt_composition)
    clean_salt_name = all_salts[0] if all_salts else brand_match.salt_composition
    brand_dosages = extract_dosages(f"{brand_match.salt_composition} {brand_match.brand_name}")
    
    gov_match = find_best_gov_match(all_salts, brand_dosages, db)

    response = {
        "found": True,
        "searched_brand": brand_match.brand_name,
        "brand_price": brand_match.mrp,
        "brand_qty": get_quantity_label(brand_match.brand_name, brand_match.mrp),
        "manufacturer": brand_match.manufacturer,
        "generic_name": clean_salt_name,
        "substitute": None,
        "gov_match": None,
        "alternatives_found": False,
        "message": "No safe alternatives found."
    }

    if substitute:
        response["substitute"] = {
            "name": substitute.brand_name,
            "price": substitute.mrp,
            "qty": get_quantity_label(substitute.brand_name, substitute.mrp),
            "manufacturer": substitute.manufacturer,
            "savings": round(brand_match.mrp - substitute.mrp, 2)
        }
        response["alternatives_found"] = True

    if gov_match:
        has_mismatch, warning = has_dosage_mismatch(brand_match.salt_composition, brand_match.brand_name, gov_match.generic_name)
        response["gov_match"] = {
            "name": gov_match.generic_name,
            "price": gov_match.price,
            "qty": get_quantity_label(gov_match.generic_name, gov_match.price),
            "savings": round(brand_match.mrp - gov_match.price, 2),
            "dosage_warning": warning if has_mismatch else None
        }
        response["alternatives_found"] = True

    return response