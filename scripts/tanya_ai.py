#!/usr/bin/env python3
"""tanya_ai.py — Pertanyaan bahasa Indonesia -> Cypher -> graf.

Menyatukan tiga bagian:
    prompt_graf.py      menyusun prompt (skema dibaca dari DB + contoh view)
    lib/llm.py          memanggil LLM (penyedia bisa ditukar lewat .env)
    lib/tanya_graf.py   menjalankan Cypher dgn tiga penjaga

Tanpa kunci API:
    .venv/bin/python scripts/tanya_ai.py --cypher "MATCH (n:Peneliti) RETURN n LIMIT 5"
    .venv/bin/python scripts/tanya_ai.py --uji-penjaga
Dengan kunci API di .env:
    .venv/bin/python scripts/tanya_ai.py --tanya "siapa pakar machine learning di PTI?"

PERCOBAAN ULANG
Bila penjaga menolak nama asing (mis. `:Researcher`), pesannya dikirim balik
ke LLM beserta nama terdekat. Percobaan kedua hampir selalu benar.
"""
import argparse
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import llm                                   # noqa: E402
from lib.tanya_graf import skema, tanya               # noqa: E402
from prompt_graf import bangun_prompt                 # noqa: E402

MAKS_COBA = 3


# Kosong tidak selalu berarti "tidak ada". "machine learning" bukan label
# topik, padahal 35 peneliti menulis judul yang memuatnya. Bila pencarian teks
# di Topik kosong, kode melebarkannya ke judul (sekali saja).
POLA_CARI_TOPIK = re.compile(r":Topik\b.*CONTAINS", re.S | re.I)
SARAN_JUDUL = (
    "Query tadi sah tapi KOSONG: istilah itu bukan label topik. Cari lewat "
    "JUDUL publikasi: MATCH (p:Peneliti {is_internal: true})-[r:MENULIS]->"
    "(k:Publikasi) WHERE toLower(k.judul) CONTAINS '<istilah huruf kecil>' "
    "RETURN p, r, k LIMIT 300")


def jawab(pertanyaan, maks_coba=MAKS_COBA, ringkas=True, cetak=print):
    """Alur penuh. Mengembalikan dict berisi cypher, status, baris, kalimat."""
    sk = skema()
    prompt = bangun_prompt(sk)
    umpan, riwayat = None, []

    for percobaan in range(1, maks_coba + 1):
        cypher = llm.tulis_cypher(pertanyaan, prompt, umpan)
        hasil = tanya(cypher, sk=sk)
        riwayat.append({"percobaan": percobaan, "cypher": cypher,
                        "status": hasil["status"], "pesan": hasil["pesan"]})
        cetak(f"[percobaan {percobaan}] {hasil['status']}: {hasil['pesan'][:100]}")

        # Diulang hanya bila pesan galatnya memberi informasi baru: nama asing,
        # atau galat Cypher dari ArcadeDB (variabel, sintaks). Tidak diulang:
        # perintah menulis, timeout, basis data mati.
        galat_cypher = (hasil["status"] == "ditolak_db"
                        and hasil["pesan"].startswith("Ditolak ArcadeDB:")
                        and "Connection" not in hasil["pesan"])
        if hasil["status"] != "nama_asing" and not galat_cypher:
            break
        umpan = hasil["pesan"]

    lewat_judul = False
    if hasil["status"] == "kosong" and POLA_CARI_TOPIK.search(hasil["cypher"]):
        # Pelebaran juga boleh diulang sekali bila ArcadeDB menolak Cypher-nya.
        umpan_judul = SARAN_JUDUL
        for _ in range(2):
            cypher = llm.tulis_cypher(pertanyaan, prompt, umpan_judul, usaha="low")
            h2 = tanya(cypher, sk=sk)
            riwayat.append({"percobaan": len(riwayat) + 1, "cypher": cypher,
                            "status": h2["status"], "pesan": h2["pesan"],
                            "catatan": "pelebaran lewat judul"})
            cetak(f"[pelebaran judul] {h2['status']}: {h2['pesan'][:100]}")
            if not (h2["status"] in ("ditolak_db", "nama_asing")
                    and "Connection" not in h2["pesan"]):
                break
            umpan_judul = f"{SARAN_JUDUL}\nQuery sebelumnya ditolak: {h2['pesan']}"
        if h2["status"] in ("ok", "terpotong"):
            hasil, lewat_judul = h2, True

    hasil["riwayat"] = riwayat
    hasil["lewat_judul"] = lewat_judul
    hasil["kalimat"] = ""
    if ringkas and hasil["status"] in ("ok", "terpotong"):
        try:
            catatan = hasil["pesan"] if hasil["status"] == "terpotong" else ""
            # Kalimat menyebut bahwa jawabannya berasal dari judul, bukan topik.
            if lewat_judul:
                catatan += (" Tidak ada topik bernama itu; hasil ini dari "
                            "JUDUL publikasi yang memuat istilahnya.")
            hasil["kalimat"] = llm.susun_kalimat(pertanyaan, hasil["baris"], catatan,
                                                 cypher=hasil["cypher"])
        except Exception as e:                          # ringkasan itu hiasan
            hasil["kalimat"] = f"(gagal meringkas: {e})"
    return hasil


