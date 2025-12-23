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
DATABASE_URL = os.getenv("DATABASE_URL", pool_pre_ping=True)

# 2. Fix the URL prefix (Neon/Render use 'postgres://', SQLAlchemy needs 'postgresql://')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

print("DEBUG DATABASE_URL =", DATABASE_URL)


engine = create_engine(DATABASE_URL)
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
# HELPER: QUANTITY GUESSER (UPDATED)
# -------------------------------------------
def get_quantity_label(name: str, price: float):
    """
    Determines quantity label by checking keywords (Injection, Syrup, etc.)
    first, then falling back to price-based guessing.
    """
    name_lower = name.lower()

    # 1. Check for specific Dosage Forms first
    if 'injection' in name_lower or ' inj' in name_lower or 'vial' in name_lower:
        return "Per Vial"
    if any(x in name_lower for x in ['syrup', 'suspension', 'liquid', 'solution', 'drop']):
        return "Per Bottle"
    if any(x in name_lower for x in ['gel', 'cream', 'ointment', 'tube']):
        return "Per Tube"
    if 'sachet' in name_lower:
        return "Per Sachet"

    # 2. Existing logic for pack sizes (e.g. (10 tabs))
    match = re.search(r'\((\d+)\s*(?:tabs|tablets|caps|capsules)?\)', name, re.IGNORECASE)
    if match:
        return f"Pack of {match.group(1)}"
    
    match_s = re.search(r'\b(\d+)s\b', name, re.IGNORECASE)
    if match_s:
        return f"Pack of {match_s.group(1)}"

    # 3. Fallback based on price
    if price > 200:
        return "Per Box/Pack"
    elif price < 20:
        return "Per Strip/Tab"
    return "Per Pack"
# -------------------------------------------
# HELPER: TRANSLATION ALIASES
# -------------------------------------------
BRAND_ALIASES = {
    "tylenol": "Dolo",
    "panadol": "Dolo",
    "advil": "Brufen",
    "motrin": "Brufen",
    "claritin": "Alerta"
}

# -------------------------------------------
# HELPER: EXTRACT ALL SALTS FROM COMPOSITION
# -------------------------------------------
def extract_all_salts(salt_composition: str):
    """
    Extract all individual salt/compound names from composition.
    Returns a list of cleaned salt names.
    """
    if not salt_composition:
        return []
    
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
    except (ValueError, SyntaxError, AttributeError, KeyError):
        pass
    
    if not salts:
        parts = re.split(r'[+/,]|\band\b', raw, flags=re.IGNORECASE)
        for part in parts:
            clean = part.strip().lower()
            clean = re.sub(r'\s*\d+\s*m?g\b', '', clean)
            clean = re.sub(r'\s*\([^)]*\)', '', clean)
            if clean and clean != 'none':
                salts.append(clean.strip())
    
    return salts

# -------------------------------------------
# HELPER: NORMALIZE SALT NAME
# -------------------------------------------
def normalize_salt_name(salt: str):
    """
    Normalize salt name for better matching by removing common variations.
    """
    normalized = salt.lower().strip()
    normalized = re.sub(r'\s+(sodium|hydrochloride|hcl|sulphate|sulfate|chloride)$', '', normalized)
    return normalized

# -------------------------------------------
# HELPER: EXTRACT DOSAGE FROM TEXT
# -------------------------------------------
def extract_dosages(text: str):
    """
    Extract all dosage values (e.g., 75mg, 100mg, 325mg) from text.
    Returns list of tuples (value, unit) e.g., [(75, 'mg'), (100, 'mg')]
    """
    dosages = []
    matches = re.findall(r'(\d+(?:\.\d+)?)\s*(mg|g|mcg|µg)', text.lower())
    for value, unit in matches:
        if unit in ['mcg', 'µg']:
            dosages.append((float(value) / 1000, 'mg'))
        elif unit == 'g':
            dosages.append((float(value) * 1000, 'mg'))
        else:
            dosages.append((float(value), 'mg'))
    return dosages

def has_dosage_mismatch(brand_composition: str, brand_name: str, generic_name: str):
    """
    Check if dosages differ between brand and generic.
    Returns (bool, str) - (has_mismatch, warning_message)
    """
    brand_dosages = extract_dosages(brand_composition + " " + brand_name)
    generic_dosages = extract_dosages(generic_name)
    
    if not brand_dosages or not generic_dosages:
        return False, ""
    
    brand_sorted = sorted(brand_dosages)
    generic_sorted = sorted(generic_dosages)
    
    # 1. Normalize data into sets of strings immediately.
    # This handles deduplication, ordering, and ensures we compare exactly what the user sees.
    brand_set = {f"{v}{u}" for v, u in brand_dosages}
    generic_set = {f"{v}{u}" for v, u in generic_dosages}
    
    # 2. Compare the sets. 
    # Since we are comparing the strings, if they look identical, this returns False.
    if brand_set != generic_set:
        brand_str = " + ".join(sorted(brand_set))
        generic_str = " + ".join(sorted(generic_set))
        return True, f"⚠️ Note: Brand dosage ({brand_str}) differs from generic ({generic_str}). Consult your doctor before switching."
    
    return False, ""

# -------------------------------------------
# HELPER: COUNT SALTS IN GENERIC NAME
# -------------------------------------------
def count_salts_in_generic(generic_name: str):
    """
    Count how many distinct salts are in a generic name by looking for 'and', '+', '/' delimiters.
    """
    parts = re.split(r'\band\b|[+/]', generic_name.lower(), flags=re.IGNORECASE)
    meaningful_parts = [p.strip() for p in parts if p.strip() and not re.match(r'^\d+\s*m?g$', p.strip())]
    return len(meaningful_parts)

