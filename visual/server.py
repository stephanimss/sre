#!/usr/bin/env python3
"""server.py — Server lokal untuk sre.html: menjalankan Cypher dan
mengembalikan graf bergaya sama dengan ekspor, plus API chat dan Brainstorm.

Kenapa perlu server:
- Halaman `file://` tidak boleh mengambil berkas lokal.
- ArcadeDB tidak mengirim header CORS, jadi server ini meneruskan
  permintaan ke port 2480 dari asal yang sama.
- ArcadeDB Studio hanya bisa menata gaya per tipe node, tidak per nilai
  properti (misalnya kategori ASJC). Server ini memakai kamus gaya
  ekspor_visual.py.

Jalankan:
    cd visual && ../.venv/bin/python server.py    # http://localhost:8000
"""
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import requests
from ekspor_visual import (ARCADEDB_AUTH, ARCADEDB_URL, LABEL_DARI, ke_cytoscape,
                           bangun_author_network,
                           TAK_BERARAH, VIEW, WARNA, WARNA_LAIN,
                           WARNA_NETRAL, bangun_individu, bangun_kolaborasi,
                           bangun_peneliti_topik, bentuk_dari_hasil,
                           CONTOH_PARAM, gaya_node, gaya_relasi, kelompok_view, urutan_view,
                           NAMA_TAMPIL, VIEW_EN)

from lib.arcadedb import URL_BACA, HOST as ARCADEDB_HOST, DB as ARCADEDB_DB
from tanya_ai import jawab
from lib.llm import LLMBelumDisetel
from banding import _q, banding
from banding_publikasi import banding_publikasi
from banding_organisasi import banding_organisasi
from ringkas_mapping import ringkas_mapping
from brainstorm import balas as brainstorm_balas, buat_sesi as brainstorm_sesi

# Batas graf yang boleh dikirim halaman untuk diringkas Brainstorm.
# Seluruh basis data ~14.850 node; di atas itu pasti bukan graf dari halaman.
MAKS_NODE_RINGKAS = 20000
MAKS_RELASI_RINGKAS = 100000
MAKS_TOPIK_GRAF_BANDING = 8
MAKS_HASIL_CARI = 50          # judul publikasi per pencarian
MAKS_PENULIS_GRAF = 12        # penulis per publikasi di graf Compare publications

# Pembentuk generik ada di ekspor_visual agar kotak query dan ekspor sama.
bentuk_graf = bentuk_dari_hasil

# Sebagian view mengembalikan nilai skalar (p.nama AS peneliti, ...), bukan
# objek node. Hasil seperti itu dikenali dari nama kolomnya, lalu diserahkan
# ke pembangun yang sama dengan ekspor.
# Urutan penting: pola paling khas lebih dulu. author_network dan
# kolaborasi_2 sama-sama berkolom a_id/b_id; bila urutannya terbalik,
# author_network kehilangan warna kategorinya.
POLA_SKALAR = [
    ({"a_id", "b_id", "a_ambigu", "b_ambigu"},     bangun_author_network),
    ({"pid", "peneliti", "pubid", "judul", "tid"}, bangun_individu),
    ({"a_id", "b_id"},                             bangun_kolaborasi),
    ({"pid", "peneliti", "tid", "topik"},          bangun_peneliti_topik),
]


def bentuk_dari_skalar(rows):
    """Kembalikan (nodes, edges) bila kolomnya cocok pola view; jika tidak, None."""
    if not rows:
        return None
    kolom = {k for k in rows[0] if not k.startswith("@")}
    for wajib, fungsi in POLA_SKALAR:
        if wajib <= kolom:
            return fungsi(rows)
    return None

# SRE_PORT: menjalankan salinan uji (mis. 8001) di samping server utama.
# SRE_HOST: 127.0.0.1 (bawaan, hanya laptop ini) atau 0.0.0.0 untuk hosting.
PORT = int(os.environ.get("SRE_PORT") or "8000")
HOST_SERVER = os.environ.get("SRE_HOST") or "127.0.0.1"
# Label node lebih panjang dari ini dipotong agar tidak saling menimpa.
MAKS_LABEL = 46


def lengkapi_kategori(nodes):
    """Catat kategori ASJC pada node Topik untuk panel detail, bukan untuk warna.

    Skornya ikut ditampilkan karena pemetaan topik -> kategori rata-rata hanya
    berskor ~0,46 dan tidak boleh terbaca sebagai kepastian.
    """
    topik = [n for n in nodes.values() if n["tipe"] == "Topik"]
    if not topik:
        return
    try:
        r = requests.post(URL_BACA, auth=ARCADEDB_AUTH, timeout=60, json={
            "language": "opencypher",
            "command": "MATCH (t:Topik {sumber:'bertopic'})-[r:TERMASUK_DALAM]->"
                       "(k:Kategori {skema:'ASJC'}) "
                       "RETURN t.nama AS nama, k.nama AS kat, "
                       "k.supergroup AS sg, r.skor AS skor"})
        peta = {}
        for x in r.json().get("result", []):
            peta.setdefault(x["nama"], []).append((x["kat"], x["sg"], x["skor"]))
    except Exception:
        return
    for n in topik:
        for nama, daftar in peta.items():
            if nama and n["label"] and nama.startswith(n["label"][:24]):
                kat, sg, skor = max(daftar, key=lambda d: d[2] or 0)
                n["kategori"], n["supergroup"] = kat, sg
                n["skor_kategori"] = skor
                n["title"] = f"{nama}\n{kat} (skor {skor})\n{sg}"
                n["asjc_dominan"] = kat
                n["warna_asjc"] = WARNA.get(kat, WARNA_LAIN)
                break


