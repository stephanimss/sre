"""lib/arcadedb.py — Konfigurasi koneksi dan pemanggil ArcadeDB.

Nilai dibaca dari .env (bawaan: ArcadeDB lokal):

    ARCADEDB_HOST=http://localhost:2480
    ARCADEDB_DB=sre_test
    ARCADEDB_USER=root
    ARCADEDB_PASSWORD=<sandi>

ARCADEDB_URL  /api/v1/command/  boleh menulis
URL_BACA      /api/v1/query/    baca saja (dipakai server dan tanya-graf)

jalankan() berhenti bila hasil terpotong di batas 20.000 baris ArcadeDB.
"""
import os
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:                                   # dotenv opsional
    pass


def _env(nama, bawaan):
    """Nilai .env; baris yang ada tapi kosong juga jatuh ke bawaan."""
    return (os.getenv(nama) or "").strip() or bawaan


HOST = _env("ARCADEDB_HOST", "http://localhost:2480").rstrip("/")
DB = _env("ARCADEDB_DB", "sre_test")
ARCADEDB_AUTH = (_env("ARCADEDB_USER", "root"), _env("ARCADEDB_PASSWORD", ""))

ARCADEDB_URL = f"{HOST}/api/v1/command/{DB}"
URL_BACA = f"{HOST}/api/v1/query/{DB}"

BATAS_AMAN = 20000          # batas potong ArcadeDB; paginasi di bawah ini


def jalankan(perintah, params=None, timeout=600):
    """Jalankan openCypher. Melempar bila gagal ATAU bila hasilnya terpotong."""
    body = {"language": "opencypher", "command": perintah}
    if params:
        body["params"] = params
    r = requests.post(ARCADEDB_URL, auth=ARCADEDB_AUTH, timeout=timeout, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"ArcadeDB {r.status_code}: {r.text[:400]}")
    hasil = r.json()
    if hasil.get("truncated"):
        raise RuntimeError(
            "Hasil TERPOTONG oleh ArcadeDB. Paginasi query-nya dan cocokkan "
            "jumlahnya dgn count() -- jangan pakai hasil setengah.")
    return hasil.get("result", [])


def hitung(perintah, medan="n"):
    """Pintasan untuk query yang mengembalikan satu angka."""
    h = jalankan(perintah)
    return h[0][medan] if h else 0
