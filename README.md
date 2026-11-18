# SRE — Smart Research Environment

SRE adalah graf pengetahuan penelitian untuk pusat riset PRSDI (BRIN) dan tiga
program studi Universitas Brawijaya (PTI, SI, TI). Sistem ini memiliki dua
fungsi inti:

1. **Penemuan topik**: mengelompokkan abstrak publikasi menjadi topik tanpa
   pelabelan manual, lalu memetakan setiap topik ke klasifikasi bidang ASJC.
2. **Pemadanan profil (*match profiling*)**: menemukan peneliti yang berbagi
   bidang, termasuk pasangan yang belum pernah menulis bersama.

Repositori ini memuat komponen yang diperlukan untuk menjalankan sistem
(server, antarmuka web, dan asisten model bahasa). Graf disimpan di ArcadeDB
yang alamatnya ditentukan melalui berkas `.env`.

## 1. Data

Keadaan graf yang digunakan (dihitung dari basis data, 12 Oktober 2026):

| Komponen | Jumlah |
|---|---:|
| Node | 14.852 |
| Relasi | 65.913 |
| Peneliti internal (PRSDI 58, SI 15, PTI 12, TI 11) | 96 |
| Peneliti eksternal (rekan penulis) | 3.451 |
| Publikasi (seluruhnya memiliki abstrak) | 6.607 |
| Topik | 81 |
| Kategori (27 ASJC + 45 subkategori ASJC-4) | 72 |

## 2. Arsitektur

```
ArcadeDB (openCypher)
      │  endpoint baca-saja /api/v1/query
      ▼
visual/server.py ──── API model bahasa (Groq atau penyedia lain berformat OpenAI)
      │  HTTP + JSON
      ▼
visual/sre.html (Cytoscape.js)
```

Server berperan sebagai perantara karena ArcadeDB tidak mengirim header CORS.
Seluruh kueri dari server melewati endpoint baca-saja, sehingga perintah
`CREATE`, `SET`, dan `DELETE` ditolak oleh basis data.

## 3. Model data

Ontologi terdiri atas tujuh tipe node: `Institusi`, `UnitKerja`, `Peneliti`,
`Publikasi`, `Venue`, `Topik`, dan `Kategori`. Nama label, relasi, dan
properti menggunakan bahasa Indonesia; antarmuka menampilkan padanan bahasa
Inggris.

| Relasi | Arah | Keluarga | Asal |
|---|---|---|---|
| `AFILIASI` | UnitKerja → Institusi | struktur | data sumber |
| `MEMILIKI` | UnitKerja → Peneliti | struktur | data sumber |
| `BERAFILIASI` | Peneliti → Institusi | struktur | data sumber |
| `MENULIS` | Peneliti → Publikasi | kepengarangan | data sumber |
| `TERBIT_DI` | Publikasi → Venue | kepengarangan | data sumber |
| `MEMBAHAS` | Publikasi → Topik | semantik | BERTopic |
| `MASUK_KATEGORI` | Publikasi → Kategori | semantik | pemetaan ASJC |
| `TERMASUK_DALAM` | Topik → Kategori | semantik | pemetaan ASJC |
| `BIDANG_KEAHLIAN` | Peneliti → Topik | semantik | turunan |
| `KOLABORASI` | Peneliti — Peneliti | kemiripan | turunan |
| `SEBIDANG` | Peneliti — Peneliti | kemiripan | turunan |

## 4. Metode

### 4.1 Penemuan topik

Topik dibentuk dengan BERTopic dari gabungan judul dan abstrak. Vektor dokumen
dihasilkan oleh `paraphrase-multilingual-MiniLM-L12-v2` karena korpus
berbahasa Indonesia dan Inggris. Reduksi dimensi memakai UMAP (15 tetangga,
5 dimensi, `random_state` 42), pengelompokan memakai HDBSCAN (ukuran topik
minimum 15), dan label topik dibentuk dengan c-TF-IDF serta KeyBERTInspired.

Setiap topik dipetakan ke kategori ASJC berdasarkan kemiripan kosinus antara
vektor kata kunci topik dan vektor deskripsi kategori. Skor pemetaan disimpan
pada relasi `TERMASUK_DALAM` dan ditampilkan di antarmuka.