def lengkapi_asjc_dominan(nodes):
    """Tambahkan asjc_dominan dan warna_asjc pada semua node yang bisa.

    Publikasi -[:MASUK_KATEGORI]-> Kategori dan Peneliti -[:MENULIS]-> Publikasi
    dibaca dalam dua kueri (masing-masing di bawah 20.000 baris), lalu digabung
    di Python. Topik dan Kategori cukup membaca properti yang sudah ada.

    Id node bisa @rid ("#8:35") atau kunci bisnis ("PRSDI016"); keduanya
    diindeks. SQL dipakai karena openCypher ArcadeDB tidak mendukung `p.@rid`.
    """
    from collections import Counter

    for n in nodes.values():
        if n["tipe"] == "Topik" and n.get("kategori"):
            n["asjc_dominan"] = n["kategori"]
            n["warna_asjc"] = WARNA.get(n["kategori"], WARNA_LAIN)
        elif n["tipe"] == "Kategori":
            nama = n.get("kategori") or n.get("label") or ""
            if nama:
                n["asjc_dominan"] = nama
                n["warna_asjc"] = WARNA.get(nama, WARNA_LAIN)

    ada_pub = any(n["tipe"] == "Publikasi" for n in nodes.values())
    ada_pen = any(n["tipe"] == "Peneliti" for n in nodes.values())
    if not ada_pub and not ada_pen:
        return

    sql_url = URL_BACA

    try:
        r = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
            "language": "sql",
            "command":
                "SELECT p.@rid AS rid, p.dedup_key AS dk, k.nama AS kat "
                "FROM (MATCH {type:Publikasi, as:p}-MASUK_KATEGORI->"
                "{type:Kategori, as:k} RETURN p, k)"})
        pub_kat = {}
        for x in r.json().get("result", []):
            rid = x.get("rid")
            dk = x.get("dk")
            kat = x.get("kat")
            if kat:
                if rid:
                    pub_kat.setdefault(rid, []).append(kat)
                if dk:
                    pub_kat.setdefault(dk, []).append(kat)
    except Exception:
        return

    try:
        r_fb = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
            "language": "sql",
            "command":
                "SELECT p.@rid AS rid, p.dedup_key AS dk, k.nama AS kat "
                "FROM (MATCH {type:Publikasi, as:p}-MEMBAHAS->"
                "{type:Topik, as:t}-TERMASUK_DALAM->"
                "{type:Kategori, as:k} RETURN p, k)"})
        for x in r_fb.json().get("result", []):
            rid = x.get("rid") or ""
            dk = x.get("dk") or ""
            kat = x.get("kat")
            if kat:
                if rid and rid not in pub_kat:
                    pub_kat.setdefault(rid, []).append(kat)
                if dk and dk not in pub_kat:
                    pub_kat.setdefault(dk, []).append(kat)
    except Exception:
        pass

    for n in nodes.values():
        if n["tipe"] == "Publikasi" and n["id"] in pub_kat:
            best = Counter(pub_kat[n["id"]]).most_common(1)[0][0]
            n["asjc_dominan"] = best
            n["warna_asjc"] = WARNA.get(best, WARNA_LAIN)

    if not ada_pen:
        return
    try:
        r2 = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
            "language": "sql",
            "command":
                "SELECT pen.@rid AS rid, pen.local_id AS lid, "
                "p.@rid AS pub_rid "
                "FROM (MATCH {type:Peneliti, as:pen}-MENULIS->"
                "{type:Publikasi, as:p} RETURN pen, p)"})
        pen_kat = {}
        for x in r2.json().get("result", []):
            rid = x.get("rid")
            lid = x.get("lid")
            pub_rid = x.get("pub_rid")
            if pub_rid:
                cats = pub_kat.get(pub_rid, [])
                if cats:
                    if rid:
                        pen_kat.setdefault(rid, []).extend(cats)
                    if lid:
                        pen_kat.setdefault(lid, []).extend(cats)
    except Exception:
        return

    for n in nodes.values():
        if n["tipe"] == "Peneliti" and n["id"] in pen_kat:
            best = Counter(pen_kat[n["id"]]).most_common(1)[0][0]
            n["asjc_dominan"] = best
            n["warna_asjc"] = WARNA.get(best, WARNA_LAIN)

def graf_cytoscape(rows):
    """Baris hasil query -> elemen Cytoscape, lewat rantai yang SAMA dgn
    /api/query. None bila tidak ada node yang bisa digambar."""
    nodes, edges, _ = bentuk_graf(rows)
    lewat_pola = False
    if not nodes:
        hasil_pola = bentuk_dari_skalar(rows)
        if hasil_pola:
            nodes, edges = hasil_pola
            lewat_pola = True
    if not nodes:
        return None
    if not lewat_pola:
        lengkapi_kategori(nodes)
    lengkapi_asjc_dominan(nodes)
    return ke_cytoscape(nodes, edges)

def kutip(x):
    """Lolos-kan tanda kutip tunggal -- banyak judul publikasi memuatnya."""
    return str(x).replace("\\", "\\\\").replace("'", "\\'")


