"""lib/arcadedb.py — Alamat, kredensial, dan pemanggil ArcadeDB.

Satu-satunya tempat koneksi ditentukan. Nilainya dibaca dari .env; tanpa .env
dipakai ArcadeDB lokal (Docker di laptop):

    ARCADEDB_HOST=http://localhost:2480     # mis. https://arcade.riset.web.id
    ARCADEDB_DB=sre_test
    ARCADEDB_USER=root
    ARCADEDB_PASSWORD=sre-team

Dua endpoint:
    ARCADEDB_URL  /api/v1/command/  boleh menulis; untuk skrip pembangun graf
    URL_BACA      /api/v1/query/    baca saja; untuk server dan Cypher dari LLM

Hanya mengimpor `requests` (dan dotenv bila ada), supaya cepat dimuat.

`jalankan()` menjaga satu jebakan: ArcadeDB memotong hasil di 20.000 baris
tanpa galat. Bila bendera `truncated` muncul, ia berhenti.
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
ARCADEDB_AUTH = (_env("ARCADEDB_USER", "root"), _env("ARCADEDB_PASSWORD", "sre-team"))

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
