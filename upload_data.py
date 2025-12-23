import pandas as pd
from sqlalchemy import create_engine
import textwrap

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
# 1. PASTE YOUR NEON DATABASE URL HERE
# It looks like: postgres://user:pass@ep-xyz.aws.neon.tech/neondb
NEON_DB_URL = 'postgresql://neondb_owner:npg_Acq1QCzPyB4I@ep-lively-resonance-a1fhs3hx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require'

# Fix URL for SQLAlchemy if needed
if NEON_DB_URL.startswith("postgres://"):
    NEON_DB_URL = NEON_DB_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------
# MAIN UPLOAD LOGIC
# ---------------------------------------------------------
def upload_data():
    print("🚀 Connecting to Neon Database...")
    engine = create_engine(NEON_DB_URL)

    # --- 1. Upload Brand Medicines ---
    try:
        print("\nReading cleaned_medicines.csv...")
        df_meds = pd.read_csv("cleaned_medicines.csv")
        
        # Renaissance check: Ensure column names match your Database Models
        # If your CSV has different headers, rename them here:
        # df_meds = df_meds.rename(columns={"Drug Name": "brand_name", "Price": "mrp"})
        
        # Basic cleanup
        df_meds.columns = [c.lower().replace(' ', '_') for c in df_meds.columns]
        
        print(f"Uploading {len(df_meds)} brand medicines...")
        
        # 'if_exists="replace"' drops the table and recreates it. 
        # Use 'append' if you want to keep existing data.
        df_meds.to_sql('medicines', engine, if_exists='replace', index=False)
        print("✅ Brand medicines uploaded successfully!")
        
    except FileNotFoundError:
        print("❌ Error: cleaned_medicines.csv not found.")
    except Exception as e:
        print(f"❌ Error uploading medicines: {e}")

    # --- 2. Upload Jan Aushadhi Data ---
    try:
        print("\nReading jan_aushadhi_clean.csv...")
        df_jan = pd.read_csv("jan_aushadhi_clean.csv")
        
        # Basic cleanup
        df_jan.columns = [c.lower().replace(' ', '_') for c in df_jan.columns]

        print(f"Uploading {len(df_jan)} Jan Aushadhi records...")
        
        df_jan.to_sql('jan_aushadhi_clean', engine, if_exists='replace', index=False)
        print("✅ Jan Aushadhi data uploaded successfully!")
        
    except FileNotFoundError:
        print("❌ Error: jan_aushadhi_clean.csv not found.")
    except Exception as e:
        print(f"❌ Error uploading Jan Aushadhi: {e}")

    print("\n🎉 All Done! Your Render app should now show data.")

if __name__ == "__main__":
    upload_data()