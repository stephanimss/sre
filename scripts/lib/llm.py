"""lib/llm.py — Lapisan LLM yang bisa ditukar penyedianya.

Tanpa pustaka `openai`: cukup satu POST JSON berbentuk /chat/completions,
yang didukung Groq, OpenAI, xAI, OpenRouter, dan Cerebras. Penyedia dan model
diganti lewat .env tanpa menyentuh kode (xAI pernah mempensiunkan delapan
model sekaligus).

Yang dikirim ke penyedia: pertanyaan + prompt skema (~1.740 token). Saat
menyusun kalimat, baris hasil kueri ikut dikirim setelah lewat
`buang_teks_panjang()`, yang membuang `abstrak` dan memotong teks panjang.
Kode inilah yang menegakkan aturan itu, bukan prompt.
"""
import json
import os
import re
import time

import requests

from collections import Counter
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:                                   # dotenv opsional
    pass

# Semua penyedia di bawah memakai bentuk /chat/completions gaya OpenAI.
PENYEDIA = {
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY",      "openai/gpt-oss-120b"),
    "openai":     ("https://api.openai.com/v1",      "OPENAI_API_KEY",    "gpt-4o-mini"),
    "xai":        ("https://api.x.ai/v1",            "XAI_API_KEY",       "grok-4.3"),
    "openrouter": ("https://openrouter.ai/api/v1",   "OPENROUTER_API_KEY","openai/gpt-oss-120b"),
    "cerebras":   ("https://api.cerebras.ai/v1",     "CEREBRAS_API_KEY",  "gpt-oss-120b"),
}

# Properti yang tidak boleh ikut kembali ke LLM. Lihat prompt_graf.DILARANG_KELUAR.
DILARANG = {"abstrak", "abstract", "abstract_source"}
BATAS_TEKS = 200        # potong string panjang (judul dsb) sebelum dikirim
BATAS_BARIS = 40        # jumlah baris maksimal yang diringkas


class LLMBelumDisetel(Exception):
    """Kunci API belum ada di .env."""


def setelan():
    """Baca penyedia & model dari .env. Ganti penyedia = ganti satu baris."""
    nama = ((os.getenv("LLM_PENYEDIA") or "").strip() or "groq").lower()
    if nama not in PENYEDIA:
        raise LLMBelumDisetel(
            f"LLM_PENYEDIA='{nama}' tidak dikenal. Pilih: {', '.join(PENYEDIA)}")
    url, nama_kunci, model_bawaan = PENYEDIA[nama]
    return {"penyedia": nama, "url": url, "nama_kunci": nama_kunci,
            "kunci": os.getenv(nama_kunci, "").strip(),
            "model": (os.getenv("LLM_MODEL") or "").strip() or model_bawaan}  # kosong = bawaan


def siap():
    """True bila kunci API sudah ada. Dipakai UI untuk menyembunyikan chat."""
    try:
        return bool(setelan()["kunci"])
    except LLMBelumDisetel:
        return False


