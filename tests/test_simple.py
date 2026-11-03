import unittest
import sqlite3
import tempfile
from pathlib import Path

class TestSimple(unittest.TestCase):
    def test_simple_insert(self):
        db = tempfile.NamedTemporaryFile(delete=False)
        db_path = Path(db.name)
        db.close()
        
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE messages (role TEXT, timestamp REAL)")
                conn.execute("INSERT INTO messages (role, timestamp) VALUES ('tool', 167253000.0)")
                cursor = conn.execute("SELECT * FROM messages WHERE timestamp >= 167253000.0")
                rows = cursor.fetchall()
                print(f"DEBUG: Rows found: {len(rows)}")
                for r in rows: print(f"DEBUG: {r}")
            
            print("SUCCESS")
        finally:
            if db_path.exists():
                db_path.unlink()

if __name__ == "__main__":
    unittest.main()