### 4.2 Lapisan turunan

- `KOLABORASI`: pasangan penulis pada publikasi yang sama, dengan properti
  jumlah publikasi bersama. Publikasi dengan lebih dari 20 penulis dikeluarkan.
- `BIDANG_KEAHLIAN`: jumlah publikasi seorang peneliti pada setiap topik.
- `SEBIDANG`: kemiripan profil topik dua peneliti. Bobot topik *t* untuk
  peneliti *p* adalah

  w(p,t) = log(1 + n(p,t)) · log(N / df(t))

  dengan *n(p,t)* jumlah publikasi *p* pada topik *t*, *N* jumlah peneliti,
  dan *df(t)* jumlah peneliti yang memiliki topik *t*. Vektor dinormalisasi L2,
  dan pasangan dengan kosinus ≥ 0,30 dihubungkan.

### 4.3 Kemiripan abstrak

Model vektor hanya membaca 128 token, sehingga abstrak dipotong per 90 kata
dan vektor potongan dirata-rata. Kemiripan dihitung dengan kosinus antara
vektor yang dipusatkan terhadap rata-rata korpus μ:

  sim(a,b) = ((a − μ)·(b − μ)) / (‖a − μ‖ ‖b − μ‖)

Ambang ditentukan dari 200.000 pasangan publikasi acak: *moderate* = persentil
ke-99 (0,39) dan *strong* = persentil ke-99,9 (0,57). Vektor yang telah
dihitung disimpan di `data/vektor_abstrak.npy`.

### 4.4 Tingkat bukti pada perbandingan

| Tingkat | Kriteria |
|---|---|
| *Direct evidence* | pernah menulis bersama, `SEBIDANG`, atau topik yang sama dengan ≥ 2 publikasi di setiap sisi |
| *Via a bridge* | publikasi mirip, peneliti perantara, atau jalur melalui rekan penulis |
| *Exploratory* | tidak ada bukti di graf |

Skor peneliti perantara antara himpunan topik *A* dan *C* adalah
|A ∩ C| / √(|A|·|C|), diambil dari sisi yang lebih lemah.

### 4.5 Asisten model bahasa

**Tanya-graf.** Pertanyaan diterjemahkan menjadi Cypher oleh model bahasa.
Prompt disusun dari skema yang dibaca dari basis data, makna setiap relasi,
dan contoh kueri yang telah diuji. Kueri diperiksa oleh tiga penjaga:

| Penjaga | Kondisi | Tindakan |
|---|---|---|
| nama asing | label, relasi, atau properti tidak ada di skema | ditolak sebelum dijalankan |
| terpotong | hasil mencapai 20.000 baris | pengguna diberi tahu |
| kosong | kueri sah tanpa hasil | dilaporkan sebagai status tersendiri |

Kueri Cypher selalu ditampilkan. Peringkat dihitung oleh program, dan abstrak
tidak dikirim ke model.

**Brainstorm.** Percakapan beberapa giliran di atas bukti perbandingan yang
dihitung program. Setiap peneliti, publikasi, dan topik dalam bukti diberi kode
`[R1]`, `[P1]`, `[T1]`, dan model wajib mengutipnya. Maksimal delapan abstrak
yang dipilih oleh graf dikirim ke model. Sesi disimpan di memori server
(maksimal 50 sesi, kedaluwarsa setelah 2 jam).

## 5. Struktur repositori

| Berkas | Fungsi |
|---|---|
| `visual/server.py` | server HTTP: halaman, API graf, tanya-graf, perbandingan, Brainstorm |
| `visual/sre.html` | antarmuka (Cytoscape.js) |
| `visual/data_cy.json` | graf awal sebelum view pertama dimuat |
| `scripts/ekspor_visual.py` | 26 view tersimpan dan aturan gaya tampilan |
| `scripts/tanya_ai.py`, `scripts/prompt_graf.py` | tanya-graf |
| `scripts/banding.py`, `banding_publikasi.py`, `banding_organisasi.py` | bukti perbandingan peneliti, publikasi, dan organisasi |
| `scripts/ringkas_mapping.py` | ringkasan graf yang sedang tampil |
| `scripts/brainstorm.py` | sesi dan giliran Brainstorm |
| `scripts/lib/arcadedb.py` | konfigurasi koneksi ArcadeDB |
| `scripts/lib/tanya_graf.py` | eksekusi Cypher dengan tiga penjaga |
| `scripts/lib/llm.py` | pemanggil model bahasa |
| `scripts/uji_paket_ab.py`, `scripts/uji_endpoint_brainstorm.py` | uji endpoint |
| `data/vektor_abstrak.npy`, `.json` | vektor abstrak dan indeksnya |

