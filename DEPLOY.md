# Menjalankan SRE di server

Panduan singkat untuk meng-host halaman SRE (`visual/sre.html`) dengan basis
data ArcadeDB online.

## Yang dibutuhkan

- Python 3.10 atau lebih baru (diuji dengan 3.13)
- Akses ke ArcadeDB yang sudah berisi graf SRE
- Kunci API LLM (Groq gratis: <https://console.groq.com/keys>) untuk fitur
  *Ask the graph* dan *Brainstorm*. Tanpa kunci, graf, view, pencarian, dan
  Compare tetap berjalan; hanya fitur AI yang mati.

Server tidak membutuhkan torch, BERTopic, atau model AI lokal. Vektor abstrak
sudah dihitung dan ikut di repo (`data/vektor_abstrak.npy`).

## Pemasangan

```bash
git clone <alamat-repo> sre && cd sre
python3 -m venv .venv
.venv/bin/pip install -r requirements_server.txt
cp .env.example .env
```

Isi `.env`:

```ini
ARCADEDB_HOST=https://arcade.riset.web.id
ARCADEDB_DB=sre
ARCADEDB_USER=root
ARCADEDB_PASSWORD=<sandi ArcadeDB>

GROQ_API_KEY=<kunci Groq>
LLM_PENYEDIA=groq

SRE_HOST=0.0.0.0
SRE_PORT=8000
```

## Menjalankan

```bash
cd visual && ../.venv/bin/python server.py
```

Baris pertama yang tercetak menunjukkan alamat ArcadeDB yang dipakai. Buka
`http://<server>:8000/sre.html`. Untuk dijalankan terus, bungkus perintah di
atas dengan systemd, pm2, atau `screen`, lalu pasang reverse proxy (nginx atau
Cloudflare) bila perlu HTTPS.

Pemeriksaan cepat setelah jalan:

```bash
.venv/bin/python scripts/uji_paket_ab.py --port 8000          # 16 uji, tanpa LLM
.venv/bin/python scripts/uji_endpoint_brainstorm.py 8000      # 23 uji, tanpa LLM
```

## Keamanan

- Semua kueri dari halaman memakai endpoint **baca-saja** ArcadeDB
  (`/api/v1/query/`). Perintah `CREATE`, `SET`, atau `DELETE` yang diketik di
  kotak Cypher ditolak oleh ArcadeDB.
- Yang tetap terbuka bagi siapa pun yang membuka halaman adalah kuota LLM
  (`/api/tanya`, `/api/brainstorm`). Kuota Groq gratis 8.000 token/menit,
  cukup untuk sekitar empat giliran Brainstorm per menit.
- Akun di `.env` sebaiknya bukan root. Server hanya membutuhkan hak baca.
- `.env` tidak ikut di repo. Jangan menaruh sandi atau kunci di berkas lain.

## Catatan

- Percakapan Brainstorm hanya disimpan di memori server (maks 50 sesi,
  kedaluwarsa 2 jam). Merestart server menghapus semuanya.
- Satu profil peneliti internal masih memuat publikasi orang lain dan sedang
  diverifikasi.
- Gambaran sistem, ontologi, dan keterbatasan data ada di `README.md`.