# -------------------------------------------
# HELPER: FIND BEST GOVERNMENT MATCH
# -------------------------------------------
def find_best_gov_match(salts: list, brand_dosages: list, db: Session):
    """
    Find the best Jan Aushadhi match by checking if:
    1. The government generic contains ALL the salts from the brand medicine
    2. The number of salts matches
    3. The dosage strength matches (NEW)
    """
    if not salts:
        return None
    
    all_generics = db.query(JanAushadhi).all()
    
    best_match = None
    max_match_score = 0
    
    # Create a set of brand dosages for comparison (e.g., {'650.0mg'})
    target_dosage_set = {f"{v}{u}" for v, u in brand_dosages}

    for generic in all_generics:
        generic_name_lower = generic.generic_name.lower()
        
        # 1. Salt Count Check
        generic_salt_count = count_salts_in_generic(generic.generic_name)
        if generic_salt_count != len(salts):
            continue
        
        # 2. Salt Name Matching
        match_score = 0
        for salt in salts:
            normalized_salt = normalize_salt_name(salt)
            if normalized_salt in generic_name_lower or any(normalized_salt in normalize_salt_name(part) for part in generic_name_lower.split()):
                match_score += 1
        
        # 3. Dosage Matching (Critical Fix)
        if match_score == len(salts):
            # Extract dosages from the generic name being checked
            generic_dosages = extract_dosages(generic.generic_name)
            generic_dosage_set = {f"{v}{u}" for v, u in generic_dosages}
            
            # If the brand has specific dosages defined, the generic MUST match them
            if target_dosage_set and generic_dosage_set:
                if target_dosage_set != generic_dosage_set:
                    continue  # Skip this generic if dosages don't match exactly
            
            # If we pass the dosage check, check if this is the best score
            if match_score > max_match_score:
                max_match_score = match_score
                best_match = generic
    
    return best_match
# -------------------------------------------
# MAIN SEARCH ENDPOINT
# -------------------------------------------
# -------------------------------------------
# SERVE HTML (ADD THIS SECTION)
# -------------------------------------------
@app.get("/")
def read_root():
    # This serves the HTML file when users visit your website URL
    return FileResponse("index.html")
# -------------------------------------------
# NEW ENDPOINT: AUTOCOMPLETE SEARCH
# -------------------------------------------
# legacy simple /search-brands removed; see consolidated implementation below

# -------------------------------------------
# NEW: AUTOCOMPLETE SEARCH ENDPOINT
# -------------------------------------------
@app.get("/search-brands")
def search_brands(query: str, db: Session = Depends(get_db)):
    if not query: return []
    
    # Search for brands starting with the query
    # We use .distinct() to avoid duplicate names in the dropdown
    brands = db.query(Medicine.brand_name)\
        .filter(Medicine.brand_name.ilike(f"{query}%"))\
        .order_by(Medicine.brand_name.asc())\
        .distinct()\
        .limit(10)\
        .all()
    
    # Return just the list of names: ["Zantac", "Zincovit", ...]
    return [b[0] for b in brands]

@app.get("/get-generic")
def get_generic_name(brand_name: str, db: Session = Depends(get_db)):
    
    # 1. Search Logic
    search_term = brand_name
    clean_input = brand_name.lower().strip()
    if clean_input in BRAND_ALIASES:
        search_term = BRAND_ALIASES[clean_input]

    brand_match = db.query(Medicine).filter(
        Medicine.brand_name.ilike(f"{search_term}%")
    ).order_by(Medicine.brand_name.asc()).first()

    if not brand_match:
        brand_match = db.query(Medicine).filter(
            Medicine.brand_name.ilike(f"%{search_term}%")
        ).order_by(Medicine.brand_name.asc()).first()

    if not brand_match:
        return {"found": False, "message": f"No brand found matching '{brand_name}'"}

    # 2. Find Substitute (Commercial)
    substitute = db.query(Medicine).filter(
        Medicine.salt_composition == brand_match.salt_composition,
        Medicine.id != brand_match.id,
        Medicine.mrp < brand_match.mrp,
        Medicine.mrp > 10
    ).order_by(Medicine.mrp.asc()).first()

    # 3. Extract Salts & Dosages
    all_salts = extract_all_salts(brand_match.salt_composition)
    clean_salt_name = all_salts[0] if all_salts else brand_match.salt_composition
    
    # --- FIX APPLIED HERE ---
    # Extract dosage from Brand Name + Composition (e.g., "Dolo 650" -> 650mg)
    brand_dosages = extract_dosages(f"{brand_match.salt_composition} {brand_match.brand_name}")
    
    # Pass dosages to the matching function to prevent 650mg vs 125mg mismatch
    gov_match = find_best_gov_match(all_salts, brand_dosages, db)
    # ------------------------

    # 4. Construct Response
    response = {
        "found": True,
        "searched_brand": brand_match.brand_name,
        "brand_price": brand_match.mrp,
        "brand_qty": get_quantity_label(brand_match.brand_name, brand_match.mrp),
        "manufacturer": brand_match.manufacturer,
        "generic_name": clean_salt_name,
        "substitute": None,
        "gov_match": None,
        "alternatives_found": False, # Default to False
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
        # Re-check mismatch for the warning flag in UI (double safety)
        has_mismatch, warning = has_dosage_mismatch(
            brand_match.salt_composition,
            brand_match.brand_name,
            gov_match.generic_name
        )
        
        response["gov_match"] = {
            "name": gov_match.generic_name,
            "price": gov_match.price,
            "qty": get_quantity_label(gov_match.generic_name, gov_match.price),
            "savings": round(brand_match.mrp - gov_match.price, 2),
            "dosage_warning": warning if has_mismatch else None
        }
        response["alternatives_found"] = True

    return response