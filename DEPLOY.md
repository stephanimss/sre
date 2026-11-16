# Pemasangan SRE di server

Dokumen ini menjelaskan pemasangan server SRE yang terhubung ke ArcadeDB
jarak jauh. Uraian sistem ada di [README.md](README.md).

## 1. Prasyarat

- Python 3.10 atau lebih baru (diuji dengan 3.13).
- ArcadeDB yang berisi graf SRE, beserta akun dengan hak baca.
- Kunci API model bahasa (misalnya Groq: <https://console.groq.com/keys>).
  Tanpa kunci, graf, view, pencarian, dan perbandingan tetap berfungsi;
  tanya-graf dan Brainstorm nonaktif.

Server tidak memerlukan torch, BERTopic, atau model vektor lokal karena vektor
abstrak telah dihitung sebelumnya (`data/vektor_abstrak.npy`).

## 2. Pemasangan

```bash
git clone https://github.com/stephanimss/sre.git
cd sre
python3 -m venv .venv
.venv/bin/pip install -r requirements_server.txt
cp .env.example .env
```

Contoh isi `.env`:

```ini
ARCADEDB_HOST=https://arcade.riset.web.id
ARCADEDB_DB=sre
ARCADEDB_USER=<akun ArcadeDB>
ARCADEDB_PASSWORD=<sandi ArcadeDB>

GROQ_API_KEY=<kunci Groq>
LLM_PENYEDIA=groq

SRE_HOST=0.0.0.0
SRE_PORT=8000
```

## 3. Menjalankan

```bash
cd visual
../.venv/bin/python server.py
```

Baris kedua keluaran menunjukkan alamat ArcadeDB yang digunakan. Halaman
tersedia di `http://<server>:8000/sre.html`. Untuk layanan yang berjalan terus,
perintah di atas dapat dijalankan melalui systemd, pm2, atau `screen`, dengan
reverse proxy (nginx atau Cloudflare) untuk HTTPS.

## 4. Verifikasi

```bash
.venv/bin/python scripts/uji_paket_ab.py --port 8000        # 16 uji endpoint
.venv/bin/python scripts/uji_endpoint_brainstorm.py 8000    # 23 uji endpoint
```

Kedua uji tidak memanggil model bahasa sehingga tidak memakai kuota.

## 5. Keamanan

- Seluruh kueri server memakai endpoint baca-saja ArcadeDB
  (`/api/v1/query/`). Perintah penulisan yang diketik di kotak Cypher ditolak
  oleh basis data.
- Endpoint `/api/tanya` dan `/api/brainstorm` memakai kuota model bahasa milik
  pemilik kunci. Kuota Groq gratis adalah 8.000 token per menit, setara
  sekitar empat giliran Brainstorm per menit.
- Akun di `.env` sebaiknya bukan root karena server hanya membutuhkan hak baca.
- Berkas `.env` tidak disertakan di repositori.

## 6. Catatan operasional

- Sesi Brainstorm disimpan di memori server (maksimal 50 sesi, kedaluwarsa
  setelah 2 jam) dan hilang saat server dijalankan ulang.
- Perubahan isi basis data memerlukan server dijalankan ulang karena skema
  untuk tanya-graf disimpan di memori.
