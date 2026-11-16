"""prompt_graf.py — Prompt sistem untuk penulisan Cypher oleh model bahasa.

Prompt berisi skema yang dibaca dari basis data, makna setiap relasi, dan
contoh kueri yang diambil dari VIEW.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.tanya_graf import skema  # noqa: E402

# Makna relasi
ARTI_RELASI = {
    "MENULIS":         "(Peneliti)->(Publikasi) — peneliti menulis publikasi. DARI DATA SUMBER.",
    "TERBIT_DI":       "(Publikasi)->(Venue) — terbit di jurnal/konferensi. DARI DATA SUMBER.",
    "MEMILIKI":        "(UnitKerja)->(Peneliti) — hanya 96 peneliti INTERNAL. DARI DATA SUMBER.",
    "BERAFILIASI":     "(Peneliti)->(Institusi) — HANYA rekan penulis EKSTERNAL "
                       "(is_internal false). DARI DATA SUMBER. Peneliti INTERNAL tidak "
                       "punya relasi ini: institusinya lewat "
                       "(Institusi)<-[:AFILIASI]-(UnitKerja)-[:MEMILIKI]->(Peneliti).",
    "AFILIASI":        "(UnitKerja)->(Institusi) — unit di bawah institusi (PTI/SI/TI "
                       "-> UB, PRSDI -> BRIN). Institusi dicari lewat `nama` ('UB', "
                       "'BRIN'). DARI DATA SUMBER.",
    "MEMBAHAS":        "(Publikasi)->(Topik) — TURUNAN, hasil BERTopic atas abstrak.",
    "MASUK_KATEGORI":  "(Publikasi)->(Kategori) — TURUNAN, pemetaan ASJC.",
    "TERMASUK_DALAM":  "(Topik)->(Kategori) — TURUNAN, topik ke kategori ASJC.",
    "BIDANG_KEAHLIAN": "(Peneliti)->(Topik) — TURUNAN. SANGAT PERMISIF: rata-rata "
                       "seorang peneliti menyentuh 20 kategori. Jangan dipakai untuk "
                       "menyimpulkan 'sedikit peneliti di bidang X'.",
    "KOLABORASI":      "(Peneliti)->(Peneliti) — TURUNAN. Pernah MENULIS PUBLIKASI "
                       "BERSAMA. Punya properti `jumlah` = jumlah publikasi bersama.",
    "SEBIDANG":        "(Peneliti)->(Peneliti) — TURUNAN. BERBAGI TOPIK, belum tentu "
                       "pernah menulis bersama. JANGAN tertukar dgn KOLABORASI: untuk "
                       "'siapa yang cocok diajak kerja sama' pakai SEBIDANG + "
                       "WHERE NOT (a)-[:KOLABORASI]-(b).",
}

# View yang dipakai sebagai contoh few-shot.
CONTOH_VIEW = ["individu", "kolaborasi_potensial", "pakar_topik",
               "jembatan_unit", "topik_naik_daun", "mirip_dengan"]

ATURAN = """ATURAN
1. Jawab HANYA dengan satu query Cypher. Tanpa penjelasan, tanpa blok kode.
2. Nama label/relasi/properti BERBAHASA INDONESIA persis seperti daftar di atas.
   Jangan menerjemahkan ke Inggris (`Peneliti` bukan `Researcher`, `nama` bukan
   `name`). Nama yang salah TIDAK menimbulkan galat, hanya 0 baris.
3. SELALU akhiri dgn LIMIT (maksimal 300). ArcadeDB memotong di 20.000 baris
   tanpa memberi tahu, dan hasil sebesar itu tidak bisa digambar.
4. Peneliti internal: `{is_internal: true}` (96 orang). Peneliti lain (3.546
   total) adalah rekan penulis eksternal.
5. Hanya MATCH/RETURN. CREATE/MERGE/SET/DELETE akan ditolak basis data.
6. Untuk menggambar graf, kembalikan NODE dan RELASI (`RETURN a, r, b`), bukan
   hanya kolom teks. Untuk menjawab angka, pakai count().
