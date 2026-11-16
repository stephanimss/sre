"""lib/tanya_graf.py — Menjalankan Cypher yang DITULIS LLM, dengan tiga penjaga.

Endpoint yang dipakai /api/v1/query/ (baca saja), bukan /command/ seperti
lib/arcadedb.py. ArcadeDB sendiri menolak CREATE, SET, dan DELETE di sana
(QueryNotIdempotentException), jadi penjagaannya tidak bisa diakali.

TIGA PENJAGA
  1. nama asing -- bahaya utama. `:Researcher`, `COLLABORATES_WITH`, atau
     `n.name` tidak melempar galat, hanya 0 baris. Ontologinya berbahasa
     Indonesia, sedangkan model terbiasa dengan nama Inggris. Kueri ditolak
     sebelum menyentuh basis data, disertai nama terdekat.
  2. terpotong -- hasil dipangkas di 20.000 baris; terdeteksi dari bendera
     `truncated`.
  3. kosong -- 0 baris dilaporkan sebagai status tersendiri. Setelah penjaga 1
     lolos, kosong berarti datanya memang tidak ada.

Yang tidak bisa dijaga: Cypher sah yang menjawab pertanyaan lain (mis.
SEBIDANG dipakai untuk "pernah menulis bersama"). Karena itu Cypher selalu
ditampilkan ke pengguna.
"""
import difflib
import re

import requests

# Endpoint baca-saja (alamat dari .env, lihat lib/arcadedb.py).
# Jangan diganti ke /command/.
from lib.arcadedb import ARCADEDB_AUTH as AUTH, URL_BACA  # noqa: E402

BATAS_ARCADEDB = 20000      # tempat ArcadeDB memotong, terukur

# Nama fungsi dan namespace yang bukan properti (untuk penjaga 1).
BUKAN_PROPERTI = {"apoc", "algo", "db", "dbms", "math", "date", "datetime"}


# Hanya untuk pesan yang jelas. Penjagaan sebenarnya ada di endpoint
# baca-saja ArcadeDB.
POLA_TULIS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|REMOVE|DROP|TRUNCATE)\b|"
    r"\bSET\s+\w+\s*[.=]", re.I)


class Terjaga(Exception):
    """Query ditolak sebelum dijalankan."""


# ---------------------------------------------------------------- skema

_skema_tersimpan = None


def skema(segarkan=False):
    """Daftar label, jenis relasi, dan properti -- dibaca dari basis data.

    Dibaca dari DB supaya penjaga 1 ikut mutakhir saat skema berubah.
    """
    global _skema_tersimpan
    if _skema_tersimpan is not None and not segarkan:
        return _skema_tersimpan

    label = {r["t"]: r["n"] for r in _kirim(
        "MATCH (n) RETURN labels(n)[0] AS t, count(n) AS n ORDER BY n DESC")["result"]}
    relasi = {r["t"]: r["n"] for r in _kirim(
        "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS n ORDER BY n DESC")["result"]}

    # Daftar properti ini jalur cepat, bukan daftar lengkap: skemanya hanya
    # memuat properti terindeks, dan sampel meleset untuk properti jarang.
    # Sisanya diperiksa lewat _properti_ada().
    properti = {}
    for lab in label:
        p_set = set()
        for b in _kirim(f"MATCH (n:{lab}) RETURN n LIMIT 60")["result"]:
            isi = b.get("n", b)
            if isinstance(isi, dict):
                p_set |= {k for k in isi if not k.startswith("@")}
        properti[lab] = sorted(p_set)
    # Properti relasi ikut didaftar (mis. `r.jumlah`).
    for jenis in relasi:
        p_set = set()
        for b in _kirim(f"MATCH ()-[r:{jenis}]->() RETURN r LIMIT 60")["result"]:
            isi = b.get("r", b)
            if isinstance(isi, dict):
                p_set |= {k for k in isi if not k.startswith("@")}
        properti[f"[:{jenis}]"] = sorted(p_set)

    _skema_tersimpan = {"label": label, "relasi": relasi, "properti": properti}
    return _skema_tersimpan


def _kirim(cypher, timeout=120):
    r = requests.post(URL_BACA, auth=AUTH, timeout=timeout,
                      json={"language": "opencypher", "command": cypher})
    if r.status_code >= 400:
        raise RuntimeError(f"ArcadeDB {r.status_code}: {r.text[:300]}")
    return r.json()


# ------------------------------------------------- penjaga 1: nama asing