def _panggil(pesan, suhu=0.0, maks=400, timeout=60, usaha=None):
    s = setelan()
    if not s["kunci"]:
        raise LLMBelumDisetel(
            f"{s['nama_kunci']} belum ada di .env. Lihat panduan di README.md.")
    body = {"model": s["model"], "messages": pesan,
            "temperature": suhu, "max_tokens": maks}
    # Token berpikir gpt-oss ikut dihitung dalam max_tokens; `reasoning_effort`
    # menyingkatnya. Hanya dikirim ke Groq (penyedia lain belum diuji).
    if usaha and s["penyedia"] == "groq":
        body["reasoning_effort"] = usaha
    for coba in (1, 2):
        r = requests.post(
            f"{s['url']}/chat/completions", timeout=timeout,
            headers={"Authorization": f"Bearer {s['kunci']}",
                     "Content-Type": "application/json"},
            json=body)
        if r.status_code != 429 or coba == 2:
            break
        # Kuota Groq gratis 8.000 token/menit. Tunggu sekali sesuai saran
        # server, paling lama 15 detik.
        m = re.search(r"try again in ([\d.]+)s", r.text)
        tunggu = float(r.headers.get("retry-after") or (m.group(1) if m else 5))
        time.sleep(min(tunggu + 0.5, 15))
    if r.status_code == 429:
        raise RuntimeError("Kuota Groq per menit habis (tier gratis: 8.000 "
                           "token/menit). Tunggu sebentar, lalu tanya lagi.")
    if r.status_code >= 400:
        raise RuntimeError(f"{s['penyedia']} {r.status_code}: {r.text[:300]}")
    pilihan = r.json()["choices"][0]
    isi = (pilihan["message"].get("content") or "").strip()
    # Token bisa habis untuk berpikir sehingga jawabannya kosong; tangkap di
    # sini, jangan biarkan muncul di hilir sebagai "Query kosong."
    if not isi and pilihan.get("finish_reason") == "length":
        raise RuntimeError(f"LLM kehabisan jatah {maks} token sebelum sempat "
                           "menjawab (habis untuk berpikir). Naikkan `maks`.")
    return isi

# ------------------------------------------------------ menulis Cypher

POLA_PAGAR = re.compile(r"```(?:cypher|sql)?\s*(.*?)```", re.S | re.I)


def tulis_cypher(pertanyaan, prompt_sistem, umpan_balik=None, usaha=None):
    """Minta LLM menulis Cypher. `umpan_balik` = pesan penjaga saat mencoba lagi."""
    isi = pertanyaan if not umpan_balik else (
        f"{pertanyaan}\n\nCatatan dari percobaan sebelumnya: {umpan_balik}\n"
        "Tulis ulang querynya saja.")
    jawab = _panggil([{"role": "system", "content": prompt_sistem},
                      {"role": "user", "content": isi}], maks=2000, usaha=usaha)
    return bersihkan_cypher(jawab)


def bersihkan_cypher(teks):
    """Buang pagar kode dan basa-basi; LLM sering membungkus dgn ```cypher."""
    m = POLA_PAGAR.search(teks)
    if m:
        teks = m.group(1)
    baris = [b for b in teks.strip().splitlines()
             if not b.strip().startswith(("Berikut", "Here", "Query:"))]
    return "\n".join(baris).strip().rstrip(";")


# ----------------------------------------------------- menyusun kalimat

def buang_teks_panjang(baris):
    """Bersihkan baris hasil SEBELUM dikirim ke LLM.

    Membuang properti terlarang (abstrak) dan memotong teks panjang, agar chat
    tetap berbasis graf, bukan data mentah.
    """
    bersih = []
    for b in baris[:BATAS_BARIS]:
        if not isinstance(b, dict):
            bersih.append(b)
            continue
        item = {}
        for k, v in b.items():
            if k.startswith("@") or k.lower() in DILARANG:
                continue
            if isinstance(v, dict):
                v = {kk: vv for kk, vv in v.items()
                     if not kk.startswith("@") and kk.lower() not in DILARANG}
                v = {kk: (vv[:BATAS_TEKS] if isinstance(vv, str) else vv)
                     for kk, vv in v.items()}
            elif isinstance(v, str):
                v = v[:BATAS_TEKS]
            item[k] = v
        bersih.append(item)
    return bersih

BATAS_QUERY = 300           # LIMIT maksimal menurut aturan 3 prompt
ANGKA_RELASI = ("jumlah", "skor")   # KOLABORASI/BIDANG_KEAHLIAN: jumlah; SEBIDANG: skor


def _node_baris(b):
    """Node di sebuah baris. ArcadeDB MERATAKAN baris ber-kolom tunggal:
    `RETURN p` menghasilkan node itu sendiri, bukan {"p": node}."""
    if not isinstance(b, dict):
        return []
    if b.get("@cat") == "v":
        return [b]
    return [v for v in b.values() if isinstance(v, dict) and v.get("@cat") == "v"]


