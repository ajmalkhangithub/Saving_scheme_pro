from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
import sqlite3
from datetime import datetime

DATABASE_FILE = "/tmp/central_welfare.db"

app = FastAPI(
    title="Welfare Scheme Synchronization Engine",
    version="1.0.0",
    description="Offline-First Central Database Sync for National Welfare Savings App"
)
@app.get("/")
def home():
    return {"status": "success", "message": "Backend is running live on Vercel!"}
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Database Setup (Mirrors Client Schema) -----------------

def get_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_central_database():
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name TEXT NOT NULL,
            monthly_amount REAL NOT NULL,
            cycle_months INTEGER DEFAULT 3,
            payout_percentage REAL DEFAULT 40.0
        );

        CREATE TABLE IF NOT EXISTS users (
            cnic TEXT PRIMARY KEY,
            full_name TEXT NOT NULL,
            digital_wallet_no TEXT,
            village TEXT DEFAULT 'Model Village',
            uc TEXT DEFAULT 'UC-1',
            district TEXT DEFAULT 'District Central',
            latitude REAL,
            longitude REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_synced INTEGER DEFAULT 1,
            server_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS enrollments (
            enrollment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cnic TEXT NOT NULL,
            plan_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            current_cycle INTEGER DEFAULT 1,
            status TEXT DEFAULT 'ACTIVE',
            is_synced INTEGER DEFAULT 1,
            server_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cnic) REFERENCES users(cnic) ON DELETE CASCADE,
            FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
        );

        CREATE TABLE IF NOT EXISTS deposits (
            deposit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_id INTEGER NOT NULL,
            cycle_number INTEGER NOT NULL,
            month_number INTEGER NOT NULL,
            expected_amount REAL NOT NULL,
            deposit_amount REAL DEFAULT 0,
            deposit_date TEXT,
            is_verified INTEGER DEFAULT 0,
            verified_at TIMESTAMP,
            is_synced INTEGER DEFAULT 1,
            server_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (enrollment_id) REFERENCES enrollments(enrollment_id) ON DELETE CASCADE
        );

        -- Seed Default Welfare Plans matching Expo client
        INSERT OR IGNORE INTO plans (plan_id, plan_name, monthly_amount, cycle_months, payout_percentage)
        VALUES 
            (1, 'Starter Welfare', 300, 3, 40.0),
            (2, 'Basic Savings', 500, 3, 40.0),
            (3, 'Premium Growth', 700, 3, 40.0);
        """)

init_central_database()

# ----------------- Pydantic Payloads -----------------

class UserSyncModel(BaseModel):
    cnic: str
    full_name: str
    digital_wallet_no: Optional[str] = None
    village: Optional[str] = "Model Village"
    uc: Optional[str] = "UC-1"
    district: Optional[str] = "District Central"
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class EnrollmentSyncModel(BaseModel):
    cnic: str
    plan_id: int = 1
    start_date: str
    end_date: str
    current_cycle: int = 1
    status: str = "ACTIVE"

class DepositSyncModel(BaseModel):
    cnic: str
    cycle_number: int
    month_number: int
    expected_amount: float
    deposit_amount: float
    deposit_date: Optional[str] = None
    is_verified: int = 1

class SyncRequest(BaseModel):
    last_sync_timestamp: Optional[str] = None
    users: List[UserSyncModel] = []
    enrollments: List[EnrollmentSyncModel] = []
    deposits: List[DepositSyncModel] = []

# ----------------- Endpoints -----------------

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.post("/api/sync")
def sync_offline_records(data: SyncRequest):
    now = datetime.utcnow().isoformat()
    synced_user_cnics = []
    synced_deposit_keys = []

    with get_connection() as conn:
        # 1. Sync Inbound Users
        for user in data.users:
            clean_cnic = user.cnic.strip()
            conn.execute("""
                INSERT INTO users (cnic, full_name, digital_wallet_no, village, uc, district, latitude, longitude, is_synced, server_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(cnic) DO UPDATE SET
                    full_name = excluded.full_name,
                    digital_wallet_no = excluded.digital_wallet_no,
                    server_updated_at = excluded.server_updated_at;
            """, (
                clean_cnic,
                user.full_name.strip(),
                user.digital_wallet_no,
                user.village or "Model Village",
                user.uc or "UC-1",
                user.district or "District Central",
                user.latitude,
                user.longitude,
                now
            ))
            synced_user_cnics.append(clean_cnic)

        # 2. Sync Inbound Enrollments (With Foreign-Key Pre-checks)
        for enr in data.enrollments:
            clean_cnic = enr.cnic.strip()

            # Safeguard A: Ensure the user exists in users table first
            conn.execute("""
                INSERT OR IGNORE INTO users (cnic, full_name, digital_wallet_no, is_synced, server_updated_at)
                VALUES (?, 'Verified Citizen', NULL, 1, ?);
            """, (clean_cnic, now))

            # Safeguard B: Ensure plan_id is valid
            plan = conn.execute("SELECT plan_id FROM plans WHERE plan_id = ?", (enr.plan_id,)).fetchone()
            valid_plan_id = enr.plan_id if plan else 1

            conn.execute("""
                INSERT INTO enrollments (cnic, plan_id, start_date, end_date, current_cycle, status, is_synced, server_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?);
            """, (
                clean_cnic,
                valid_plan_id,
                enr.start_date,
                enr.end_date,
                enr.current_cycle,
                enr.status or "ACTIVE",
                now
            ))

        # 3. Sync Inbound Deposits (With Foreign-Key Pre-checks)
        for dep in data.deposits:
            clean_cnic = dep.cnic.strip()

            # Ensure user exists
            conn.execute("""
                INSERT OR IGNORE INTO users (cnic, full_name, digital_wallet_no, is_synced, server_updated_at)
                VALUES (?, 'Verified Citizen', NULL, 1, ?);
            """, (clean_cnic, now))

            # Find existing enrollment or create a fallback
            enrollment = conn.execute(
                "SELECT enrollment_id FROM enrollments WHERE cnic = ? ORDER BY enrollment_id DESC LIMIT 1;",
                (clean_cnic,)
            ).fetchone()

            if not enrollment:
                cur = conn.execute("""
                    INSERT INTO enrollments (cnic, plan_id, start_date, end_date, current_cycle, status, is_synced, server_updated_at)
                    VALUES (?, 1, date('now'), date('now', '+90 days'), 1, 'ACTIVE', 1, ?);
                """, (clean_cnic, now))
                enrollment_id = cur.lastrowid
            else:
                enrollment_id = enrollment["enrollment_id"]

            # Upsert deposit row
            existing = conn.execute("""
                SELECT deposit_id FROM deposits 
                WHERE enrollment_id = ? AND cycle_number = ? AND month_number = ?;
            """, (enrollment_id, dep.cycle_number, dep.month_number)).fetchone()

            if existing:
                conn.execute("""
                    UPDATE deposits 
                    SET deposit_amount = ?, deposit_date = ?, is_verified = ?, is_synced = 1, server_updated_at = ?
                    WHERE deposit_id = ?;
                """, (dep.deposit_amount, dep.deposit_date, dep.is_verified, now, existing["deposit_id"]))
            else:
                conn.execute("""
                    INSERT INTO deposits (enrollment_id, cycle_number, month_number, expected_amount, deposit_amount, deposit_date, is_verified, is_synced, server_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?);
                """, (
                    enrollment_id,
                    dep.cycle_number,
                    dep.month_number,
                    dep.expected_amount,
                    dep.deposit_amount,
                    dep.deposit_date,
                    dep.is_verified,
                    now
                ))
            synced_deposit_keys.append(f"{clean_cnic}-{dep.cycle_number}-{dep.month_number}")

        conn.commit()

        # 4. Pull Server-side updates
        server_updates = {"users": [], "deposits": []}
        if data.last_sync_timestamp:
            users_pull = conn.execute(
                "SELECT cnic, full_name, digital_wallet_no, village, uc, district FROM users WHERE server_updated_at > ?",
                (data.last_sync_timestamp,)
            ).fetchall()
            server_updates["users"] = [dict(u) for u in users_pull]

            deposits_pull = conn.execute("""
                SELECT e.cnic, d.cycle_number, d.month_number, d.expected_amount, d.deposit_amount, d.deposit_date, d.is_verified
                FROM deposits d
                JOIN enrollments e ON d.enrollment_id = e.enrollment_id
                WHERE d.server_updated_at > ?;
            """, (data.last_sync_timestamp,)).fetchall()
            server_updates["deposits"] = [dict(d) for d in deposits_pull]

    return {
        "status": "success",
        "synced_users": synced_user_cnics,
        "synced_deposits": synced_deposit_keys,
        "server_updates": server_updates,
        "sync_timestamp": now
    }