# Nama sesudah ':' di dalam [...] adalah jenis relasi; di luar adalah label.
# Teks dalam kutip dan isi {...} dibuang dulu, agar `{is_internal: true}`
# tidak membuat `true` dikira label.
POLA_RELASI = re.compile(r"-\s*\[[^\]]*?:\s*([A-Za-z_][A-Za-z0-9_]*)")
POLA_NAMA = re.compile(r":\s*([A-Za-z_][A-Za-z0-9_]*)")
POLA_KUTIP = re.compile(r"'[^']*'|\"[^\"]*\"")
POLA_KURUNG = re.compile(r"\{[^{}]*\}")
# Kunci di dalam {...} juga divalidasi: {name:'Devi'} sama salahnya dengan n.name.
POLA_KUNCI_MAP = re.compile(r"([A-Za-z_]\w*)\s*:")
# Kata kunci Cypher yang sah muncul sesudah ':' tapi bukan nama apa pun.
LITERAL = {"true", "false", "null"}
# `x.y`, bukan `f(...)` atau desimal. Awalan ditangkap untuk melewati namespace fungsi.
POLA_PROPERTI = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b(?!\s*\()")


def periksa_nama(cypher, sk=None):
    """Kembalikan (berat, ringan).

    BERAT  -- label atau jenis relasi asing. Daftarnya pasti (7 label,
              11 relasi), jadi kueri ditolak.
    RINGAN -- properti asing. Daftar properti tidak mungkin lengkap di basis
              data schema-flexible, jadi kueri tetap dijalankan; catatan ini
              hanya dipakai sebagai dugaan bila hasilnya kosong.
    """
    sk = sk or skema()
    label, relasi = set(sk["label"]), set(sk["relasi"])
    semua_prop = {p for daftar in sk["properti"].values() for p in daftar}

    tanpa_kutip = POLA_KUTIP.sub("''", cypher)
    kunci_map = set()
    for blok in POLA_KURUNG.findall(tanpa_kutip):
        kunci_map |= set(POLA_KUNCI_MAP.findall(blok))
    bersih = POLA_KURUNG.sub(" ", tanpa_kutip)

    dipakai_relasi = set(POLA_RELASI.findall(bersih)) - LITERAL
    dipakai_label = set(POLA_NAMA.findall(bersih)) - dipakai_relasi - LITERAL

    berat, ringan = [], []

    for n in sorted(dipakai_label - label):
        if n in relasi:
            berat.append(f"`{n}` itu jenis relasi, bukan label node "
                           f"-- tulis -[:{n}]- bukan (:{n})")
        else:
            berat.append(f"label `:{n}` tidak ada{_dekat(n, label)}")

    for n in sorted(dipakai_relasi - relasi):
        if n in label:
            berat.append(f"`{n}` itu label node, bukan jenis relasi "
                           f"-- tulis (:{n}) bukan -[:{n}]-")
        else:
            berat.append(f"relasi `:{n}` tidak ada{_dekat(n, relasi)}")

    dipakai_prop = {prop for awalan, prop in POLA_PROPERTI.findall(tanpa_kutip)
                    if awalan.lower() not in BUKAN_PROPERTI} | kunci_map
    for n in sorted(dipakai_prop - semua_prop):
        if _properti_ada(n):
            continue
        ringan.append(f"properti `.{n}` tidak ditemukan di data"
                      f"{_dekat(n, semua_prop)}")

    return berat, ringan


_cache_properti = {}


def _properti_ada(nama):
    """Adakah properti ini di NODE atau RELASI mana pun? Ditanyakan ke DB.

    Hanya untuk nama di luar daftar cepat, agar properti jarang seperti
    `ambigu` tidak salah dituduh.
    """
    if nama in _cache_properti:
        return _cache_properti[nama]
    ada = False
    for pola in (f"MATCH (n) WHERE n.`{nama}` IS NOT NULL RETURN count(n) AS n",
                 f"MATCH ()-[r]->() WHERE r.`{nama}` IS NOT NULL RETURN count(r) AS n"):
        try:
            if _kirim(pola, timeout=60)["result"][0]["n"] > 0:
                ada = True
                break
        except Exception:
            ada = True          # ragu-ragu: jangan menuduh
            break
    _cache_properti[nama] = ada
    return ada


def _dekat(nama, kandidat):
    m = difflib.get_close_matches(nama, sorted(kandidat), n=2, cutoff=0.6)
    return f" -- maksudnya `{'` atau `'.join(m)}`?" if m else ""


# --------------------------------------------------------------- tanya
# Relasi disimpan satu arah. Panah terbalik tidak menimbulkan galat, hanya 0
# baris: (Peneliti)-[:MEMILIKI]->(UnitKerja) kosong, tanpa arah 10 baris.
def _tanpa_arah(cypher):
    cypher = re.sub(r"<-\s*\[", "-[", cypher)
    return re.sub(r"\]\s*->", "]-", cypher)


# LLM kadang menulis kode unit atau institusi dengan huruf yang salah
# (`{kode:'pti'}` -> 0 baris). Nilainya sedikit dan tetap, jadi dibakukan
# program sebelum dijalankan. `toLower(u.kode) = 'pti'` tidak tersentuh karena
# polanya menuntut `kode` langsung diikuti `:` atau `=`.
POLA_KODE_UNIT = re.compile(r"(\bkode\s*(?::|=)\s*)(['\"])([^'\"]{1,20})\2")
POLA_NAMA_INSTITUSI = re.compile(r"(:Institusi\s*\{\s*nama\s*:\s*)(['\"])([^'\"]{1,40})\2")
_NILAI_BAKU = None


def _nilai_baku():
    global _NILAI_BAKU
    if _NILAI_BAKU is None:
        try:
            kode = [b["k"] for b in _kirim("MATCH (u:UnitKerja) RETURN u.kode AS k").get("result", [])]
            nama = [b["k"] for b in _kirim("MATCH (i:Institusi) RETURN i.nama AS k").get("result", [])]
            _NILAI_BAKU = ({k.lower(): k for k in kode if k}, {n.lower(): n for n in nama if n})
        except Exception:
            return ({}, {})                  # basis data mati: jangan dicache
    return _NILAI_BAKU


def betulkan_huruf(cypher):
    """`{kode:'pti'}` -> `{kode:'PTI'}`; `(:Institusi {nama:'brin'})` -> 'BRIN'."""
    kode, nama = _nilai_baku()

    def ganti(peta):
        def f(m):
            baku = peta.get(m.group(3).lower())
            return f"{m.group(1)}{m.group(2)}{baku}{m.group(2)}" if baku else m.group(0)
        return f
    cypher = POLA_KODE_UNIT.sub(ganti(kode), cypher)
    return POLA_NAMA_INSTITUSI.sub(ganti(nama), cypher)


def tanya(cypher, timeout=120, sk=None):
    """Jalankan Cypher dari LLM. Selalu mengembalikan dict, tidak melempar.

    status:
      nama_asing -- ditolak penjaga 1, TIDAK menyentuh basis data
      ditolak_db -- ArcadeDB menolak (mis. perintah menulis, sintaks salah)
      terpotong  -- jalan, tapi hasilnya dipangkas di 20.000 baris
      kosong     -- jalan, sah, memang tidak ada datanya
      ok         -- jalan, ada hasilnya
    """
    cypher = (cypher or "").strip().rstrip(";")
    if not cypher:
        return {"status": "ditolak_db", "cypher": cypher, "baris": [],
                "pesan": "Query kosong."}
    cypher = betulkan_huruf(cypher)

    if POLA_TULIS.search(cypher):
        return {"status": "ditolak_db", "cypher": cypher, "baris": [],
                "pesan": "Ditolak: query ini mengubah data. Chat hanya boleh "
                         "MATCH/RETURN -- basis data pun akan menolaknya."}

    berat, ringan = periksa_nama(cypher, sk)
    if berat:
        return {"status": "nama_asing", "cypher": cypher, "baris": [],
                "masalah": berat,
                "pesan": "Query TIDAK dijalankan. " + " ".join(berat)}

    try:
        d = _kirim(cypher, timeout=timeout)
    except requests.Timeout:
        return {"status": "ditolak_db", "cypher": cypher, "baris": [],
                "pesan": f"Query melebihi {timeout} detik dan dihentikan dari "
                         "sisi klien. CATATAN: ArcadeDB mungkin masih "
                         "mengerjakannya di latar belakang."}
    except Exception as e:
        return {"status": "ditolak_db", "cypher": cypher, "baris": [],
                "pesan": _rapikan_galat(str(e))}

    baris = d.get("result", [])
    if d.get("truncated"):
        return {"status": "terpotong", "cypher": cypher, "baris": baris,
                "pesan": f"Hasil dipotong ArcadeDB di {BATAS_ARCADEDB:,} baris. "
                         "Jumlah sebenarnya lebih besar -- persempit dgn WHERE "
                         "atau pakai count(). JANGAN pakai angka ini sbg total."}
    if not baris:
        if ringan:
            return {"status": "nama_asing", "cypher": cypher, "baris": [],
                    "masalah": ringan,
                    "pesan": "Hasilnya kosong, dan penyebabnya kemungkinan besar "
                             "ini: " + " ".join(ringan)}
        if "->" in cypher or "<-" in cypher:
            try:
                d2 = _kirim(_tanpa_arah(cypher), timeout=timeout)
            except Exception:
                d2 = {}
            if d2.get("result"):
                return {"status": "nama_asing", "cypher": cypher, "baris": [],
                        "masalah": ["arah relasi"],
                        "pesan": "Hasilnya kosong, tapi ADA hasil bila arah relasi "
                                 "diabaikan -- kemungkinan besar arah panah terbalik. "
                                 "Cocokkan arah tiap relasi dgn daftar RELASI, mis. "
                                 "(UnitKerja)-[:MEMILIKI]->(Peneliti)."}
        return {"status": "kosong", "cypher": cypher, "baris": [],
                "pesan": "Query sah dan berjalan, tapi tidak ada data yang cocok. "
                         "Label dan relasinya sudah diperiksa. Kalau mencari "
                         "teks, periksa juga ejaan dan huruf besar-kecilnya."}
    return {"status": "ok", "cypher": cypher, "baris": baris,
            "pesan": f"{len(baris):,} baris."}


def _rapikan_galat(teks):
    if "NotIdempotent" in teks:
        return ("Ditolak: query ini mengubah data. Endpoint yang dipakai "
                "baca-saja. Hanya MATCH/RETURN yang diizinkan.")
    m = re.search(r'"detail"\s*:\s*"([^"]{0,220})', teks)
    return f"Ditolak ArcadeDB: {m.group(1)}" if m else f"Ditolak ArcadeDB: {teks[:220]}"