def _nama_peneliti(b):
    return {n["nama"] for n in _node_baris(b) if n.get("@type") == "Peneliti" and n.get("nama")}


def _angka_baris(b):
    """(nama_angka, nilai) dari RELASI di baris, atau kolom agregat `jumlah`."""
    if not isinstance(b, dict):
        return None, None
    for v in b.values():
        if isinstance(v, dict) and v.get("@cat") == "e":
            for k in ANGKA_RELASI:
                if isinstance(v.get(k), (int, float)):
                    return k, v[k]
    j = b.get("jumlah")
    return ("jumlah", j) if isinstance(j, (int, float)) else (None, None)


def peringkat(baris):
    """Peringkat peneliti di hasil query -- dihitung KODE, bukan LLM.

    Pakai angka relasi (`jumlah`, atau `skor` utk SEBIDANG) bila ada; kalau
    tidak, banyaknya baris tempat nama itu muncul. Nama yang muncul di SEMUA
    baris adalah subjek pertanyaan (Devi pada "kolaborator Devi"), dikeluarkan.
    """
    muncul, skor, dasar = Counter(), Counter(), None
    for b in baris:
        nama = _nama_peneliti(b)
        muncul.update(nama)
        k, j = _angka_baris(b)
        if j is not None:
            dasar = k
            for n in nama:
                skor[n] += j
    if len(baris) > 1:
        for n in [n for n, c in muncul.items() if c == len(baris)]:
            muncul.pop(n, None)
            skor.pop(n, None)
    if skor:
        return len(muncul), dasar, skor
    if muncul and max(muncul.values()) > 1:
        return len(muncul), "kemunculan", muncul
    return len(muncul), None, Counter()


def hitung_tipe(baris):
    lihat = {}
    for b in baris:
        for n in _node_baris(b):
            lihat.setdefault(n.get("@type"), set()).add(n.get("@rid"))
    return {t: len(s) for t, s in sorted(lihat.items(), key=lambda x: -len(x[1]))}


def contoh_nama(baris, maks=5):
    """Nama/judul unik per tipe node selain Peneliti (yang punya peringkat)."""
    per = {}
    for b in baris:
        for n in _node_baris(b):
            t = n.get("@type")
            nm = n.get("nama") or n.get("judul")
            if t != "Peneliti" and nm and nm not in per.setdefault(t, []):
                per[t].append(str(nm)[:120])
    return {t: v[:maks] for t, v in per.items() if v}


def _fmt(dasar, v):
    return f"{v:.2f}" if dasar == "skor" else str(v)


def ringkasan_hitungan(baris, berurut=False):
    """Angka yang disiapkan program untuk kalimat jawaban.

    `berurut`: Cypher memakai ORDER BY, jadi yang teratas tetap benar walau
    hasil dibatasi 300 baris.
    Semua tipe dihitung, bukan hanya Peneliti; kalau tidak, hasil berisi
    topik terbaca "0 peneliti" dan dijawab "Tidak ada hasil."
    """
    n, dasar, c = peringkat(baris)
    # Batas 300 tidak disebut ke LLM; kalau disebut, ia dibaca sebagai jumlah.
    terpotong = len(baris) >= BATAS_QUERY and not berurut
    r = ["- jumlah baris: TIDAK DIKETAHUI (daftar sebagian) -- jangan menyebut jumlah total, "
         "pakai 'antara lain'" if terpotong else f"- jumlah baris: {len(baris)}"]
    tipe = hitung_tipe(baris)
    if n:
        # Hanya 'peneliti berbeda' (tanpa subjek pertanyaan) yang disebut;
        # dua angka membuat LLM memilih yang salah.
        tipe.pop("Peneliti", None)
    if tipe:
        r.append("- isi hasil: " + ", ".join(f"{t} {k}" for t, k in tipe.items()))
    if n:
        r.append(f"- peneliti berbeda: {n}")
    if dasar:
        label = {"jumlah": "jumlah", "skor": "skor kemiripan",
                 "kemunculan": "jumlah baris tempat namanya muncul"}[dasar]
        r.append(f"- teratas menurut {label}: " +
                 ", ".join(f"{k} ({_fmt(dasar, v)})" for k, v in c.most_common(8)))
    for t, v in contoh_nama(baris).items():
        r.append(f"- contoh {t}: " + "; ".join(v))
    if len(baris) >= BATAS_QUERY and berurut:
        r.append(f"- hasil sudah DIURUTKAN query; yang teratas benar walau dibatasi "
                 f"{BATAS_QUERY} baris -- JANGAN menyebut batas baris.")
    # Catatan batas ditempel kode (susun_kalimat), tidak diminta ke LLM.
    return "RINGKASAN HITUNGAN (dihitung program):\n" + "\n".join(r)


