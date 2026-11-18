# Pemasangan SRE di server

Dokumen ini menjelaskan pemasangan server SRE yang terhubung ke ArcadeDB
jarak jauh. Uraian sistem ada di [README.md](README.md).

## 1. Prasyarat

- Python 3.10 atau lebih baru (diuji dengan 3.13).
- ArcadeDB yang berisi graf SRE, beserta akun dengan hak baca.
- Kunci API model bahasa dari salah satu penyedia: Groq, OpenAI, xAI,
  OpenRouter, Cerebras, atau layanan lain yang berformat OpenAI.
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

LLM_PENYEDIA=openai
LLM_API_KEY=<kunci API penyedia>
LLM_MODEL=

SRE_HOST=0.0.0.0
SRE_PORT=8000
```

`LLM_PENYEDIA` dapat diisi `groq`, `openai`, `xai`, `openrouter`, atau
`cerebras`. `LLM_MODEL` yang kosong berarti model bawaan penyedia (misalnya
`gpt-4o-mini` untuk OpenAI). Layanan lain yang berformat OpenAI dapat dipakai
dengan mengisi `LLM_URL` dan `LLM_MODEL`.

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
  pemilik kunci. Satu giliran Brainstorm memerlukan sekitar 3.000 token
  masukan; pada Groq versi gratis (8.000 token per menit) setara sekitar
  empat giliran per menit.
- Akun di `.env` sebaiknya bukan root karena server hanya membutuhkan hak baca.
- Berkas `.env` tidak disertakan di repositori.

## 6. Catatan operasional

- Sesi Brainstorm disimpan di memori server (maksimal 50 sesi, kedaluwarsa
  setelah 2 jam) dan hilang saat server dijalankan ulang.
- Perubahan isi basis data memerlukan server dijalankan ulang karena skema
  untuk tanya-graf disimpan di memori.