def uji_penjaga():
    """Uji ketiga penjaga tanpa menyentuh LLM sama sekali."""
    KASUS = [
        ("label Inggris",        "MATCH (n:Researcher) RETURN n LIMIT 5",                    "nama_asing"),
        ("relasi Inggris",       "MATCH ()-[r:COLLABORATES_WITH]->() RETURN r LIMIT 5",      "nama_asing"),
        ("properti Inggris",     "MATCH (n:Peneliti) WHERE n.name='Devi' RETURN n LIMIT 5",  "nama_asing"),
        ("label jadi relasi",    "MATCH (a)-[r:Peneliti]->(b) RETURN a LIMIT 5",             "nama_asing"),
        ("relasi jadi label",    "MATCH (n:KOLABORASI) RETURN n LIMIT 5",                    "nama_asing"),
        ("menulis (SET)",        "MATCH (n:Topik) SET n.x=1 RETURN n",                       "ditolak_db"),
        ("menulis (CREATE)",     "CREATE (x:Peneliti {nama:'palsu'}) RETURN x",              "ditolak_db"),
        ("sintaks salah",        "MATCH (n:Peneliti RETURN n",                               "ditolak_db"),
        ("kosong yang sah",      "MATCH (t:Topik) WHERE t.nama='zzz-tidak-ada' RETURN t",    "kosong"),
        ("terpotong 20.000",     "MATCH (a)-[r]->(b) RETURN id(a) AS a, id(b) AS b",         "terpotong"),
        ("benar",                "MATCH (p:Peneliti {is_internal:true})-[:MENULIS]->(k) "
                                 "RETURN p.nama AS n LIMIT 5",                               "ok"),
        ("pattern comprehension", "MATCH (p:Peneliti {local_id:'SIUB014'})-[:MENULIS]->(k) "
                                  "WITH k, size([(q:Peneliti)-[:MENULIS]->(k) | q]) AS n "
                                  "WHERE n = 1 RETURN count(k) AS c",             "ok"),
        ("arah relasi terbalik", "MATCH (p:Peneliti {local_id:'PRSDI016'})-[:MEMILIKI]->"
                                  "(u:UnitKerja) RETURN u LIMIT 5",           "nama_asing"),
        ("kode unit huruf kecil", "MATCH (u:UnitKerja {kode:'pti'})-[:MEMILIKI]->(p:Peneliti) "
                                  "RETURN p LIMIT 5",                          "ok"),
    ]
    sk = skema()
    gagal = 0
    for nama, cypher, harap in KASUS:
        h = tanya(cypher, sk=sk)
        ok = h["status"] == harap
        gagal += not ok
        print(f"  {'OK   ' if ok else 'GAGAL'} {nama:22} {h['status']:11} "
              f"(harap {harap})")
        if not ok:
            print(f"        -> {h['pesan'][:110]}")
    print(f"\n{len(KASUS)-gagal}/{len(KASUS)} lulus")
    return gagal


def main():
    a = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--tanya", help="pertanyaan bahasa Indonesia (butuh kunci API)")
    a.add_argument("--cypher", help="jalankan Cypher langsung, tanpa LLM")
    a.add_argument("--uji-penjaga", action="store_true", help="uji ketiga penjaga")
    a.add_argument("--prompt", action="store_true", help="cetak prompt sistem")
    a.add_argument("--tanpa-ringkas", action="store_true", help="lewati penyusunan kalimat")
    p = a.parse_args()

    if p.uji_penjaga:
        sys.exit(1 if uji_penjaga() else 0)

    if p.prompt:
        print(bangun_prompt())
        return

    if p.cypher:
        h = tanya(p.cypher)
        print(f"status : {h['status']}\npesan  : {h['pesan']}\nbaris  : {len(h['baris'])}")
        for b in h["baris"][:10]:
            if isinstance(b, dict):
                ringkas = {}
                for k, v in b.items():
                    if isinstance(v, dict):
                        v = {kk: vv for kk, vv in v.items()
                             if kk in ("nama", "judul", "local_id", "kode",
                                       "jumlah", "skor", "tahun", "sitasi")}
                    ringkas[k] = v
                print("  ", ringkas)
            else:
                print("  ", b)
        return

    if p.tanya:
        if not llm.siap():
            s = llm.setelan()
            print(f"{s['nama_kunci']} belum ada di .env.\n"
                  f"Panduan pengisiannya ada di README.md bagian 'Chat AI'.\n"
                  f"Sementara itu, semuanya bisa diuji tanpa kunci:\n"
                  f"  python scripts/tanya_ai.py --uji-penjaga\n"
                  f"  python scripts/tanya_ai.py --cypher \"MATCH ...\"")
            sys.exit(2)
        h = jawab(p.tanya, ringkas=not p.tanpa_ringkas)
        print(f"\n--- Cypher ---\n{h['cypher']}")
        print(f"\n--- Status --- {h['status']}: {h['pesan']}")
        if h.get("lewat_judul"):
            print("(tidak ada topik bernama itu -- dicari lewat judul publikasi)")
        if h["kalimat"]:
            print(f"\n--- Jawaban ---\n{h['kalimat']}")
        return

    a.print_help()


if __name__ == "__main__":
    main()