# Nama tipe utk kalimat cadangan berbahasa Inggris. Kunci = label basis data.
TIPE_EN = {"Peneliti": "researchers", "Publikasi": "publications", "Topik": "topics",
           "Kategori": "categories", "UnitKerja": "work units", "Institusi": "institutions",
           "Venue": "venues"}


def kalimat_cadangan(baris, bahasa="id"):
    """Kalimat yang disusun KODE -- dipakai bila kalimat LLM menyangkal hasil."""
    n, dasar, c = peringkat(baris)
    tipe = hitung_tipe(baris)
    if n:
        tipe["Peneliti"] = n
    en = bahasa == "en"
    isi = ", ".join(f"{TIPE_EN.get(t, t) if en else t} {k}" for t, k in tipe.items())
    bagian = [(f"{len(baris)} result rows" if en else f"Ada {len(baris)} baris hasil") +
              (f" ({isi})" if tipe else "") + "."]
    if dasar:
        bagian.append(("Top: " if en else "Teratas: ") +
                      ", ".join(f"{k} ({_fmt(dasar, v)})" for k, v in c.most_common(5)) + ".")
    for t, v in contoh_nama(baris, 3).items():
        bagian.append((f"Examples ({TIPE_EN.get(t, t)}): " if en else f"Contoh {t}: ") + "; ".join(v) + ".")
    return " ".join(bagian)


# Dengan masukan sama, LLM pernah menjawab "Tidak ada hasil." padahal ada
# 8 baris. Kode yang menegakkan: ada baris = tidak boleh disebut kosong.
# Penyangkalan dalam dua bahasa ditangkap.
POLA_MENYANGKAL = re.compile(
    r"\btidak ada (hasil|data)\b|\btidak ditemukan\b"
    r"|\bno (results?|data|matches|matching)\b|\bnot found\b|\bnothing (was )?found\b", re.I)


def jaga_kalimat(kalimat, baris, bahasa="id"):
    if baris and (not kalimat.strip() or POLA_MENYANGKAL.search(kalimat)):
        return kalimat_cadangan(baris, bahasa)
    return kalimat


# Bahasa jawaban ditentukan kode dari kata-kata pertanyaan, lalu disebut
# eksplisit ke LLM.
POLA_INDONESIA = re.compile(
    r"\b(yang|siapa|apa|apakah|berapa|mana|di|ke|dari|dengan|untuk|tentang|"
    r"peneliti|topik|publikasi|paling|pernah|belum|sudah|saja|pakar|ahli|cocok|"
    r"kerja|sama|naik|sejak|jurnal|seluruhnya|menulis|bidang)\b", re.I)


def bahasa_pertanyaan(teks):
    return "id" if POLA_INDONESIA.search(teks or "") else "en"