class Handler(SimpleHTTPRequestHandler):
    def _kirim(self, kode, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(kode)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/view":
            # `catatan` memuat alasan, angka, dan peringatan tiap view.
            # `contoh` memberi nilai parameter yang sah; tebakan keliru gagal
            # diam-diam (0 baris).
            # Kolom *_en untuk tampilan Inggris; kolom asli tetap dikirim.
            daftar = [{"nama": k, "judul": v["judul"], "params": v["params"],
                       "contoh": {p: CONTOH_PARAM.get(p) for p in v["params"]},
                       "catatan": v.get("catatan", ""),
                       "kelompok": kelompok_view(k),
                       "judul_en": VIEW_EN.get(k, (v["judul"], ""))[0],
                       "keterangan_en": VIEW_EN.get(k, ("", ""))[1],
                       "kelompok_en": NAMA_TAMPIL["kelompok"].get(kelompok_view(k), kelompok_view(k)),
                       "cypher": v["cypher"].strip()} for k, v in VIEW.items()]
            daftar.sort(key=lambda x: urutan_view(x["nama"]))
            return self._kirim(200, daftar)
        if self.path == "/api/peneliti":
            return self._daftar_peneliti()
        if self.path == "/api/tampilan":
            # Nama tampil Inggris; kuncinya tetap nama di basis data.
            return self._kirim(200, NAMA_TAMPIL)
        if self.path == "/api/asjc":
            return self._peta_asjc()
        return super().do_GET()

    def _bukti_relasi(self, muatan):
        """Kenapa dua node tersambung: publikasi yang melahirkan garisnya.

        KOLABORASI, SEBIDANG, dan BIDANG_KEAHLIAN adalah relasi turunan, jadi
        satu-satunya cara memeriksanya adalah menelusuri balik ke MENULIS dan
        MEMBAHAS.
        """
        jenis = muatan.get("jenis") or ""
        a, b = muatan.get("source"), muatan.get("target")
        if not a or not b:
            return self._kirim(400, {"error": "butuh source & target"})

        def q(c):
            r = requests.post(URL_BACA, auth=ARCADEDB_AUTH, timeout=120,
                              json={"language": "opencypher", "command": c})
            if r.status_code >= 400:
                raise RuntimeError(r.text[:200])
            return r.json().get("result", [])

        # Id node datang dalam dua bentuk:
        #   RID ArcadeDB ("#8:92")       -- view `RETURN p, r, t`
        #   kunci bisnis ("PTIUB002")    -- view yang mengembalikan skalar
        # Keduanya harus dikenali; kalau tidak, bukti garis kosong tanpa galat.
        def sisi(nid):
            if str(nid).startswith("#"):
                h = q(f"MATCH (n) WHERE id(n) = '{kutip(nid)}' RETURN n LIMIT 1")
                if h:
                    n = h[0]
                    lbl = (n.get("@type") or "?")
                    return {"tipe": lbl, "id": nid,
                            "nama": n.get("nama") or n.get("judul") or nid,
                            "internal": n.get("is_internal") is True,
                            "kunci": (n.get("local_id") or n.get("topik_id")
                                      or n.get("dedup_key") or n.get("kode")
                                      or n.get("venue_id") or n.get("nama"))}
                return {"tipe": "?", "id": nid, "nama": nid,
                        "internal": False, "kunci": None}
            for lbl, kunci in (("Peneliti", "local_id"), ("Publikasi", "dedup_key"),
                               ("Topik", "topik_id"), ("Kategori", "nama"),
                               ("Venue", "venue_id"), ("UnitKerja", "kode"),
                               ("Institusi", "nama")):
                h = q(f"MATCH (n:{lbl} {{{kunci}:'{kutip(nid)}'}}) RETURN n LIMIT 1")
                if h:
                    n = h[0]
                    return {"tipe": lbl, "id": nid,
                            "nama": n.get("nama") or n.get("judul") or nid,
                            "internal": n.get("is_internal") is True, "kunci": nid}
            return {"tipe": "?", "id": nid, "nama": nid,
                    "internal": False, "kunci": None}

        try:
            sa, sb = sisi(a), sisi(b)
            # Mulai sini pakai kunci bisnis (local_id/topik_id), bukan RID.
            a, b = sa.get("kunci") or a, sb.get("kunci") or b
            bukti, catatan = [], ""

            if jenis == "KOLABORASI":
                catatan = ("Publications listing BOTH as authors. If one of them is not "
                           "really by either person, the error is in the source data "
                           "(MySQL), not in the graph.")
                for p in q(f"MATCH (:Peneliti {{local_id:\'{kutip(a)}\'}})-[:MENULIS]->"
                           f"(pub:Publikasi)<-[:MENULIS]-(:Peneliti {{local_id:\'{kutip(b)}\'}}) "
                           f"RETURN pub.dedup_key AS kunci, pub.judul AS judul, "
                           f"pub.tahun AS tahun, pub.doi AS doi, "
                           f"pub.mysql_ids AS mysql_ids ORDER BY pub.tahun DESC")[:60]:
                    pen = q(f"MATCH (x:Peneliti)-[:MENULIS]->"
                            f"(:Publikasi {{dedup_key:\'{kutip(p['kunci'])}\'}}) "
                            f"RETURN x.nama AS n, x.is_internal AS i ORDER BY x.nama")
                    p["penulis"] = [{"nama": x["n"], "internal": x["i"] is True}
                                    for x in pen]
                    bukti.append(p)

            elif jenis == "SEBIDANG":
                catatan = ("Topics both researchers work on. SIMILAR_FIELD does NOT "
                           "mean they have ever co-authored.")
                bukti = q(f"MATCH (:Peneliti {{local_id:\'{kutip(a)}\'}})-[ba:BIDANG_KEAHLIAN]->"
                          f"(t:Topik)<-[bb:BIDANG_KEAHLIAN]-(:Peneliti {{local_id:\'{kutip(b)}\'}}) "
                          f"RETURN t.nama AS judul, ba.jumlah AS jumlah_a, "
                          f"bb.jumlah AS jumlah_b ORDER BY ba.jumlah DESC")[:60]

            elif jenis in ("MEMBAHAS", "TERMASUK_DALAM", "MASUK_KATEGORI"):
                # Relasi turunan (BERTopic, pemetaan ASJC). Buktinya adalah
                # skor kecocokan, yang wajib ditampilkan karena rata-ratanya
                # rendah (~0,46).
                pass

            elif jenis == "BIDANG_KEAHLIAN":
                catatan = "This researcher's publications on that topic."
                bukti = q(f"MATCH (:Peneliti {{local_id:\'{kutip(a)}\'}})-[:MENULIS]->"
                          f"(pub:Publikasi)-[:MEMBAHAS]->(:Topik {{topik_id:\'{kutip(b)}\'}}) "
                          f"RETURN pub.dedup_key AS kunci, pub.judul AS judul, "
                          f"pub.tahun AS tahun, pub.doi AS doi "
                          f"ORDER BY pub.tahun DESC")[:60]

            # Sifat relasi selalu diambil; untuk MEMBAHAS/TERMASUK_DALAM
            # hanya itu buktinya (skor + sumber).
            sifat = {}
            if jenis:
                h = q(f"MATCH (x)-[r:{jenis}]->(y) "
                      f"WHERE (x.local_id = '{kutip(a)}' OR x.dedup_key = '{kutip(a)}' "
                      f"OR x.topik_id = '{kutip(a)}' OR x.nama = '{kutip(a)}') "
                      f"AND (y.local_id = '{kutip(b)}' OR y.dedup_key = '{kutip(b)}' "
                      f"OR y.topik_id = '{kutip(b)}' OR y.nama = '{kutip(b)}') "
                      f"RETURN r LIMIT 1")
                if h:
                    sifat = {k: v for k, v in h[0].items() if not k.startswith("@")}

            # Hanya relasi ini yang berasal dari data sumber; sisanya dihitung.
            ASLI = {"AFILIASI", "MEMILIKI", "BERAFILIASI", "MENULIS", "TERBIT_DI"}
            return self._kirim(200, {"jenis": jenis, "dari": sa, "ke": sb,
                                     "catatan": catatan, "bukti": bukti,
                                     "jumlah_bukti": len(bukti),
                                     "sifat": sifat,
                                     "dari_sumber": jenis in ASLI})
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300]})

    def _detail_node(self, muatan):
        """Isi lengkap satu node -- termasuk ABSTRAK untuk Publikasi.

        Abstrak sengaja TIDAK ikut di JSON graf: 6.607 abstrak menambah sekitar
        6 MB pada tiap muat halaman, padahal yang dibaca satu-dua saja. Diambil
        sesuai permintaan, seperti bukti relasi.
        """
        nid = muatan.get("id")
        if not nid:
            return self._kirim(400, {"error": "id required"})

        def q(c):
            r = requests.post(URL_BACA, auth=ARCADEDB_AUTH, timeout=120,
                              json={"language": "opencypher", "command": c})
            if r.status_code >= 400:
                raise RuntimeError(r.text[:200])
            return r.json().get("result", [])

        try:
            if str(nid).startswith("#"):
                h = q(f"MATCH (n) WHERE id(n) = '{kutip(nid)}' RETURN n LIMIT 1")
            else:
                h = []
                for lbl, k in (("Peneliti", "local_id"), ("Publikasi", "dedup_key"),
                               ("Topik", "topik_id"), ("Kategori", "nama"),
                               ("Venue", "venue_id"), ("UnitKerja", "kode"),
                               ("Institusi", "nama")):
                    h = q(f"MATCH (n:{lbl} {{{k}:'{kutip(nid)}'}}) RETURN n LIMIT 1")
                    if h:
                        break
            if not h:
                return self._kirim(404, {"error": "node not found"})
            n = h[0]
            tipe = n.get("@type") or "?"
            kunci = (n.get("local_id") or n.get("dedup_key") or n.get("topik_id")
                     or n.get("kode") or n.get("venue_id") or n.get("nama"))
            out = {"tipe": tipe, "id": nid, "kunci": kunci, "rid": n.get("@rid"),
                   "nama": n.get("nama") or n.get("judul") or kunci,
                   "sifat": {k: v for k, v in n.items()
                             if not k.startswith("@") and k != "abstrak"},
                   "abstrak": None, "daftar": []}

            if tipe == "Publikasi":
                out["abstrak"] = n.get("abstrak")
                out["daftar"] = [
                    {"judul": "Authors", "isi": [
                        {"nama": x["n"], "internal": x["i"] is True} for x in
                        q(f"MATCH (p:Peneliti)-[:MENULIS]->"
                          f"(:Publikasi {{dedup_key:'{kutip(kunci)}'}}) "
                          f"RETURN p.nama AS n, p.is_internal AS i ORDER BY p.nama")]},
                    {"judul": "Topics", "isi": [
                        {"nama": x["n"], "skor": x["s"]} for x in
                        q(f"MATCH (:Publikasi {{dedup_key:'{kutip(kunci)}'}})"
                          f"-[m:MEMBAHAS]->(t:Topik) "
                          f"RETURN t.nama AS n, m.skor AS s")]},
                    {"judul": "Published in", "isi": [
                        {"nama": x["n"], "skor": x["k"]} for x in
                        q(f"MATCH (:Publikasi {{dedup_key:'{kutip(kunci)}'}})"
                          f"-[:TERBIT_DI]->(v:Venue) "
                          f"RETURN v.nama AS n, v.kuartil AS k")]},
                ]

            elif tipe == "Peneliti":
                out["daftar"] = [
                    {"judul": "Unit", "isi": [
                        {"nama": x["n"]} for x in
                        q(f"MATCH (u:UnitKerja)-[:MEMILIKI]->"
                          f"(:Peneliti {{local_id:'{kutip(kunci)}'}}) "
                          f"RETURN u.kode AS n")]},
                    {"judul": "Top expertise topics", "isi": [
                        {"nama": x["n"], "jumlah": x["j"]} for x in
                        q(f"MATCH (:Peneliti {{local_id:'{kutip(kunci)}'}})"
                          f"-[b:BIDANG_KEAHLIAN]->(t:Topik) "
                          f"RETURN t.nama AS n, b.jumlah AS j "
                          f"ORDER BY b.jumlah DESC LIMIT 8")]},
                    {"judul": "Top collaborators", "isi": [
                        {"nama": x["n"], "jumlah": x["j"], "internal": x["i"] is True}
                        for x in
                        q(f"MATCH (:Peneliti {{local_id:'{kutip(kunci)}'}})"
                          f"-[r:KOLABORASI]-(p:Peneliti) "
                          f"RETURN p.nama AS n, r.jumlah AS j, p.is_internal AS i "
                          f"ORDER BY r.jumlah DESC LIMIT 8")]},
                    # `kunci`: judul bisa diklik ke detail, "Show all" memuat semua.
                    {"judul": "Latest publications", "isi": [
                        {"nama": f"[{x['t']}] {x['j']}", "kunci": x["k"],
                         "judul_pub": x["j"], "tahun": x["t"]} for x in
                        q(f"MATCH (:Peneliti {{local_id:'{kutip(kunci)}'}})"
                          f"-[:MENULIS]->(pub:Publikasi) WHERE pub.tahun IS NOT NULL "
                          f"RETURN pub.judul AS j, pub.tahun AS t, pub.dedup_key AS k "
                          f"ORDER BY pub.tahun DESC LIMIT 8")]},
                ]
                out["cacah"] = {"publikasi": q(
                    f"MATCH (:Peneliti {{local_id:'{kutip(kunci)}'}})-[:MENULIS]->(x) "
                    f"RETURN count(x) AS c")[0]["c"]}

            elif tipe == "Topik":
                out["daftar"] = [
                    {"judul": "ASJC category", "isi": [
                        {"nama": x["n"], "skor": x["s"]} for x in
                        q(f"MATCH (:Topik {{topik_id:'{kutip(kunci)}'}})"
                          f"-[td:TERMASUK_DALAM]->(k:Kategori) "
                          f"RETURN k.nama AS n, td.skor AS s")]},
                    {"judul": "Most active researchers", "isi": [
                        {"nama": x["n"], "jumlah": x["j"], "internal": x["i"] is True}
                        for x in
                        q(f"MATCH (p:Peneliti)-[b:BIDANG_KEAHLIAN]->"
                          f"(:Topik {{topik_id:'{kutip(kunci)}'}}) "
                          f"RETURN p.nama AS n, b.jumlah AS j, p.is_internal AS i "
                          f"ORDER BY b.jumlah DESC LIMIT 8")]},
                ]
            return self._kirim(200, out)
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300]})

    def _tanya(self):
        """Pertanyaan bahasa Indonesia -> Cypher (LLM) -> penjaga -> graf.

        Cypher ikut dikirim ke halaman dan ditaruh di kotak query, supaya
        pengguna bisa memeriksa dan menjalankannya ulang. Kesalahan yang tak
        tertangkap penjaga -- Cypher sah yang menjawab pertanyaan lain --
        hanya bisa ketahuan kalau query-nya terlihat.
        """
        n = int(self.headers.get("Content-Length", 0))
        try:
            p = str(json.loads(self.rfile.read(n))["pertanyaan"]).strip()
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        if not p:
            return self._kirim(400, {"error": "pertanyaan kosong"})
        if len(p) > 300:
            return self._kirim(400, {"error": "pertanyaan terlalu panjang (maks 300 huruf)"})
        try:
            h = jawab(p, cetak=lambda *a: None)
        except LLMBelumDisetel as e:
            return self._kirim(503, {"error": str(e)})
        except Exception as e:
            # termasuk kuota Groq per menit habis (429) -- pesannya dari llm.py
            return self._kirim(502, {"error": str(e)})
        out = graf_cytoscape(h["baris"]) or {"nodes": [], "edges": []}
        out.update({"pertanyaan": p, "cypher": h["cypher"],
                    "status": h["status"], "pesan": h["pesan"],
                    "kalimat": h.get("kalimat", ""),
                    "lewat_judul": h.get("lewat_judul", False),
                    "riwayat": h.get("riwayat", []),
                    "baris": len(h["baris"])})
        return self._kirim(200, out)

    # ------------------------------------------------------------ Brainstorm
    def _baca_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _peta_asjc(self):
        """Peta node-ID -> {asjc_dominan, warna_asjc} untuk SEMUA node.

        Dipanggil halaman saat toggle ASJC diaktifkan pada data statis
        (data_cy.json) yang tidak membawa properti asjc_dominan.
        """
        from collections import Counter
        sql_url = URL_BACA
        peta = {}
        try:
            r = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
                "language": "sql",
                "command":
                    "SELECT p.@rid AS rid, p.dedup_key AS dk, k.nama AS kat "
                    "FROM (MATCH {type:Publikasi, as:p}-MASUK_KATEGORI->"
                    "{type:Kategori, as:k} RETURN p, k)"})
            pub_kat = {}
            for x in r.json().get("result", []):
                rid = x.get("rid") or ""
                dk = x.get("dk") or ""
                kat = x.get("kat")
                if kat:
                    if rid:
                        pub_kat.setdefault(rid, []).append(kat)
                    if dk:
                        pub_kat.setdefault(dk, []).append(kat)

            r_fb = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
                "language": "sql",
                "command":
                    "SELECT p.@rid AS rid, p.dedup_key AS dk, k.nama AS kat "
                    "FROM (MATCH {type:Publikasi, as:p}-MEMBAHAS->"
                    "{type:Topik, as:t}-TERMASUK_DALAM->"
                    "{type:Kategori, as:k} RETURN p, k)"})
            for x in r_fb.json().get("result", []):
                rid = x.get("rid") or ""
                dk = x.get("dk") or ""
                kat = x.get("kat")
                if kat:
                    if rid and rid not in pub_kat:
                        pub_kat.setdefault(rid, []).append(kat)
                    if dk and dk not in pub_kat:
                        pub_kat.setdefault(dk, []).append(kat)

            for k, cats in pub_kat.items():
                best = Counter(cats).most_common(1)[0][0]
                peta[k] = {"asjc_dominan": best,
                           "warna_asjc": WARNA.get(best, WARNA_LAIN)}

            r2 = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
                "language": "sql",
                "command":
                    "SELECT pen.@rid AS rid, pen.local_id AS lid, "
                    "p.@rid AS pub_rid "
                    "FROM (MATCH {type:Peneliti, as:pen}-MENULIS->"
                    "{type:Publikasi, as:p} RETURN pen, p)"})
            pen_kat = {}
            for x in r2.json().get("result", []):
                rid = x.get("rid") or ""
                lid = x.get("lid") or ""
                pub_rid = x.get("pub_rid") or ""
                cats = pub_kat.get(pub_rid, [])
                if cats:
                    if rid:
                        pen_kat.setdefault(rid, []).extend(cats)
                    if lid:
                        pen_kat.setdefault(lid, []).extend(cats)
            for k, cats in pen_kat.items():
                best = Counter(cats).most_common(1)[0][0]
                peta[k] = {"asjc_dominan": best,
                           "warna_asjc": WARNA.get(best, WARNA_LAIN)}

            r3 = requests.post(sql_url, auth=ARCADEDB_AUTH, timeout=120, json={
                "language": "sql",
                "command":
                    "SELECT t.@rid AS rid, t.topik_id AS tid, "
                    "k.nama AS kat, r.skor AS skor "
                    "FROM (MATCH {type:Topik, as:t, where:(sumber='bertopic')}"
                    "-TERMASUK_DALAM->{type:Kategori, as:k, "
                    "where:(skema='ASJC')} RETURN t, k)"})
            topik_kat = {}
            for x in r3.json().get("result", []):
                rid = x.get("rid") or ""
                tid = x.get("tid") or ""
                kat = x.get("kat")
                skor = x.get("skor") or 0
                for kunci in (rid, tid):
                    if kunci:
                        prev = topik_kat.get(kunci)
                        if not prev or skor > prev[1]:
                            topik_kat[kunci] = (kat, skor)
            for k, (kat, _) in topik_kat.items():
                peta[k] = {"asjc_dominan": kat,
                           "warna_asjc": WARNA.get(kat, WARNA_LAIN)}
        except Exception as e:
            return self._kirim(503, {"error": f"ArcadeDB: {str(e)[:200]}"})
        return self._kirim(200, peta)

    def _daftar_peneliti(self):
        """96 peneliti internal utk kotak ketik nama. `rid` ikut dikirim supaya
        halaman bisa mengenali node Peneliti di graf (id-nya @rid)."""
        try:
            rows = _q("""
MATCH (p:Peneliti {is_internal: true})
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
OPTIONAL MATCH (u)-[:AFILIASI]->(i:Institusi)
RETURN p.local_id AS id, elementId(p) AS rid, p.nama AS nama,
       u.kode AS unit, i.nama AS institusi ORDER BY p.nama""")
        except Exception as e:
            return self._kirim(503, {"error": f"ArcadeDB: {str(e)[:200]}"})
        return self._kirim(200, rows)

    def _banding(self):
        """Mode Compare: bukti kolaborasi 2-3 peneliti + graf perbandingannya.

        Graf sengaja RAMPING: peneliti terpilih, topik BERSAMA teratas, relasi
        SEBIDANG/KOLABORASI di antara mereka, dan publikasi di pasangan
        "publikasi mirip". Perantara tidak digambar -- mereka di panel bukti;
        di kanvas mereka hanya node melayang tanpa garis.
        """
        try:
            ids = self._baca_json().get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                return self._kirim(400, {"error": "butuh ids: daftar local_id"})
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        try:
            h = banding(ids)
        except ValueError as e:              # penolakan yg disengaja -- teruskan pesannya
            return self._kirim(400, {"error": str(e)})
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300]})

        ids = [p["id"] for p in h["peneliti"]]
        tids = [t["topik_id"] for t in h["topik_bersama"][:MAKS_TOPIK_GRAF_BANDING]]
        kunci = [m[s]["kunci"] for m in h["publikasi_mirip"] for s in ("a", "b")]
        try:
            graf = self._graf_mentah(ids, tids, kunci)
            rid = {r["k"]: r["e"] for r in _q(
                "MATCH (p:Peneliti) WHERE p.local_id IN $ids RETURN p.local_id AS k, elementId(p) AS e",
                {"ids": ids})}
        except Exception as e:
            return self._kirim(500, {"error": f"graf perbandingan: {str(e)[:250]}"})
        # `rid`: local_id -> @rid, untuk memberi cincin pada node terpilih.
        return self._kirim(200, {"bukti": h, "graf": graf, "rid": rid})

    def _banding_publikasi(self):
        """Compare publications: bukti 2-3 publikasi + graf kecilnya.

        Graf: publikasi terpilih, penulisnya (maks 12 per publikasi, internal
        didahulukan), topiknya, dan publikasi terkait beserta penulis
        internalnya -- supaya terlihat SIAPA lagi yang meneliti hal serupa.
        """
        try:
            kunci = self._baca_json().get("kunci")
            if not isinstance(kunci, list) or not all(isinstance(k, str) for k in kunci):
                return self._kirim(400, {"error": "butuh kunci: daftar dedup_key"})
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        try:
            h = banding_publikasi(kunci)
        except ValueError as e:
            return self._kirim(400, {"error": str(e)})
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300]})
        pilih = [p["kunci"] for p in h["publikasi"]]
        terkait = [t["kunci"] for t in h["publikasi_terkait"]]
        penulis = [x["id"] for p in h["publikasi"] for x in p["penulis"][:MAKS_PENULIS_GRAF]]
        internal = [p["id"] for p in h["peneliti"]]
        try:
            baris = self._mentah("MATCH (p:Peneliti)-[r:MENULIS]->(k:Publikasi) "
                                 "WHERE k.dedup_key IN $k AND p.local_id IN $p RETURN p, r, k",
                                 {"k": pilih, "p": penulis})
            baris += self._mentah("MATCH (k:Publikasi)-[r:MEMBAHAS]->(t:Topik) "
                                  "WHERE k.dedup_key IN $k RETURN k, r, t", {"k": pilih + terkait})
            if terkait:
                baris += self._mentah("MATCH (p:Peneliti)-[r:MENULIS]->(k:Publikasi) "
                                      "WHERE k.dedup_key IN $k AND p.local_id IN $p RETURN p, r, k",
                                      {"k": terkait, "p": internal})
            graf = graf_cytoscape(baris) or {"nodes": [], "edges": []}
            rid = {r["k"]: r["e"] for r in _q(
                "MATCH (k:Publikasi) WHERE k.dedup_key IN $k RETURN k.dedup_key AS k, elementId(k) AS e",
                {"k": pilih})}
        except Exception as e:
            return self._kirim(500, {"error": f"graf perbandingan: {str(e)[:250]}"})
        return self._kirim(200, {"bukti": h, "graf": graf, "rid": rid})

    def _banding_organisasi(self):
        """Compare organizations: dua unit kerja atau institusi."""
        try:
            m = self._baca_json()
            kode_a = m.get("a")
            kode_b = m.get("b")
            mode = m.get("mode", "unit")
            if not kode_a or not kode_b:
                return self._kirim(400, {"error": "butuh a dan b (kode unit/nama institusi)"})
            if mode not in ("unit", "institusi"):
                return self._kirim(400, {"error": "mode harus 'unit' atau 'institusi'"})
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        try:
            h = banding_organisasi(kode_a, kode_b, mode)
        except ValueError as e:
            return self._kirim(400, {"error": str(e)})
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300]})
        ids_a = [p["id"] for p in h["org_a"]["daftar"]]
        ids_b = [p["id"] for p in h["org_b"]["daftar"]]
        semua = ids_a + ids_b
        try:
            baris = self._mentah(
                "MATCH (p:Peneliti) WHERE p.local_id IN $ids RETURN p", {"ids": semua})
            baris += self._mentah("""
MATCH (a:Peneliti)-[r:KOLABORASI]-(b:Peneliti)
WHERE a.local_id IN $a AND b.local_id IN $b RETURN a, r, b""",
                {"a": ids_a, "b": ids_b})
            tids = [t["topik_id"] for t in h["topik_bersama"][:8]]
            if tids:
                baris += self._mentah("""
MATCH (p:Peneliti)-[r:BIDANG_KEAHLIAN]->(t:Topik)
WHERE p.local_id IN $ids AND t.topik_id IN $tids RETURN p, r, t""",
                    {"ids": semua, "tids": tids})
            graf = graf_cytoscape(baris) or {"nodes": [], "edges": []}
        except Exception as e:
            return self._kirim(500, {"error": f"graf organisasi: {str(e)[:250]}"})
        return self._kirim(200, {"bukti": h, "graf": graf})

    def _cari(self):
        """Cari di BASIS DATA, bukan hanya di graf yang tampil: peneliti
        internal (nama) dan publikasi (judul). Tiap kata harus ada -- "deep
        learning tea" menemukan "... Deep Learning-Based Tea Diseases ...".
        Berparameter; teks pengguna tidak pernah disambung ke query."""
        try:
            teks = str(self._baca_json().get("teks") or "").strip().lower()
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        kata = [k for k in teks.split() if k][:5]
        if len(teks) < 2 or not kata:
            return self._kirim(400, {"error": "ketik minimal 2 huruf"})
        if len(teks) > 100:
            return self._kirim(400, {"error": "teks pencarian terlalu panjang"})
        par = {f"t{i}": k for i, k in enumerate(kata)}
        syarat_judul = " AND ".join(f"toLower(k.judul) CONTAINS $t{i}" for i in range(len(kata)))
        syarat_nama = " AND ".join(f"toLower(p.nama) CONTAINS $t{i}" for i in range(len(kata)))
        try:
            peneliti = _q(f"""
MATCH (p:Peneliti {{is_internal: true}}) WHERE {syarat_nama}
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
RETURN p.local_id AS id, elementId(p) AS rid, p.nama AS nama, u.kode AS unit
ORDER BY p.nama LIMIT 8""", par)
            total = _q(f"MATCH (k:Publikasi) WHERE {syarat_judul} RETURN count(k) AS n", par)[0]["n"]
            publikasi = _q(f"""
MATCH (k:Publikasi) WHERE {syarat_judul}
OPTIONAL MATCH (p:Peneliti {{is_internal: true}})-[:MENULIS]->(k)
WITH k, collect(p.nama) AS internal
RETURN k.dedup_key AS kunci, elementId(k) AS rid, k.judul AS judul, k.tahun AS tahun,
       k.sitasi AS sitasi, internal
ORDER BY coalesce(k.sitasi, 0) DESC LIMIT {MAKS_HASIL_CARI}""", par)
        except Exception as e:
            return self._kirim(503, {"error": f"ArcadeDB: {str(e)[:200]}"})
        return self._kirim(200, {"peneliti": peneliti, "publikasi": publikasi,
                                 "total_publikasi": total, "batas": MAKS_HASIL_CARI})

    def _publikasi_peneliti(self):
        """SEMUA publikasi satu peneliti (judul, tahun, sitasi) untuk daftar
        "Show all" di panel detail. Terbanyak 361 (terukur 18 Sep 2026) --
        dikirim sekaligus; halaman yang menyaring & mengurutkan."""
        try:
            lid = str(self._baca_json().get("id") or "")
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        if not lid:
            return self._kirim(400, {"error": "butuh id: local_id peneliti"})
        try:
            orang = _q("MATCH (p:Peneliti {local_id: $id}) RETURN p.nama AS nama", {"id": lid})
            if not orang:
                return self._kirim(404, {"error": "peneliti tidak ditemukan"})
            daftar = _q("""
MATCH (:Peneliti {local_id: $id})-[:MENULIS]->(k:Publikasi)
RETURN k.dedup_key AS kunci, elementId(k) AS rid, k.judul AS judul, k.tahun AS tahun,
       k.sitasi AS sitasi ORDER BY coalesce(k.tahun, 0) DESC, k.judul""", {"id": lid})
        except Exception as e:
            return self._kirim(503, {"error": f"ArcadeDB: {str(e)[:200]}"})
        return self._kirim(200, {"id": lid, "nama": orang[0]["nama"], "publikasi": daftar})

    def _mentah(self, c, p):
        r = requests.post(URL_BACA, auth=ARCADEDB_AUTH,
                          timeout=60, json={"language": "opencypher", "command": c, "params": p})
        r.raise_for_status()
        return r.json().get("result", [])

    def _graf_mentah(self, ids, tids, kunci):
        """Baris ArcadeDB UTUH (dgn @rid/@cat) -> elemen Cytoscape."""
        def mentah(c, p):
            r = requests.post(URL_BACA, auth=ARCADEDB_AUTH,
                              timeout=60, json={"language": "opencypher", "command": c, "params": p})
            r.raise_for_status()
            return r.json().get("result", [])
        baris = mentah("MATCH (p:Peneliti) WHERE p.local_id IN $ids RETURN p", {"ids": ids})
        if tids:
            baris += mentah("""MATCH (p:Peneliti)-[r:BIDANG_KEAHLIAN]->(t:Topik)
WHERE p.local_id IN $ids AND t.topik_id IN $tids RETURN p, r, t""", {"ids": ids, "tids": tids})
        baris += mentah("""MATCH (a:Peneliti)-[r:SEBIDANG|KOLABORASI]-(b:Peneliti)
WHERE a.local_id IN $ids AND b.local_id IN $ids RETURN a, r, b""", {"ids": ids})
        if kunci:
            baris += mentah("""MATCH (p:Peneliti)-[r:MENULIS]->(k:Publikasi)
WHERE p.local_id IN $ids AND k.dedup_key IN $k RETURN p, r, k""", {"ids": ids, "k": kunci})
        return graf_cytoscape(baris) or {"nodes": [], "edges": []}

    def _ringkas(self):
        """Mode Mapping: ringkasan graf yang sedang tampil di halaman."""
        try:
            m = self._baca_json()
            nodes, edges = m.get("nodes"), m.get("edges") or []
            if not isinstance(nodes, list) or not isinstance(edges, list):
                return self._kirim(400, {"error": "butuh nodes (daftar) dan edges (daftar)"})
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        if not nodes:
            return self._kirim(400, {"error": "graf kosong -- jalankan view atau query dulu"})
        if len(nodes) > MAKS_NODE_RINGKAS or len(edges) > MAKS_RELASI_RINGKAS:
            return self._kirim(413, {"error": f"graf terlalu besar ({len(nodes):,} node, "
                                              f"{len(edges):,} relasi) -- persempit lewat finder"})
        try:
            h = ringkas_mapping([{"id": n.get("id"), "tipe": n.get("tipe")} for n in nodes],
                                [{"source": e.get("source"), "target": e.get("target")} for e in edges],
                                m.get("sumber"))
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300]})
        return self._kirim(200, h)

    def _brainstorm(self):
        """Satu giliran brainstorming. Tanpa `sid`: sesi baru dari `mode` + `bukti`
        (bukti yang sedang tampil di panel). Sesi hanya di memori server."""
        try:
            m = self._baca_json()
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        sid = m.get("sid")
        try:
            if not sid:
                sid = brainstorm_sesi(m.get("mode"), m.get("bukti"))
            h = brainstorm_balas(sid, m.get("pesan"), m.get("abstrak", True) is not False)
        except KeyError as e:                       # sesi hilang (server restart / kedaluwarsa)
            return self._kirim(410, {"error": e.args[0] if e.args else "sesi tidak ditemukan"})
        except ValueError as e:
            return self._kirim(400, {"error": str(e), "sid": sid})
        except LLMBelumDisetel as e:
            return self._kirim(503, {"error": str(e), "sid": sid})
        except RuntimeError as e:
            # Kuota habis: sid tetap dikirim agar percobaan ulang memakai sesi yang sama.
            kode = 429 if "Kuota" in str(e) else 502
            return self._kirim(kode, {"error": str(e)[:300], "sid": sid})
        except Exception as e:
            return self._kirim(500, {"error": str(e)[:300], "sid": sid})
        return self._kirim(200, h)

    def do_POST(self):
        if self.path == "/api/brainstorm":
            return self._brainstorm()
        if self.path == "/api/node":
            n = int(self.headers.get("Content-Length", 0))
            try:
                return self._detail_node(json.loads(self.rfile.read(n)))
            except Exception as e:
                return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        if self.path == "/api/tanya":
            return self._tanya()
        if self.path == "/api/banding":
            return self._banding()
        if self.path == "/api/ringkas":
            return self._ringkas()
        if self.path == "/api/banding_publikasi":
            return self._banding_publikasi()
        if self.path == "/api/banding_organisasi":
            return self._banding_organisasi()
        if self.path == "/api/cari":
            return self._cari()
        if self.path == "/api/publikasi_peneliti":
            return self._publikasi_peneliti()
        if self.path == "/api/relasi":
            n = int(self.headers.get("Content-Length", 0))
            try:
                return self._bukti_relasi(json.loads(self.rfile.read(n)))
            except Exception as e:
                return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        if self.path != "/api/query":
            return self._kirim(404, {"error": "hanya /api/query, /api/tanya, /api/relasi, "
                                              "/api/node, /api/banding, /api/banding_publikasi, "
                                              "/api/banding_organisasi, /api/cari, "
                                              "/api/publikasi_peneliti, /api/ringkas, "
                                              "atau /api/brainstorm"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            muatan = json.loads(self.rfile.read(n))
            q = muatan["cypher"]
        except Exception as e:
            return self._kirim(400, {"error": f"payload tidak valid: {e}"})
        # 'vis' (bawaan) atau 'cytoscape': kamus gaya sama, bentuk JSON beda.
        bentuk = muatan.get("format", "vis")

        try:
            r = requests.post(URL_BACA, auth=ARCADEDB_AUTH, timeout=120,
                              json={"language": "opencypher", "command": q})
        except requests.exceptions.ConnectionError:
            return self._kirim(503, {"error":
                "ArcadeDB is not responding. Check ARCADEDB_HOST in .env."})
        if r.status_code >= 400:
            # teruskan pesan asli ArcadeDB; di situ letak salahnya dijelaskan
            try:
                pesan = r.json().get("detail") or r.json().get("error")
            except Exception:
                pesan = r.text[:300]
            return self._kirim(400, {"error": pesan})

        hasil = r.json()
        rows = hasil.get("result", [])
        batas = hasil.get("limit")
        nodes, edges, ditebak = bentuk_graf(rows)
        lewat_pola = False
        if not nodes:
            # Coba pola view tersimpan dulu: hasil skalar masih bisa digambar.
            hasil_pola = bentuk_dari_skalar(rows)
            if hasil_pola:
                nodes, edges = hasil_pola
                nodes = {k: v for k, v in nodes.items()}
                lewat_pola, ditebak = True, False
        if not nodes:
            # Dua sebab yang berbeda:
            #  (a) nol baris: kueri benar, datanya tidak ada (cna_uji sengaja begini)
            #  (b) ada baris tetapi skalar: baru di sini saran "RETURN p, t" berlaku
            if not rows:
                return self._kirim(200, {"nodes": [], "edges": [], "peringatan":
                    "The query ran but returned NO results (0 rows). For the "
                    "citation-network test view this is the expected proof: "
                    "there is no publication-to-publication relation, so a "
                    "citation network cannot be built."})
            return self._kirim(200, {"nodes": [], "edges": [], "baris": len(rows),
                "peringatan":
                f"{len(rows)} rows returned, but no NODES in them. To draw a "
                "graph, return the nodes (RETURN p, t), not their values "
                "(RETURN p.nama)."})
        if not lewat_pola:
            lengkapi_kategori(nodes)
        lengkapi_asjc_dominan(nodes)
        if bentuk == "cytoscape":
            out = ke_cytoscape(nodes, edges)
            out["baris"] = len(rows)
        else:
            out = {"nodes": list(nodes.values()), "edges": edges,
                   "baris": len(rows)}
        if batas and len(rows) >= batas:
            out["peringatan"] = (f"Result CUT at {batas} rows. "
                                 f"Add a smaller LIMIT.")
        elif len(nodes) > 400:
            out["peringatan"] = (f"{len(nodes)} nodes -- the drawing will be dense and "
                                 f"hard to read. Consider a smaller LIMIT.")
        elif ditebak:
            out["peringatan"] = ("Lines were GUESSED from column order because the "
                                 "query did not return the relations. Write "
                                 "RETURN p, r, t to get their type and weight.")
        return self._kirim(200, out)

    def log_message(self, *a):
        pass          # jangan banjiri terminal dgn log tiap permintaan


class Server(ThreadingHTTPServer):
    # Tanpa ini port tertahan ~1 menit (TIME_WAIT) setelah Ctrl+C.
    allow_reuse_address = True


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    try:
        # Bawaan hanya laptop ini. Semua kueri server lewat endpoint baca-saja,
        # jadi SRE_HOST=0.0.0.0 tidak membuka jalan untuk menulis; yang tetap
        # terbuka adalah kuota LLM lewat /api/tanya dan /api/brainstorm.
        srv = Server((HOST_SERVER, PORT), Handler)
    except OSError as e:
        if e.errno == 48:      # Address already in use
            print(f"Port {PORT} sudah dipakai -- kemungkinan server ini sudah")
            print("jalan di jendela lain. Buka saja halamannya:")
            print(f"  http://localhost:{PORT}/sre.html")
            print(f"\nKalau ingin menjalankan ulang, hentikan dulu yang lama:")
            print(f"  lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
            raise SystemExit(1)
        raise
    print(f"Graf SRE  ->  http://localhost:{PORT}/sre.html  (mendengarkan {HOST_SERVER})")
    print(f"  ArcadeDB: {ARCADEDB_HOST} / {ARCADEDB_DB}")
    print(f"  {len(VIEW)} view tersimpan · {len(WARNA)} warna kategori")
    print("  Ctrl+C untuk berhenti")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nberhenti.")