7. JANGAN mengembalikan `abstrak`. Pakai `judul` bila perlu menyebut publikasi.
   Chat ini bekerja atas GRAF, bukan atas teks mentahnya.
8. Sebelum `ORDER BY` sebuah angka, saring NULL-nya lebih dulu:
   `WHERE x IS NOT NULL`. ArcadeDB menaruh NULL di ATAS pada urutan DESC,
   jadi tanpa saringan itu pertanyaan "yang paling banyak X" justru
   mengembalikan baris yang nilainya kosong. Terukur: 112 Publikasi tidak
   punya `sitasi`.
9. Kata "peneliti" berarti peneliti INTERNAL — pakai `{is_internal: true}`,
   kecuali pertanyaannya jelas menyebut rekan penulis luar. Dari 3.546 node
   Peneliti hanya 96 yang internal; sisanya rekan penulis eksternal yang akan
   membanjiri hasil bila ikut terjaring.
10. Pencarian TEKS dari pertanyaan pengguna (nama peneliti, nama topik, judul,
   kode unit) harus TIDAK peka huruf besar-kecil. Tulis katanya dgn huruf
   kecil dan pakai `toLower(x) CONTAINS 'kata'`, atau `toLower(x) = 'nama
   lengkap'` untuk nama persis. `CONTAINS` dan `{nama:'...'}` PEKA huruf:
   'Learning' tidak cocok dgn 'learning', dan hasilnya 0 baris tanpa galat.
   Fungsinya `toLower()` -- `toLowerCase()` TIDAK ADA di ArcadeDB.
11. Untuk pertanyaan "pakar", "ahli", atau "siapa yang paling ..." di suatu
   bidang, URUTKAN dari bukti terkuat: `ORDER BY r.jumlah DESC` pada
   BIDANG_KEAHLIAN, atau jumlah publikasi. Daftar tanpa urutan menaruh
   peneliti berpublikasi 1 sejajar dgn yang berpublikasi 12."""

# Properti yang tidak dikirim ke LLM (ditegakkan di lib/llm.py).
DILARANG_KELUAR = {"abstrak", "abstract", "abstract_source"}


def _isi_contoh(cypher, contoh):
    """Ganti $param dgn nilai contoh, supaya few-shot berupa query lengkap."""
    for nama, nilai in sorted(contoh.items(), key=lambda x: -len(x[0])):
        teks = f"'{nilai}'" if isinstance(nilai, str) else str(nilai)
        cypher = re.sub(rf"\${nama}\b", teks, cypher)
    return cypher


def _ambil_contoh():
    from ekspor_visual import CONTOH_PARAM, VIEW
    bagian = []
    for nama in CONTOH_VIEW:
        v = VIEW.get(nama)
        if not v or not v.get("cypher"):
            continue
        bagian.append(f"# {v['judul']}\n{_isi_contoh(str(v['cypher']).strip(), CONTOH_PARAM)}")
    return bagian


def bangun_prompt(sk=None):
    """Prompt sistem lengkap: skema + makna + contoh + aturan."""
    sk = sk or skema()

    node = "\n".join(f"  (:{lab})  {n:,} node — properti: {', '.join(sk['properti'][lab])}"
                     for lab, n in sk["label"].items())
    rel = "\n".join(f"  [:{jenis}]  {n:,} — {ARTI_RELASI.get(jenis, '')}"
                    for jenis, n in sk["relasi"].items())

    return (f"Kamu menulis openCypher untuk graf penelitian SRE di ArcadeDB.\n\n"
            f"NODE\n{node}\n\nRELASI\n{rel}\n\n"
            f"CONTOH QUERY YANG SUDAH DIUJI\n" + "\n\n".join(_ambil_contoh()) +
            f"\n\n{ATURAN}\n")


if __name__ == "__main__":
    p = bangun_prompt()
    print(p)
    print(f"\n--- {len(p):,} karakter, ~{len(p)/3.5:,.0f} token ---", file=sys.stderr)