Pipeline pembangun graf (penarikan data, BERTopic, pemetaan ASJC, dan lapisan
turunan) tidak termasuk repositori ini.

## 6. Instalasi

Prasyarat: Python 3.10 atau lebih baru dan akses ke ArcadeDB yang berisi graf
SRE.

```bash
git clone https://github.com/stephanimss/sre.git
cd sre
python3 -m venv .venv
.venv/bin/pip install -r requirements_server.txt
cp .env.example .env
```

Variabel di `.env`:

| Variabel | Nilai bawaan | Keterangan |
|---|---|---|
| `ARCADEDB_HOST` | `http://localhost:2480` | alamat server ArcadeDB |
| `ARCADEDB_DB` | `sre_test` | nama basis data |
| `ARCADEDB_USER`, `ARCADEDB_PASSWORD` | `root`, — | akun ArcadeDB (cukup hak baca) |
| `LLM_PENYEDIA` | `groq` | `groq`, `openai`, `xai`, `openrouter`, atau `cerebras` |
| `LLM_API_KEY` | — | kunci API penyedia; tanpa ini tanya-graf dan Brainstorm nonaktif |
| `LLM_MODEL` | bawaan penyedia | nama model, misalnya `gpt-4o-mini` |
| `LLM_URL` | — | opsional, alamat API lain yang berformat OpenAI |
| `SRE_HOST` | `127.0.0.1` | `0.0.0.0` agar server dapat diakses dari jaringan |
| `SRE_PORT` | `8000` | port server |

## 7. Penggunaan

```bash
cd visual
../.venv/bin/python server.py
```

Halaman tersedia di `http://<host>:<port>/sre.html`. Antarmuka terdiri atas
tiga panel:

- **Brainstorm** (kiri): pemilihan 2–3 peneliti, 2–3 publikasi, atau dua
  organisasi; bukti perbandingan; percakapan.
- **Kanvas** (tengah): graf, pencarian judul dan nama di basis data, navigasi.
- **Explore** (kanan): tanya-graf, kotak Cypher beserta penjelasan pola, 26 view
  tersimpan, detail node, legenda, dan pewarnaan menurut kategori ASJC.

Panduan pemasangan di server ada di [DEPLOY.md](DEPLOY.md).

## 8. Pengujian

```bash
.venv/bin/python scripts/ekspor_visual.py --periksa-view    # menjalankan 26 view
.venv/bin/python scripts/tanya_ai.py --uji-penjaga          # 14 kasus penjaga
.venv/bin/python scripts/uji_paket_ab.py --port 8000        # 16 uji endpoint
.venv/bin/python scripts/uji_endpoint_brainstorm.py 8000    # 23 uji endpoint
```

Dua perintah terakhir memerlukan server yang sedang berjalan. Tidak ada uji
yang memanggil model bahasa.

## 9. Keterbatasan

- Skor pemetaan topik ke ASJC rata-rata 0,46 dan belum divalidasi oleh pakar.
- Nama topik bercampur bahasa Indonesia dan Inggris sesuai bahasa abstraknya.
- Publikasi peneliti bernama umum dapat tercampur dengan karya orang lain;
  satu profil masih dalam verifikasi.
- Sekitar 7% DOI tersimpan menunjuk karya lain, dan 91,5% node `Venue` berupa
  string sitasi, sehingga data sitasi dan kuartil jurnal belum layak dipakai
  untuk klaim.
- Tingkat *Via a bridge* masih longgar: hampir semua pasangan lintas institusi
  memperolehnya karena median 19 topik per peneliti.
- Jawaban model bahasa tidak sepenuhnya deterministik meskipun temperatur 0.