PROMPT_KALIMAT = """Kamu meringkas hasil query graf penelitian menjadi 1-2 kalimat.
BAHASA: tulis dalam bahasa di baris "Bahasa jawaban" (en = English, id = bahasa
Indonesia), apa pun bahasa data dan catatannya.

WAJIB:
- Pakai HANYA angka dan nama yang ada di RINGKASAN HITUNGAN dan data di bawah.
  Jangan menambah apa pun dari pengetahuanmu sendiri.
- Kalau datanya kosong, katakan tidak ada hasil. Jangan mengarang sebabnya.
- Jawab pertanyaannya LANGSUNG. Kalau menanyakan "siapa", sebut sampai 5 nama
  teratas beserta angkanya. Jangan menjawab hanya "Ada N hasil".
- Kalau RINGKASAN HITUNGAN punya baris "teratas", nama dan angkanya WAJIB
  diambil DARI SITU, dengan urutan yang sama. Jangan memilih sendiri dari data.
- Kalau masukan memuat baris "Catatan:", sebutkan isinya. Jangan menyebut
  nilai kolom teknis seperti `sumber` atau `skor`.
- Kalau 'jumlah baris' di RINGKASAN lebih dari 0, hasilnya TIDAK kosong: jangan
  menulis "tidak ada hasil". Sebut isinya (mis. nama topik).
- Untuk pertanyaan "siapa saja" / "apa saja", sebut JUMLAH totalnya dari
  RINGKASAN (mis. 12 peneliti), lalu beberapa contohnya.
- Singkat saja; grafnya sudah ditampilkan di sebelah."""

# Peringkat sudah dihitung program; data cukup sebagai contoh agar hemat kuota.
BATAS_KALIMAT_BARIS = 15
BATAS_KALIMAT_HURUF = 3000


POLA_URUT = re.compile(r"\bORDER\s+BY\b", re.I)
POLA_CATATAN_BATAS = re.compile(rf"\s*\([^()]*\b{BATAS_QUERY}\b[^()]*\)")


def susun_kalimat(pertanyaan, baris, catatan="", cypher=""):
    """Ringkas baris hasil jadi kalimat. Hanya baris yang dikirim, bukan DB."""
    n, dasar, c = peringkat(baris)
    kirim = baris
    if dasar:
        # Data diurutkan sesuai peringkat agar LLM melihat yang teratas.
        kirim = sorted(baris, reverse=True, key=lambda b: max(
            (c.get(x, 0) for x in _nama_peneliti(b)), default=0))
    data = json.dumps(buang_teks_panjang(kirim)[:BATAS_KALIMAT_BARIS],
                      ensure_ascii=False)[:BATAS_KALIMAT_HURUF]
    bahasa = bahasa_pertanyaan(pertanyaan)
    berurut = bool(POLA_URUT.search(cypher or ""))
    isi = (f"Pertanyaan: {pertanyaan}\nBahasa jawaban: {bahasa}\n"
           f"{ringkasan_hitungan(baris, berurut)}\n"
           f"{'Catatan: ' + catatan if catatan else ''}\n"
           f"Data:\n{data}")
    kalimat = _panggil([{"role": "system", "content": PROMPT_KALIMAT},
                        {"role": "user", "content": isi}], suhu=0.2, maks=600, usaha="low")
    kalimat = jaga_kalimat(kalimat, baris, bahasa)
    if len(baris) >= BATAS_QUERY and not berurut:
        # Catatan batas buatan LLM sendiri (dlm kurung, menyebut 300) dibuang.
        kalimat = POLA_CATATAN_BATAS.sub("", kalimat).rstrip()
        kalimat += (f" (Only the first {BATAS_QUERY} results were retrieved; the full list may be longer.)"
                    if bahasa == "en" else
                    f" (Hanya {BATAS_QUERY} hasil pertama yang diambil; daftar lengkapnya bisa lebih panjang.)")
    return kalimat