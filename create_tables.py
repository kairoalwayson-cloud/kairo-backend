"""
Run this script once to create all tables in the database.
Usage: python create_tables.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import Base, engine
from app.models import *  # noqa — imports all models so SQLAlchemy registers them

def main():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    print("All tables created successfully!")

    # List created tables
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"\nTables in database ({len(tables)}):")
    for t in sorted(tables):
        print(f"  - {t}")

if __name__ == "__main__":
    main()
