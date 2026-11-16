# SRE — Smart Research Environment

Graf pengetahuan penelitian untuk **BRIN/PRSDI** dan tiga program studi
**Universitas Brawijaya** (PTI, SI, TI). Graf ini menjawab: siapa meneliti apa,
bersama siapa, dan di bidang ilmu mana.

Repo ini berisi semua yang dibutuhkan untuk **menjalankan** SRE: server,
antarmuka web, chat tanya-graf, Compare, dan Brainstorm. Grafnya sendiri
disimpan di ArcadeDB yang alamatnya diatur lewat `.env`. Cara memasang ada di
**[DEPLOY.md](DEPLOY.md)**.

| | |
|---|---|
| Node / relasi | 14.852 / 65.913 |
| Peneliti internal | 96 (PRSDI 58, SI 15, PTI 12, TI 11) |
| Publikasi | 6.607, seluruhnya berabstrak |
| Topik | 81 (BERTopic), dipetakan ke ASJC |
| View tersimpan | 26 |

## Dua fungsi inti

1. **Menemukan topik dari abstrak.** Abstrak dikelompokkan menjadi topik dengan
   BERTopic, lalu setiap topik dipetakan ke kategori ASJC.
2. **Match profiling.** Menemukan peneliti yang berbagi bidang, termasuk yang
   belum pernah menulis bersama.

## Ontologi

**7 tipe node:** Institusi, UnitKerja, Peneliti, Publikasi, Venue, Topik, Kategori.

| Keluarga | Relasi |
|---|---|
| struktur | `AFILIASI` `MEMILIKI` `BERAFILIASI` |
| kepengarangan | `MENULIS` `TERBIT_DI` |
| semantik | `MEMBAHAS` `MASUK_KATEGORI` `TERMASUK_DALAM` `BIDANG_KEAHLIAN` |
| kemiripan | `KOLABORASI` `SEBIDANG` |

Label, relasi, dan properti di basis data berbahasa Indonesia; antarmuka
berbahasa Inggris.

## Isi repo

    visual/server.py          server: halaman, API graf, chat, Compare, Brainstorm
    visual/sre.html           antarmuka (Cytoscape.js)
    visual/data_cy.json       graf awal sebelum view pertama dimuat
    scripts/ekspor_visual.py  26 view tersimpan dan kamus gaya
    scripts/tanya_ai.py       tanya-graf: pertanyaan -> Cypher -> jawaban
    scripts/prompt_graf.py    prompt tanya-graf (skema dibaca dari basis data)
    scripts/banding*.py       bukti Compare: peneliti, publikasi, organisasi
    scripts/ringkas_mapping.py  ringkasan graf untuk Brainstorm
    scripts/brainstorm.py     percakapan Brainstorm
    scripts/lib/              koneksi ArcadeDB, penjaga Cypher, pemanggil LLM
    scripts/uji_*.py          uji endpoint setelah server jalan
    data/vektor_abstrak.*     vektor abstrak untuk kemiripan publikasi

Pipeline pembangun graf (penarikan data, BERTopic, pemetaan ASJC) dan data
mentah tidak termasuk repo ini.

## Menjalankan singkat

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_server.txt
cp .env.example .env              # isi alamat ArcadeDB dan kunci LLM
cd visual && ../.venv/bin/python server.py
```

Buka `http://localhost:8000/sre.html`. Rincian dan catatan keamanan ada di
[DEPLOY.md](DEPLOY.md).

## Fitur

- **Explore** (panel kanan): 26 view tersimpan, kotak Cypher dengan penjelasan
  pola, detail node, legenda yang bisa menyaring, mode warna ASJC.
- **Ask the graph**: pertanyaan bahasa Indonesia atau Inggris diterjemahkan
  LLM menjadi Cypher. Cypher selalu ditampilkan. Tiga penjaga menolak label
  asing, menandai hasil terpotong, dan melaporkan hasil kosong. Abstrak tidak
  dikirim ke LLM.
- **Compare** (panel kiri): bukti kedekatan 2–3 peneliti, 2–3 publikasi, atau
  2 organisasi, dihitung program dari graf, dengan tingkat bukti *Direct
  evidence*, *Via a bridge*, atau *Exploratory*.
- **Brainstorm**: percakapan banyak giliran di atas bukti Compare. LLM wajib
  mengutip kode bukti ([R1], [P1], [T1]); kode yang tidak ada ditandai merah.
  Sesi hanya di memori server.
- Semua kueri server memakai endpoint **baca-saja** ArcadeDB.

## Pemeriksaan

```bash
.venv/bin/python scripts/ekspor_visual.py --periksa-view     # 26 view dijalankan
.venv/bin/python scripts/tanya_ai.py --uji-penjaga           # 14 kasus penjaga
.venv/bin/python scripts/uji_paket_ab.py --port 8000         # server harus jalan
.venv/bin/python scripts/uji_endpoint_brainstorm.py 8000     # server harus jalan
```

## Keterbatasan data

- Skor pemetaan topik → ASJC rata-rata 0,46 dan belum divalidasi pakar. Skornya
  ditampilkan supaya tidak dibaca sebagai kepastian.
- Nama topik bercampur bahasa Indonesia dan Inggris, mengikuti abstraknya.
- Profil peneliti bernama umum bisa tercampur karya orang lain. Sebagian sudah
  dibersihkan; satu profil masih dalam verifikasi.
- Sekitar 7% DOI tersimpan menunjuk karya lain, dan 91,5% node Venue berupa
  string sitasi. Cacah sitasi dan kuartil jurnal belum layak untuk klaim.
- Tingkat *Via a bridge* masih longgar: pasangan lintas institusi hampir
  selalu mendapat tingkat ini.
