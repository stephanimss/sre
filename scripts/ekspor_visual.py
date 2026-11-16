#!/usr/bin/env python3
"""ekspor_visual.py — Ekspor subgraf ArcadeDB ke JSON siap gambar.

Dua peran:
  1. PUSTAKA VIEW. Setiap tampilan disimpan sebagai entri bernama di VIEW
     (judul, parameter, Cypher). Inilah "Cypher preset" yang diminta
     pembimbing. Disimpan di repo, bukan di basis data, karena tim membaca
     dan menyalinnya.
  2. EKSPOR. Menjalankan view terpilih lalu menulis JSON.

Format keluaran: 'vis' (bawaan, vis-network) atau 'cytoscape'
(sre.html). Kueri dan transformasinya sama; hanya penulisan JSON yang beda.

    vis        {"nodes":[{"id","label","value","color",...}],
                "edges":[{"from","to","value","label","arrows"}]}
    cytoscape  {"nodes":[{"data":{...}}], "edges":[{"data":{"source","target"}}]}

Jalankan:
    python3 scripts/ekspor_visual.py --daftar
    python3 scripts/ekspor_visual.py --view unit_kerja --kode SI
    python3 scripts/ekspor_visual.py --view individu --local-id PRSDI007
    python3 scripts/ekspor_visual.py --view semua_internal --format cytoscape
"""
import argparse
import json
import os
import re
import colorsys

import requests

from lib.arcadedb import ARCADEDB_AUTH, ARCADEDB_URL

VISUAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "visual")
KELUARAN_BAKU = os.path.join(VISUAL, "data_graf.json")

# 27 kategori ASJC resmi, urut kode. Urutan ini menentukan warna: jangan
# diacak, atau warna berpindah kategori.
KATEGORI_ASJC = [
    "Multidisciplinary", "Agricultural and Biological Sciences",
    "Arts and Humanities", "Biochemistry, Genetics and Molecular Biology",
    "Business, Management and Accounting", "Chemical Engineering", "Chemistry",
    "Computer Science", "Decision Sciences", "Earth and Planetary Sciences",
    "Economics, Econometrics and Finance", "Energy", "Engineering",
    "Environmental Science", "Immunology and Microbiology", "Materials Science",
    "Mathematics", "Medicine", "Neuroscience", "Nursing",
    "Pharmacology, Toxicology and Pharmaceutics", "Physics and Astronomy",
    "Psychology", "Social Sciences", "Veterinary", "Dentistry",
    "Health Professions",
]

WARNA_LAIN = "#9CA3AF"            # nilai yang tidak dikenali kamus gaya
WARNA_PENELITI_BAKU = "#0F6E7B"   # peneliti tanpa kategori dominan

# Hanya untuk menampilkan supergroup tiap kategori di legenda.
# Bukan penentu bentuk node: semua node digambar lingkaran.
BENTUK = {
    "Life Sciences":     "dot",
    "Physical Sciences": "square",
    "Social Sciences":   "triangle",
    "Health Sciences":   "diamond",
}

# Relasi simetris ("A berkolaborasi dengan B" = sebaliknya): digambar
# putus-putus tanpa panah.
TAK_BERARAH = {"KOLABORASI", "SEBIDANG"}

def _palet(n):
    """n warna yang saling berjauhan.

    Rona disebar dengan sudut emas (137,508°), sehingga kategori bernomor
    berdekatan tidak mendapat rona yang mirip. Kecerahan diselang-seling tiga
    tingkat, karena rona saja tidak cukup untuk 27 warna.
    """
    keluar = []
    for i in range(n):
        rona = (i * 137.508 % 360) / 360
        terang = (0.40, 0.55, 0.68)[i % 3]
        jenuh = (0.72, 0.62)[i % 2]
        r, g, b = colorsys.hls_to_rgb(rona, terang, jenuh)
        keluar.append("#%02X%02X%02X" % (int(r*255), int(g*255), int(b*255)))
    return keluar

WARNA = dict(zip(KATEGORI_ASJC, _palet(len(KATEGORI_ASJC))))

# Koreksi manual: Arts and Humanities dan Social Sciences hanya berjarak
# ΔE 9,8 dan keduanya terpakai. Cokelat-oranye ini jauh dari keduanya dan
# sudah diperiksa tidak menimbulkan bentrokan baru.
WARNA["Arts and Humanities"] = "#B45309"

# Kamus gaya: satu-satunya tempat warna, ukuran, dan bentuk ditentukan.
# Berupa aturan "tipe X -> gaya Y" karena node baru lahir saat kueri
# dijalankan; aturan ini berlaku untuk kueri apa pun.
#
# 1. Semua node lingkaran ('dot'). Bentuk 'ellipse'/'box' melebar mengikuti
#    label, sehingga judul publikasi jadi kapsul raksasa.
# 2. Hanya Kategori yang membawa warna ASJC. Topik sengaja seragam karena
#    skor pemetaan topik -> kategori rata-rata hanya 0,46. Kategorinya tetap
#    tampil di panel detail.

WARNA_NETRAL = {
    "Institusi":  "#111827",   # paling gelap = paling tinggi di hierarki
    "UnitKerja":  "#4B5563",
    "Peneliti":   "#0F6E7B",   # satu-satunya warna beraksen: tokoh utama
    "Topik":      "#94A3B8",
    # Empat tingkat abu: kecerahan 17%, 33%, 45%, 84%.
    "Venue":      "#6B7280",
    "Publikasi":  "#D1D5DB",
}

# Ukuran tetap per tipe. None = diskalakan dari `value` (jumlah publikasi).
UKURAN_NODE = {
    "Institusi": 30, "UnitKerja": 23, "Peneliti": 16,
    "Venue": 10, "Publikasi": 7, "Kategori": 26,
    "Topik": None,          # None = diskalakan dari `value` (jumlah_dok)
}

# Properti yang dipakai jadi label, urut prioritas.
LABEL_DARI = {
    "Institusi": ("nama", "kode"), "UnitKerja": ("kode", "nama"),
    "Peneliti":  ("nama", "local_id"), "Publikasi": ("judul",),
    "Venue":     ("nama",), "Kategori": ("nama",),
    "Topik":     ("nama", "topik_id"),
}

# 11 tipe relasi dikelompokkan menjadi 4 keluarga warna.
KELUARGA_RELASI = {
    "struktur":     ("#94A3B8", {"AFILIASI", "MEMILIKI", "BERAFILIASI"}),
    "kepengarangan":("#CBD5E1", {"MENULIS", "TERBIT_DI"}),
    "semantik":     ("#0F766E", {"MEMBAHAS", "MASUK_KATEGORI",
                                 "TERMASUK_DALAM", "BIDANG_KEAHLIAN"}),
    "kemiripan":    ("#D97706", {"KOLABORASI", "SEBIDANG"}),
}
# Indeks balik jenis -> (keluarga, warna), dibuat sekali.
GAYA_RELASI = {j: (nama, warna)
               for nama, (warna, jenis) in KELUARGA_RELASI.items()
               for j in jenis}
# Bobot hanya bermakna pada dua keluarga ini; yang lain selalu 1.
BERBOBOT = {"semantik", "kemiripan"}




def gaya_node(tipe, kategori=None):
    """Gaya vis-network untuk satu tipe node. Dipakai ekspor DAN server."""
    g = {"shape": "dot", "borderWidth": 1.5}
    if tipe == "Kategori":
        # satu-satunya pembawa warna ASJC
        g["color"] = WARNA.get(kategori, WARNA_LAIN)
        g["size"] = UKURAN_NODE["Kategori"]
    else:
        g["color"] = WARNA_NETRAL.get(tipe, WARNA_LAIN)
        ukuran = UKURAN_NODE.get(tipe)
        if ukuran:
            g["size"] = ukuran
    return g


def gaya_relasi(jenis):
    """Gaya vis-network untuk satu tipe relasi."""
    keluarga, warna = GAYA_RELASI.get(jenis, ("struktur", WARNA_LAIN))
    # `keluarga`/`warna`/`berarah` diabaikan vis-network, tetapi dibaca
    # legenda garis, jadi legenda dan garis memakai sumber yang sama.
    g = {"color": {"color": warna, "highlight": "#1F2937"},
         "keluarga": keluarga, "warna": warna,
         "berarah": jenis not in TAK_BERARAH}
    if jenis in TAK_BERARAH:
        g["dashes"] = True          # simetris -> tanpa panah
    else:
        g["arrows"] = "to"
    if keluarga not in BERBOBOT:
        g["value"] = 1              # abaikan bobot, selalu tipis
    return g

def tulis_palet(path):
    """Halaman contoh warna. Periksa ini SEBELUM menggambar graf -- jauh
    lebih mudah menilai keterbacaan pada blok besar daripada pada node
    berdiameter 12-45 piksel.

    Kategori yang benar-benar terpakai di korpus ditandai, supaya perhatian
    tertuju ke situ: warna yang bentrok tapi kategorinya tidak muncul di data
    tidak perlu diperbaiki.
    """
    try:
        terpakai = {x["n"]: x["t"] for x in jalankan(
            "MATCH (t:Topik)-[:TERMASUK_DALAM]->"
            "(k:Kategori {skema:'ASJC'}) RETURN k.nama AS n, count(t) AS t")}
    except Exception:
        terpakai = {}

    # supergroup ikut ditampilkan sebagai pembeda kedua bagi warna yang mirip
    peta_bentuk = {}
    try:
        for x in jalankan("MATCH (k:Kategori {skema:'ASJC'}) RETURN k.nama AS n, "
                          "k.supergroup AS sg"):
            peta_bentuk[x["n"]] = (x["sg"], BENTUK.get(x["sg"], "dot"))
    except Exception:
        pass

    baris = []
    for nama, w in WARNA.items():
        n_topik = terpakai.get(nama, 0)
        sg, bentuk = peta_bentuk.get(nama, ("-", "-"))
        pudar = "" if n_topik else "opacity:.35;"
        tanda = f"{n_topik} topik" if n_topik else "tidak terpakai"
        baris.append(
            f'<div style="background:{w};{pudar}padding:15px 18px;color:#fff;'
            f'font:13px/1.4 -apple-system,sans-serif;text-shadow:0 1px 2px rgba(0,0,0,.4)">'
            f'<b>{nama}</b>'
            f'<span style="float:right;font-family:monospace">{w}</span><br>'
            f'<small>{tanda} &middot; {sg} &middot; bentuk: {bentuk}</small></div>')

    html = (f'<!doctype html><meta charset="utf-8"><title>Palet ASJC</title>'
            f'<body style="margin:0;background:#111">'
            f'<div style="padding:14px 18px;color:#eee;font:14px sans-serif">'
            f'{len(WARNA)} warna &middot; {sum(1 for v in terpakai.values() if v)} '
            f'kategori terpakai di korpus &middot; yang pudar tidak muncul di data'
            f'</div>{"".join(baris)}</body>')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"-> {path}")
    print("\n  Buka di browser, lalu uji dgn simulator buta warna:")
    print("    Chrome DevTools (Cmd+Opt+I) -> menu tiga titik -> More tools")
    print("    -> Rendering -> Emulate vision deficiencies -> deuteranopia")


def jalankan(command, params=None, timeout=300):
    body = {"language": "opencypher", "command": command}
    if params:
        body["params"] = params
    r = requests.post(ARCADEDB_URL, json=body, auth=ARCADEDB_AUTH, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"ArcadeDB error {r.status_code}: {r.text[:400]}")
    hasil = r.json()
    batas, n = hasil.get("limit"), len(hasil.get("result") or [])
    if batas and n >= batas:
        raise RuntimeError(
            f"Hasil TERPOTONG di {batas} baris. Persempit cakupan view-nya, "
            f"atau paginasi -- jangan gambar graf sebesar itu.")
    return hasil["result"]


# Pembantu pembentuk node/edge
def node_topik(n, tid, nama, dok, kategori, supergroup=None):
    """Node Topik untuk view gaya lama (yang mengembalikan skalar).

    Sejak kamus gaya dipakai, warna & bentuk TIDAK lagi ditentukan di sini --
    semuanya dari gaya_node(), supaya ekspor statis dan kotak query tidak
    pernah berbeda tampilan. `kategori` tetap disimpan sbg properti untuk
    legenda dan penyorotan, tapi bukan lagi penentu warna: pemetaan
    topik->kategori rata-rata cuma berkeyakinan 0,49.
    """
    # Label dipendekkan ke 2 kata kunci; 4 kata saling menimpa di kanvas.
    # Nama lengkap tetap ada di `title` (tooltip).
    pendek = ", ".join([x.strip() for x in (nama or tid).split(",")][:2])
    n[tid] = {
        "id": tid, "label": pendek, "tipe": "Topik",
        "value": dok or 1,
        "kategori": kategori, "supergroup": supergroup,
        "title": f"{dok} publikasi\n{kategori or '-'}\n{supergroup or '-'}",
    }
    n[tid].update(gaya_node("Topik"))


def node_peneliti(n, pid, nama, keterangan="", ambigu=False):
    n[pid] = {
        "id": pid, "label": nama or pid, "tipe": "Peneliti",
        "font": {"size": 10, "color": "#111827"},
        "title": f"Peneliti{(' · ' + keterangan) if keterangan else ''}",
    }
    n[pid].update(gaya_node("Peneliti"))
    if ambigu:
        # Node yang terbukti berisi lebih dari satu orang: tepi merah tebal,
        # angkanya bukan milik satu individu.
        warna = n[pid]["color"]
        n[pid].update({
            "ambigu": True, "borderWidth": 4,
            "color": {"background": warna, "border": "#B91C1C",
                      "highlight": {"background": warna, "border": "#B91C1C"}},
            "title": f"[AMBIGU] {n[pid]['title']}",
        })


def node_kategori(n, nama, supergroup=None, jumlah=None):
    """Satu-satunya node yang membawa warna ASJC."""
    n[nama] = {
        "id": nama, "label": nama, "tipe": "Kategori",
        "kategori": nama, "supergroup": supergroup,
        "title": f"Kategori ASJC\n{nama}" + (f"\n{jumlah} topik" if jumlah else ""),
    }
    n[nama].update(gaya_node("Kategori", nama))


def edge(dari, ke, label, bobot=None, judul=None):
    """Nama relasi hanya ditulis ke `title`, tidak ke `label`.

    Label di setiap garis membuat gambar jadi gumpalan teks, dan sekali dikirim
    label itu tidak bisa dimatikan lagi di vis-network.
    """
    e = {"from": dari, "to": ke, "jenis": label,
         "value": bobot if bobot else 1,
         "title": judul or (f"{label} · bobot {bobot}" if bobot else label)}
    e.update(gaya_relasi(label))
    return e


# Pembentuk generik untuk kueri yang mengembalikan node dan relasi
# (RETURN p, r, t). Dipakai server.py dan ekspor.
MAKS_LABEL = 46

# Nama properti bobot berbeda per relasi. Tanpa bobot = garis tipis seragam.
#   KOLABORASI/BIDANG_KEAHLIAN/BERAFILIASI -> jumlah
#   SEBIDANG                               -> jumlah_topik_sama (+ skor)
#   MEMBAHAS/TERMASUK_DALAM                -> skor (0..1, dikali 10)
BOBOT_CACAH = ("jumlah", "jumlah_topik_sama")


def bobot_relasi(r):
    for k in BOBOT_CACAH:
        if isinstance(r.get(k), (int, float)):
            return r[k]
    if isinstance(r.get("skor"), (int, float)):
        return round(r["skor"] * 10, 2)     # skor 0..1 -> setara cacah 0..10
    return 1


def label_node(o, tipe):
    for k in LABEL_DARI.get(tipe, ()):
        if o.get(k):
            return str(o[k])
    return str(o.get("nama") or o.get("@rid"))


def bentuk_dari_hasil(rows):
    """Ubah baris hasil Cypher menjadi node dan edge.

    Dua jalur:
    1. Kueri mengembalikan relasinya (`RETURN p, r, t`): garis diambil apa
       adanya, lengkap dengan tipe dan bobot. Selalu lebih baik.
    2. Kueri hanya mengembalikan node (`RETURN p, t`): garis ditebak dari urutan
       kolom. Benar untuk dua kolom, bisa keliru untuk kolom yang lebih banyak.

    Bila hanya satu kolom dikembalikan (`RETURN r`), ArcadeDB meratakan isinya
    ke tingkat baris.
    """
    nodes, edges, dipakai = {}, [], set()
    ditebak = False

    def tambah_node(o):
        rid = o.get("@rid")
        if not rid:
            return None
        tipe = o.get("@type", "")
        label = label_node(o, tipe)
        n = {"id": rid, "label": label[:MAKS_LABEL], "tipe": tipe,
             "title": f"{tipe}\n{label}"}
        n.update(gaya_node(tipe, o.get("nama") if tipe == "Kategori" else None))
        if o.get("ambigu"):
            # Node berisi lebih dari satu orang: tepi merah tebal.
            n["borderWidth"] = 4
            n["color"] = {"background": n["color"], "border": "#B91C1C",
                          "highlight": {"background": n["color"],
                                        "border": "#B91C1C"}}
            n["ambigu"] = True
            n["title"] = f"[AMBIGU] {n['title']}\n{o.get('alasan_ambigu','')}"
        if tipe == "Topik":
            n["value"] = o.get("jumlah_dok") or 1
        if tipe == "Kategori":
            n["kategori"] = o.get("nama")
            n["supergroup"] = o.get("supergroup")
        nodes[rid] = n
        return rid

    def tambah_edge(o):
        a, b = o.get("@out"), o.get("@in")
        if not a or not b:
            return
        jenis = o.get("@type", "")
        kunci = (a, b, jenis)
        if kunci in dipakai:
            return
        dipakai.add(kunci)
        bobot = bobot_relasi(o)
        e = {"from": a, "to": b, "jenis": jenis, "value": bobot,
             "title": f"{jenis} · bobot {bobot}"}
        e.update(gaya_relasi(jenis))
        edges.append(e)

    for baris in rows:
        # baris itu sendiri bisa berupa node/relasi (kasus RETURN satu kolom)
        kandidat = [baris] if baris.get("@cat") else []
        kandidat += [v for k, v in baris.items()
                     if not k.startswith("@") and isinstance(v, dict)
                     and v.get("@cat")]
        ids, ada_relasi = [], False
        for o in kandidat:
            if o.get("@cat") == "v":
                r = tambah_node(o)
                if r:
                    ids.append(r)
            elif o.get("@cat") == "e":
                ada_relasi = True
        # relasi diproses setelah node, supaya ujung-ujungnya sudah ada
        for o in kandidat:
            if o.get("@cat") == "e":
                tambah_edge(o)
        if not ada_relasi:
            ditebak = True
            for a, b in zip(ids, ids[1:]):
                if (a, b, "?") in dipakai:
                    continue
                dipakai.add((a, b, "?"))
                edges.append({"from": a, "to": b, "jenis": "(ditebak)",
                              "value": 1, "arrows": "to", "title": "",
                              "color": {"color": "#E2E8F0"}, "dashes": [2, 4]})

    # Buang garis yang salah satu ujungnya tidak ikut dikembalikan kueri.
    edges = [e for e in edges if e["from"] in nodes and e["to"] in nodes]
    return nodes, edges, ditebak




def bangun_dari_node(rows):
    """Pembungkus 2-tuple untuk dipakai sbg `bangun` di VIEW."""
    nodes, edges, _ = bentuk_dari_hasil(rows)
    return nodes, edges


def bangun_author_network(rows):
    """Jaringan ko-penulisan bergaya VOSviewer: peneliti diwarnai menurut bidangnya.

    Pengecualian sengaja terhadap aturan "hanya Kategori yang membawa warna
    ASJC": di view ini tidak ada node Kategori, jadi warna hanya bermakna satu.

    Ukuran node = jumlah publikasi. Tebal garis = jumlah karya bersama.
    """
    nodes, edges, lihat = {}, [], set()
    ids = {r[k] for r in rows for k in ("a_id", "b_id") if r.get(k)}
    if not ids:
        return nodes, edges

    daftar = ",".join("'" + str(i).replace("'", "\\'") + "'" for i in sorted(ids))
    # kategori dominan = kategori dengan bobot keahlian terbesar
    dominan = {}
    for x in jalankan(
            f"MATCH (p:Peneliti)-[bk:BIDANG_KEAHLIAN]->(:Topik)"
            f"-[:TERMASUK_DALAM]->(k:Kategori {{skema:'ASJC'}}) "
            f"WHERE p.local_id IN [{daftar}] "
            f"WITH p, k, sum(bk.jumlah) AS b ORDER BY b DESC "
            f"RETURN p.local_id AS id, collect(k.nama)[0] AS kat, "
            f"collect(k.supergroup)[0] AS sg"):
        dominan[x["id"]] = (x["kat"], x["sg"])
    jumlah_pub = {x["id"]: x["n"] for x in jalankan(
        f"MATCH (p:Peneliti)-[:MENULIS]->(pub:Publikasi) "
        f"WHERE p.local_id IN [{daftar}] "
        f"RETURN p.local_id AS id, count(pub) AS n")}

    def tambah(pid, nama, ambigu):
        if pid in nodes:
            return
        kat, sg = dominan.get(pid, (None, None))
        n = {"id": pid, "label": nama or pid, "tipe": "Peneliti",
             "shape": "dot", "borderWidth": 1.5,
             "value": jumlah_pub.get(pid, 1),
             "color": WARNA.get(kat, WARNA_PENELITI_BAKU),
             "kategori": kat, "supergroup": sg,
             # memberi tahu legenda bahwa warna ini berasal dari kategori,
             # walau view ini tidak memuat node Kategori
             "warna_dari_kategori": True,
             "font": {"size": 11, "color": "#111827"},
             "title": f"{nama}\n{jumlah_pub.get(pid,0)} publikasi\n"
                      f"{kat or 'kategori tidak diketahui'}"}
        if ambigu:
            n.update({"ambigu": True, "borderWidth": 4,
                      "color": {"background": n["color"], "border": "#B91C1C",
                                "highlight": {"background": n["color"],
                                              "border": "#B91C1C"}},
                      "title": f"[AMBIGU] {n['title']}"})
        nodes[pid] = n

    for r in rows:
        tambah(r["a_id"], r.get("a"), r.get("a_ambigu"))
        tambah(r["b_id"], r.get("b"), r.get("b_ambigu"))
        kunci = tuple(sorted((r["a_id"], r["b_id"])))
        if kunci in lihat:
            continue
        lihat.add(kunci)
        edges.append(edge(r["a_id"], r["b_id"], "KOLABORASI", r.get("bobot"),
                          f"{r.get('bobot')} publikasi bersama"))
    return nodes, edges


# Pustaka view. View baru ditambahkan di sini.
def _tanpa_ganda(nodes, edges):
    """Buang relasi kembar. Satu topik muncul di banyak baris (digarap banyak
    peneliti), jadi TERMASUK_DALAM-nya ikut terbentuk berulang kali."""
    unik, lihat = [], set()
    for e in edges:
        kunci = (e["from"], e["to"], e["jenis"])
        if kunci in lihat:
            continue
        lihat.add(kunci)
        unik.append(e)
    return nodes, unik


def _sambung_organisasi(nodes, edges, r):
    """Sambungkan Institusi -> UnitKerja -> Peneliti bila barisnya membawanya.

    Murah: hanya 6 node untuk seluruh graf (2 institusi, 4 unit kerja), tapi
    memberi konteks organisasi yang selama ini hilang dari gambar. Bandingkan
    dgn Publikasi (5.749 node) dan Venue (3.755) yang sengaja TIDAK diikutkan
    -- keduanya akan membuat gambar ini ~48x lebih besar tanpa menambah apa pun
    yang bisa dibaca mata.
    """
    unit, inst = r.get("unit"), r.get("inst")
    if not unit:
        return
    if unit not in nodes:
        nodes[unit] = {"id": unit, "label": unit, "tipe": "UnitKerja",
                       "title": f"Unit kerja {unit}"}
        nodes[unit].update(gaya_node("UnitKerja"))
    edges.append(edge(unit, r["pid"], "MEMILIKI"))
    if inst:
        if inst not in nodes:
            nodes[inst] = {"id": inst, "label": inst, "tipe": "Institusi",
                           "title": f"Institusi {inst}"}
            nodes[inst].update(gaya_node("Institusi"))
        edges.append(edge(unit, inst, "AFILIASI"))


def _sambung_kategori(nodes, edges, r):
    """Buat node Kategori + relasi TERMASUK_DALAM bila barisnya membawanya.

    WAJIB, bukan hiasan: sejak warna ASJC hanya dipegang node Kategori,
    ekspor tanpa lapisan ini keluar cuma dua warna (teal utk peneliti, abu
    utk topik) -- tidak ada sinyal bidang ilmu sama sekali. Sudah terjadi
    pada ekspor pertama setelah kamus gaya dipakai.
    """
    kat = r.get("kategori")
    if not kat:
        return
    if kat not in nodes:
        node_kategori(nodes, kat, r.get("supergroup"))
    edges.append(edge(r["tid"], kat, "TERMASUK_DALAM"))


def bangun_peneliti_topik(rows, keterangan_dari=None):
    """Bentuk umum: Peneliti -[BIDANG_KEAHLIAN]-> Topik.

    Topiknya TIDAK berwarna kategori lagi -- lihat catatan di kamus gaya.
    Kategorinya tetap tersimpan sbg properti untuk legenda & penyorotan.
    """
    nodes, edges = {}, []
    for r in rows:
        node_peneliti(nodes, r["pid"], r["peneliti"],
                      r.get(keterangan_dari) if keterangan_dari else "",
                      ambigu=bool(r.get("ambigu")))
        node_topik(nodes, r["tid"], r["topik"], r["dok"],
                   r.get("kategori"), r.get("supergroup"))
        edges.append(edge(r["pid"], r["tid"], "BIDANG_KEAHLIAN", r.get("bobot"),
                          f"{r.get('bobot')} publikasi"))
        _sambung_kategori(nodes, edges, r)
        _sambung_organisasi(nodes, edges, r)
    return _tanpa_ganda(nodes, edges)


def bangun_individu(rows):
    """Peneliti -> Publikasi -> Topik. Publikasi ikut digambar supaya terlihat
    LEWAT KARYA APA seorang peneliti tersambung ke sebuah topik."""
    nodes, edges = {}, []
    for r in rows:
        node_peneliti(nodes, r["pid"], r["peneliti"],
                      ambigu=bool(r.get("ambigu")))
        nodes[r["pubid"]] = {
            "id": r["pubid"], "label": (r["judul"] or "")[:60],
            "tipe": "Publikasi", "title": r["judul"] or "",
        }
        nodes[r["pubid"]].update(gaya_node("Publikasi"))
        node_topik(nodes, r["tid"], r["topik"], r["dok"],
                   r.get("kategori"), r.get("supergroup"))
        edges.append(edge(r["pid"], r["pubid"], "MENULIS"))
        edges.append(edge(r["pubid"], r["tid"], "MEMBAHAS"))
        _sambung_kategori(nodes, edges, r)
    return _tanpa_ganda(nodes, edges)


def bangun_kolaborasi(rows):
    nodes, edges = {}, []
    for r in rows:
        node_peneliti(nodes, r["a_id"], r["a"], r.get("a_inst", ""),
                      ambigu=bool(r.get("a_ambigu")))
        node_peneliti(nodes, r["b_id"], r["b"], r.get("b_inst", ""),
                      ambigu=bool(r.get("b_ambigu")))
        edges.append(edge(r["a_id"], r["b_id"], "KOLABORASI", r.get("bobot"),
                          f"{r.get('bobot')} publikasi bersama"))
    return nodes, edges


# Kelompok di daftar pilihan view, menurut pertanyaan yang dijawab.
# Urutan kelompok = urutan tampil.
KELOMPOK = [
    ("Peta jalan pembimbing", ["individu", "unit_kerja", "institusi",
                               "banding_institusi", "semua_internal"]),
    ("Kolaborasi",            ["kolaborasi_2", "jaringan_kolaborasi",
                               "kolaborasi_potensial", "jembatan_unit", "kolaborasi_lintas_institusi"]),
    ("Ontologi & klasifikasi", ["ontologi", "asjc_dua_tingkat",
                                "ontologi_penuh"]),
    ("Analisis jaringan (ANA/CNA)", ["author_network", "ana_lintas_unit",
                                     "ana_terisolasi", "cna_uji",
                                     "cna_pengganti", "sitasi_teratas"]),
    ("Waktu & mutu",          ["kolaborasi_terkini", "topik_naik_daun",
                               "jurnal_bereputasi"]),
    ("Pencarian & profil",    ["pakar_topik", "mirip_dengan", "venue_unit",
                               "peneliti_lintas_disiplin"]),
]

# Nama tampil berbahasa Inggris.
# Hanya untuk tampilan. Kunci (label, relasi, nama view, kelompok) tetap
# Indonesia: prompt chat, penjaga, dan Cypher bergantung padanya.
NAMA_TAMPIL = {
    "tipe": {"Institusi": "Institution", "UnitKerja": "Work unit", "Peneliti": "Researcher",
             "Publikasi": "Publication", "Venue": "Venue", "Topik": "Topic",
             "Kategori": "ASJC category"},
    "warna_asjc": {k: v for k, v in WARNA.items()},
    "warna_netral": WARNA_NETRAL,
    "keluarga": {"struktur": "structure", "kepengarangan": "authorship",
                 "semantik": "semantic", "kemiripan": "similarity"},
    "relasi": {"AFILIASI": "PART_OF", "MEMILIKI": "HAS_MEMBER", "BERAFILIASI": "AFFILIATED_WITH",
               "MENULIS": "WROTE", "TERBIT_DI": "PUBLISHED_IN", "MEMBAHAS": "DISCUSSES",
               "MASUK_KATEGORI": "IN_CATEGORY", "TERMASUK_DALAM": "BELONGS_TO",
               "BIDANG_KEAHLIAN": "EXPERTISE_IN", "KOLABORASI": "CO_AUTHORED",
               "SEBIDANG": "SIMILAR_FIELD", "(ditebak)": "(guessed)"},
    "kelompok": {"Peta jalan pembimbing": "Supervisor's roadmap", "Kolaborasi": "Collaboration",
                 "Ontologi & klasifikasi": "Ontology & classification",
                 "Analisis jaringan (ANA/CNA)": "Network analysis (ANA/CNA)",
                 "Waktu & mutu": "Time & quality", "Pencarian & profil": "Search & profiles",
                 "Lainnya": "Other"},
}

# Judul dan keterangan view dalam bahasa Inggris, termasuk angka dan
# peringatannya. View yang belum ada di sini tampil berbahasa Indonesia
# (--periksa-view memperingatkannya).
VIEW_EN = {
    "individu": ("One researcher: publications and topics",
                 "Personal view: one researcher -> publications -> topics."),
    "unit_kerja": ("All researchers of one work unit and their expertise",
                   "Choose PRSDI (58 researchers), SI (15), PTI (12) or TI (11). At this scale, clusters start to appear."),
    "semua_internal": ("All 96 internal researchers and their expertise",
                       "Institution > work unit > researcher > topic > category. Publications and venues are left out on purpose: they make the picture ~48x larger without adding anything readable."),
    "kolaborasi_2": ("Topic match between two researchers",
                     "Topics of A, of B, and those they SHARE -- shared topics sit in the middle because both pull on them."),
    "jaringan_kolaborasi": ("Collaboration network around one researcher",
                            "CO_AUTHORED is drawn without arrows (a symmetric relation)."),
    "ontologi": ("Layered ontology: publication -> topic -> category",
                 "Sliced by one ASJC category so it stays readable (e.g. 'Computer Science')."),
    "institusi": ("One institution: units, researchers, down to categories",
                  "Enter 'UB' or 'BRIN'. UB has 3 units (SI, PTI, TI); BRIN has PRSDI. Filtered to expertise with >= 3 publications to keep lines readable."),
    "banding_institusi": ("Compare UB and BRIN through shared categories",
                          "Compares the field profiles, not publication counts. Categories used by BOTH institutions are reached from two sides."),
    "pakar_topik": ("Topic experts",
                    "Matches part of a topic name, e.g. 'learning', 'language processing'. Line width = the researcher's publications on that topic. Internal researchers only."),
    "kolaborasi_potensial": ("Similar field but NOT yet co-authored",
                             "The main reason this graph exists: people whose fields overlap but who have never worked together. Enter a unit code: PRSDI, SI, PTI, TI."),
    "jembatan_unit": ("Topics bridging two work units",
                      "Topics worked on by people from TWO different units -- the most natural meeting points for cross-unit collaboration."),
    "kolaborasi_lintas_institusi": ("Collaboration between two institutions",
                                    "Internal researchers from two institutions who have co-authored. As of 16 Sep 2026, UB-BRIN has only 10 pairs, all through one BRIN researcher (Rio Nurtantyana). Compare with 'Similar field but NOT yet co-authored'."),
    "venue_unit": ("Where one work unit publishes",
                   "Map of publication venues (journals vs proceedings). Note: 91.5% of venue names are citation strings, not clean journal names."),
    "ontologi_penuh": ("Full ontology chain: researcher > publication > topic > category",
                       "The chain exactly as stored, with no shortcuts. LARGE: ~5,950 nodes and ~13,870 relations."),
    "author_network": ("Co-authorship network of internal researchers",
                       "VOSviewer style: colour = dominant ASJC category, size = publications, line width = joint publications. Internal-to-internal only (96 people, 638 links). min_bobot: 1 (638 links), 2 (404), 3 (324), 5 (212)."),
    "cna_uji": ("TEST: citation network -- proves the data does not exist",
                "Returns ZERO on purpose. Citation network analysis needs 'publication A cites publication B'; no publication-to-publication relation exists in this data, only citation COUNTS."),
    "cna_pengganti": ("CNA substitute: publications linked by shared topics",
                      "WARNING: this is NOT a citation network. Two publications are linked because they share a topic, not because one cites the other -- always report it as a 'topic similarity network'. Filtered to publications with >= 60 citations."),
    "ana_lintas_unit": ("Collaboration between work units",
                        "Measured: PTI<->SI is by far the strongest (78 pairs), PRSDI<->TI has only 1. The three UB units are tightly linked while PRSDI is nearly separate, despite having the most researchers (58)."),
    "ana_terisolasi": ("Internal researchers without internal co-authors",
                       "Their topics and categories are included to show WHY: a truly separate field, or an overlap that deserves a collaboration nudge. Pair with 'Similar field but NOT yet co-authored'."),
    "sitasi_teratas": ("Most cited publications and their fields",
                       "Citation ANALYSIS, not a citation NETWORK: ranking by citation counts is valid; drawing who cites whom is not possible (see the CNA test view). Filtered to >= 100 citations; the highest has 877."),
    "asjc_dua_tingkat": ("Two ASJC levels: parent category and 4-digit subcategory",
                         "Each topic hangs on ONE parent category and ONE subcategory -- the mapping is single-label."),
    "kolaborasi_terkini": ("Collaboration through recent publications",
                           "Unlike the co-authorship network (all years combined), this follows publications, so it can be filtered by year -- and the publications are drawn as evidence."),
    "topik_naik_daun": ("Rising topics",
                        "Topics whose publications SINCE the chosen year already exceed all earlier years combined -- growth, not size. A minimum of 10 documents blocks tiny topics from looking like spikes."),
    "jurnal_bereputasi": ("Publications in Q1/Q2 journals and their authors",
                          "READ THE LIMIT BEFORE REPORTING: quartiles are known for only 80 of 4,539 venues (1.8%). This is a LOWER BOUND, not a complete count."),
    "mirip_dengan": ("Researchers similar to one person",
                     "Match profiling around ONE researcher: who shares their fields, and WHICH topics make them similar. Most useful: high-scoring pairs who have not co-authored yet."),
    "peneliti_lintas_disiplin": ("Researchers with the broadest fields",
                                 "The PERSON is interdisciplinary, not the topic: every topic maps to exactly one ASJC category, so breadth only appears at researcher level."),
}


def kelompok_view(nama):
    """Nama kelompok sebuah view; 'Lainnya' bila belum didaftarkan."""
    for kel, isi in KELOMPOK:
        if nama in isi:
            return kel
    return "Lainnya"


def urutan_view(nama):
    """Posisi view dalam daftar terkelompok, untuk pengurutan."""
    for i, (_, isi) in enumerate(KELOMPOK):
        if nama in isi:
            return (i, isi.index(nama))
    return (len(KELOMPOK), 0)


# Nilai contoh per nama parameter (`kode` selalu kode unit, `local_id` selalu
# peneliti). Tebakan yang wajar pun bisa gagal diam-diam: $institusi =
# "Universitas Brawijaya" mengembalikan 0 baris karena nodenya bernama "UB".
# Nilainya diambil dari basis data; periksa dengan --periksa-contoh.
CONTOH_PARAM = {
    # Devi Munandar (49 publikasi, 58 kolaborasi): sengaja dari rentang
    # menengah supaya gambarnya terbaca. Peneliti paling produktif membuat
    # view `individu` terlalu padat.
    "local_id":  "PRSDI016",
    "kode":      "PTI",              # dari 4: PRSDI, PTI, SI, TI
    "institusi": "UB",               # hanya ada dua: UB, BRIN
    "a":         "PTIUB002",         # Admaja Dwi Herlambang
    "b":         "PTIUB011",         # + Satrio Hadi Wijoyo, kolaborasi terberat (59)
    "unit_a":    "PTI",
    "unit_b":    "SI",
    "institusi_a": "UB",             # hanya ada dua institusi: UB, BRIN
    "institusi_b": "BRIN",
    "kategori":  "Computer Science", # kategori ASJC dgn topik terbanyak (26)
    "min_bobot": 2,
    "kata":      "learning",         # cocok ke 10 topik
    # 2023-2026 seimbang dengan 2019-2022 untuk membandingkan naik/turun.
    "sejak_tahun": 2023,
}


VIEW = {
    "individu": {
        "judul": "Satu peneliti: karya dan topiknya",
        "params": ["local_id"],
        "catatan": "Tampilan personal (satu orang -> paper -> topik).",
        "cypher": """
MATCH (p:Peneliti {local_id: $local_id})-[:MENULIS]->(pub:Publikasi)
      -[:MEMBAHAS]->(t:Topik)
MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN p.local_id AS pid, p.nama AS peneliti,
       pub.dedup_key AS pubid, pub.judul AS judul,
       t.topik_id AS tid, t.nama AS topik, t.jumlah_dok AS dok,
       k.nama AS kategori, k.supergroup AS supergroup
""",
        "bangun": bangun_individu,
    },

    "unit_kerja": {
        "judul": "Seluruh peneliti satu unit kerja dan bidang keahliannya",
        "params": ["kode"],
        "catatan": "Pilihan: PRSDI (58 peneliti), SI (15), PTI (12), TI (11). "
                   "Inilah cakupan tempat klasterisasi mulai terlihat.",
        "cypher": """
MATCH (u:UnitKerja {kode: $kode})-[:MEMILIKI]->(p:Peneliti)
      -[r:BIDANG_KEAHLIAN]->(t:Topik)
MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN p.local_id AS pid, p.nama AS peneliti,
       t.topik_id AS tid, t.nama AS topik, t.jumlah_dok AS dok,
       k.nama AS kategori, k.supergroup AS supergroup, r.jumlah AS bobot
""",
        "bangun": bangun_peneliti_topik,
    },

    "semua_internal": {
        "judul": "Seluruh 96 peneliti internal dan bidang keahliannya",
        "params": [],
        "catatan": "Peta menyeluruh: Institusi > UnitKerja > Peneliti > Topik "
                   "> Kategori. Publikasi (5.749) dan Venue (3.755) sengaja "
                   "TIDAK diikutkan -- keduanya membuat gambar ini ~48x lebih "
                   "besar tanpa menambah apa pun yang terbaca. Untuk melihat "
                   "keduanya, pakai view 'individu' atau 'venue_unit'.",
        "cypher": """
MATCH (p:Peneliti {is_internal:true})-[r:BIDANG_KEAHLIAN]->(t:Topik)
MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
OPTIONAL MATCH (u)-[:AFILIASI]->(i:Institusi)
RETURN p.local_id AS pid, p.nama AS peneliti, p.institusi AS institusi,
       p.ambigu AS ambigu, u.kode AS unit, i.nama AS inst,
       t.topik_id AS tid, t.nama AS topik, t.jumlah_dok AS dok,
       k.nama AS kategori, k.supergroup AS supergroup, r.jumlah AS bobot
""",
        "bangun": lambda rows: bangun_peneliti_topik(rows, "institusi"),
    },

    "kolaborasi_2": {
        "judul": "Kecocokan topik dua peneliti",
        "params": ["a", "b"],
        "catatan": "Fitur yang pembimbing sebut paling dinanti. Menampilkan "
                   "topik milik A, milik B, dan yang DIPAKAI BERSAMA -- topik "
                   "bersama otomatis berada di tengah karena tertarik keduanya.",
        "cypher": """
MATCH (p:Peneliti)-[r:BIDANG_KEAHLIAN]->(t:Topik)
WHERE p.local_id IN [$a, $b]
MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN p.local_id AS pid, p.nama AS peneliti,
       t.topik_id AS tid, t.nama AS topik, t.jumlah_dok AS dok,
       k.nama AS kategori, k.supergroup AS supergroup, r.jumlah AS bobot
""",
        "bangun": bangun_peneliti_topik,
    },

    "jaringan_kolaborasi": {
        "judul": "Jaringan kolaborasi di sekitar satu peneliti",
        "params": ["local_id"],
        "catatan": "KOLABORASI digambar TANPA panah (relasi simetris).",
        "cypher": """
MATCH (a:Peneliti {local_id: $local_id})-[r:KOLABORASI]-(b:Peneliti)
RETURN a.local_id AS a_id, a.nama AS a, a.institusi AS a_inst,
       b.local_id AS b_id, b.nama AS b, b.institusi AS b_inst,
       r.jumlah AS bobot
""",
        "bangun": bangun_kolaborasi,
    },

    # View yang mengembalikan node dan relasi (RETURN p, r, t).
    # Lima view di atas mengembalikan nilai skalar dan punya pembangun
    # sendiri. View di bawah ini memakai bangun_dari_node(), sehingga tipe dan
    # bobot garis dibaca langsung dari relasinya.

    "ontologi": {
        "judul": "Ontologi bertingkat: Publikasi -> Topik -> Kategori",
        "params": ["kategori"],
        "catatan": "Menjawab poin 4 pembimbing. Diiris per kategori supaya "
                   "terbaca -- seluruh 6.607 publikasi sekaligus tidak ada "
                   "gunanya dilihat. Contoh: 'Computer Science'.",
        "cypher": """
MATCH (pub:Publikasi)-[mb:MEMBAHAS]->(t:Topik)
MATCH (t)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC', nama: $kategori})
RETURN pub, mb, t, td, k LIMIT 120
""",
        "bangun": bangun_dari_node,
    },

    "institusi": {
        "judul": "Satu institusi: unit kerja, peneliti, sampai kategori",
        "params": ["institusi"],
        "catatan": "Langkah 3 peta jalan (skala institusi). Isi 'UB' atau "
                   "'BRIN'. UB punya 3 unit (SI, PTI, TI), BRIN punya PRSDI. "
                   "Disaring bk.jumlah >= 3 supaya garisnya terbaca; turunkan "
                   "ke 1 di kotak query kalau ingin gambaran utuh.",
        "cypher": """
MATCH (u:UnitKerja)-[af:AFILIASI]->(i:Institusi {nama: $institusi})
MATCH (u)-[mk:MEMILIKI]->(p:Peneliti)-[bk:BIDANG_KEAHLIAN]->(t:Topik)
WHERE bk.jumlah >= 3
MATCH (t)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN i, af, u, mk, p, bk, t, td, k LIMIT 3000
""",
        "bangun": bangun_dari_node,
    },

    "banding_institusi": {
        "judul": "Bandingkan UB dan BRIN lewat kategori bersama",
        "params": [],
        "catatan": "Langkah 4 peta jalan. Publikasi sengaja TIDAK ditarik: "
                   "yang dibandingkan profil bidangnya, bukan jumlah karyanya. "
                   "Kategori yang dipakai KEDUA institusi akan tampak sbg "
                   "simpul yang dituju dari dua sisi. Peneliti WAJIB ikut "
                   "dikembalikan -- tanpa dia, UnitKerja dan Topik tidak "
                   "punya penghubung dan grafnya pecah jadi dua bagian.\n"
                   "DISARING bk.jumlah >= 5. Tanpa saringan hasilnya 202 node "
                   "dengan 2.330 garis -- sudah diuji di browser dan benar-"
                   "benar tak terbaca, menyorot kategori pun tidak menolong "
                   "karena garisnya yang menutupi. Ambang 5 bukan angka asal: "
                   "914 dari 2.149 relasi hanya berbobot 1 (satu publikasi), "
                   "yang lebih tepat disebut singgungan daripada bidang "
                   "keahlian.",
        "cypher": """
MATCH (u:UnitKerja)-[af:AFILIASI]->(i:Institusi)
MATCH (u)-[mk:MEMILIKI]->(p:Peneliti)-[bk:BIDANG_KEAHLIAN]->(t:Topik)
WHERE bk.jumlah >= 5
MATCH (t)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN i, af, u, mk, p, bk, t, td, k LIMIT 3000
""",
        "bangun": bangun_dari_node,
    },

    "pakar_topik": {
        "judul": "Siapa yang menguasai bidang tertentu",
        "params": ["kata"],
        "catatan": "Pencarian pakar. "
                   "Cocokkan potongan kata pada nama topik, "                   
                   "mis. 'language processing', 'pertanian', 'keamanan'. "
                   "Tebal garis = jumlah publikasi peneliti itu di topik tsb. "
                   "Dibatasi peneliti INTERNAL: tanpa itu satu topik bisa "
                   "menarik 150 nama, hampir semuanya penulis eksternal.",
        "cypher": """
MATCH (p:Peneliti {is_internal: true})-[r:BIDANG_KEAHLIAN]->(t:Topik)
WHERE toLower(t.nama) CONTAINS toLower($kata)
MATCH (t)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN p, r, t, td, k ORDER BY r.jumlah DESC LIMIT 150
""",
        "bangun": bangun_dari_node,
    },

    "kolaborasi_potensial": {
        "judul": "Sebidang tapi BELUM pernah berkolaborasi",
        "params": ["kode"],
        "catatan": "Inilah yang tidak bisa dijawab pencarian teks biasa, dan "
                   "alasan utama graf ini dibangun: menemukan orang yang "
                   "bidangnya beririsan tapi belum pernah bekerja sama. "
                   "Isi kode unit: PRSDI, SI, PTI, TI.",
        "cypher": """
MATCH (u:UnitKerja {kode: $kode})-[:MEMILIKI]->(a:Peneliti)
MATCH (a)-[r:SEBIDANG]-(b:Peneliti)
WHERE NOT (a)-[:KOLABORASI]-(b)
RETURN a, r, b LIMIT 300
""",
        "bangun": bangun_dari_node,
    },

    "jembatan_unit": {
        "judul": "Topik yang menjembatani dua unit kerja",
        "params": ["unit_a", "unit_b"],
        "catatan": "Topik yang dikerjakan orang dari DUA unit berbeda -- "
                   "titik temu paling masuk akal untuk kolaborasi lintas unit. "
                   "Contoh: unit_a=SI, unit_b=PRSDI.",
        "cypher": """
MATCH (:UnitKerja {kode: $unit_a})-[:MEMILIKI]->(a:Peneliti)
      -[r1:BIDANG_KEAHLIAN]->(t:Topik)
MATCH (t)<-[r2:BIDANG_KEAHLIAN]-(b:Peneliti)
      <-[:MEMILIKI]-(:UnitKerja {kode: $unit_b})
RETURN a, r1, t, r2, b LIMIT 150
""",
        "bangun": bangun_dari_node,
    },
    "kolaborasi_lintas_institusi": {
        "judul": "Kolaborasi antar dua institusi",
        "params": ["institusi_a", "institusi_b"],
        "catatan": "Peneliti internal dari dua institusi yang pernah menulis "
                   "bersama, lengkap dgn unit kerjanya. Per 16 Sep 2026 UB-BRIN "
                   "hanya 10 pasangan dan semuanya lewat satu orang BRIN (Rio "
                   "Nurtantyana) -- bandingkan dgn kolaborasi_potensial untuk "
                   "melihat peluang yang belum tergarap. Chat AI belum andal "
                   "untuk pertanyaan ini; pakai view ini.",
        "cypher": """
MATCH (ia:Institusi {nama: $institusi_a})<-[fa:AFILIASI]-(ua:UnitKerja)-[ma:MEMILIKI]->(a:Peneliti)
MATCH (a)-[r:KOLABORASI]-(b:Peneliti)<-[mb:MEMILIKI]-(ub:UnitKerja)-[fb:AFILIASI]->(ib:Institusi {nama: $institusi_b})
RETURN ia, fa, ua, ma, a, r, b, mb, ub, fb, ib LIMIT 300
""",
        "bangun": bangun_dari_node,
    },

    "venue_unit": {
        "judul": "Ke mana satu unit kerja menerbitkan karyanya",
        "params": ["kode"],
        "catatan": "Peta tempat terbit. Berguna untuk melihat sebaran jurnal "
                   "vs prosiding. Catatan: 496 venue palsu (isinya daftar "
                   "penulis) sudah dibuang, jadi angkanya kini bisa dipercaya.",
        "cypher": """
MATCH (:UnitKerja {kode: $kode})-[:MEMILIKI]->(p:Peneliti)
MATCH (p)-[w:MENULIS]->(pub:Publikasi)-[tb:TERBIT_DI]->(v:Venue)
RETURN p, w, pub, tb, v LIMIT 200
""",
        "bangun": bangun_dari_node,
    },

    "ontologi_penuh": {
        "judul": "Rantai ontologi utuh: Peneliti > Publikasi > Topik > Kategori",
        "params": [],
        "catatan": "Rantai APA ADANYA sesuai ontologi di ArcadeDB, tanpa "
                   "jalan pintas. Perlu diingat BIDANG_KEAHLIAN yang dipakai "
                   "view lain adalah relasi TURUNAN -- diringkas dari jalur "
                   "ini. Di sini Publikasi tidak dilewati.\n"
                   "BESAR: ~5.950 node, ~13.870 relasi. Ukur dulu sanggup "
                   "atau tidak sebelum dipakai untuk gambar laporan.",
        "cypher": """
MATCH (p:Peneliti {is_internal:true})-[w:MENULIS]->(pub:Publikasi)
MATCH (pub)-[mb:MEMBAHAS]->(t:Topik)
MATCH (t)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
OPTIONAL MATCH (u:UnitKerja)-[mk:MEMILIKI]->(p)
OPTIONAL MATCH (u)-[af:AFILIASI]->(i:Institusi)
RETURN p, w, pub, mb, t, td, k, u, mk, i, af
""",
        "bangun": bangun_dari_node,
    },

    "author_network": {
        "judul": "Jaringan ko-penulisan antar peneliti internal",
        "params": ["min_bobot"],
        "catatan": "Gaya VOSviewer: warna = kategori ASJC dominan orang itu, "
                   "ukuran = jumlah publikasi, tebal garis = jumlah karya "
                   "bersama. Cakupannya internal<->internal (96 orang, 638 "
                   "relasi) -- menyertakan rekan eksternal membengkakkannya "
                   "jadi 4.910 relasi dan tidak terbaca.\n"
                   "Isi min_bobot: 1 (semua, 638), 2 (404), 3 (324), 5 (212). "
                   "Mulai dari 2 kalau ingin langsung terbaca.\n"
                   "Catatan: di sini peneliti BERWARNA menurut bidangnya -- "
                   "pengecualian sadar dari kamus gaya, sah karena tidak ada "
                   "node Kategori di layar sehingga warna hanya mengemban "
                   "satu makna.",
        "cypher": """
MATCH (a:Peneliti {is_internal:true})-[r:KOLABORASI]->(b:Peneliti {is_internal:true})
WHERE r.jumlah >= $min_bobot
RETURN a.local_id AS a_id, a.nama AS a, a.ambigu AS a_ambigu,
       b.local_id AS b_id, b.nama AS b, b.ambigu AS b_ambigu,
       r.jumlah AS bobot
""",
        "bangun": bangun_author_network,
    },

    # Analisis jaringan akademik.

    "cna_uji": {
        "judul": "UJI Citation Network — membuktikan datanya tidak ada",
        "params": [],
        "catatan": "SENGAJA mengembalikan NOL. Ini bukan view rusak, melainkan "
                   "BUKTI. Citation Network Analysis menuntut relasi "
                   "'publikasi A mengutip publikasi B'; kueri ini mencari "
                   "relasi APA PUN antar-publikasi dan tidak menemukan satu "
                   "pun.\n"
                   "Yang dimiliki hanya ANGKA sitasi (6.495 publikasi, total "
                   "41.043, tertinggi 877) -- angka itu memberi tahu sebuah "
                   "makalah dikutip 877 kali, tapi TIDAK memberi tahu oleh "
                   "siapa. Jalankan view ini di depan pembimbing untuk "
                   "menunjukkan keterbatasannya secara langsung.",
        "cypher": """
MATCH (a:Publikasi)-[r]->(b:Publikasi)
RETURN a, r, b LIMIT 50
""",
        "bangun": bangun_dari_node,
    },

    "cna_pengganti": {
        "judul": "Pengganti CNA: publikasi terhubung lewat topik bersama",
        "params": [],
        "catatan": "PERINGATAN: ini BUKAN citation network. Dua publikasi "
                   "terhubung karena membahas topik yang sama, BUKAN karena "
                   "saling mengutip. Bentuk grafnya mirip "
                   "(Publikasi-Publikasi) sehingga mudah tertukar saat "
                   "melaporkan -- sebut selalu sebagai 'jaringan kesamaan "
                   "topik'.\n"
                   "Disaring ke publikasi berdampak (>=60 sitasi); tanpa "
                   "saringan ada 355.934 pasangan.",
        "cypher": """
MATCH (a:Publikasi)-[m1:MEMBAHAS]->(t:Topik)<-[m2:MEMBAHAS]-(b:Publikasi)
WHERE a.dedup_key < b.dedup_key AND a.sitasi >= 60 AND b.sitasi >= 60
RETURN a, m1, t, m2, b LIMIT 80
""",
        "bangun": bangun_dari_node,
    },

    "ana_lintas_unit": {
        "judul": "Kolaborasi antar unit kerja",
        "params": [],
        "catatan": "Menjawab langkah 4 dari sisi kolaborasi, bukan bidang "
                   "ilmu. Terukur: PTI<->SI 78 pasangan (bobot 480) jauh "
                   "terkuat, sedangkan PRSDI<->TI hanya 1 pasangan. Ketiga "
                   "unit UB rapat satu sama lain sementara PRSDI nyaris "
                   "terpisah -- padahal PRSDI punya peneliti terbanyak (58). "
                   "Itu temuan yang layak dibahas, bukan sekadar gambar.",
        "cypher": """
MATCH (u1:UnitKerja)-[mk1:MEMILIKI]->(a:Peneliti)-[r:KOLABORASI]->(b:Peneliti)
      <-[mk2:MEMILIKI]-(u2:UnitKerja)
WHERE u1.kode <> u2.kode
RETURN u1, mk1, a, r, b, mk2, u2 LIMIT 300
""",
        "bangun": bangun_dari_node,
    },

    "ana_terisolasi": {
        "judul": "Peneliti internal yang belum berkolaborasi di dalam",
        "params": [],
        "catatan": "Tiga peneliti internal belum pernah menulis bersama "
                   "sesama internal. Topik dan kategorinya ikut ditarik "
                   "supaya terlihat MENGAPA -- apakah bidangnya memang "
                   "menyendiri, atau justru beririsan dengan orang lain "
                   "sehingga layak didorong berkolaborasi. Pasangkan dgn "
                   "view kolaborasi_potensial. "
                   "Profil yang tercampur karya orang lain bernama sama bisa "
                   "tampak menyendiri -- periksa dulu sebelum menyimpulkan.",
        "cypher": """
MATCH (p:Peneliti {is_internal:true})
OPTIONAL MATCH (p)-[k:KOLABORASI]-(q:Peneliti {is_internal:true})
WITH p, count(k) AS n WHERE n = 0
MATCH (p)-[bk:BIDANG_KEAHLIAN]->(t:Topik)-[td:TERMASUK_DALAM]->(kat:Kategori {skema:'ASJC'})
RETURN p, bk, t, td, kat
""",
        "bangun": bangun_dari_node,
    },

    "sitasi_teratas": {
        "judul": "Publikasi paling banyak dikutip dan bidangnya",
        "params": [],
        "catatan": "Ini ANALISIS SITASI, bukan analisis JARINGAN sitasi -- "
                   "bedanya penting saat melaporkan. Peringkat berdasarkan "
                   "jumlah kutipan sah dilakukan tanpa tahu siapa yang "
                   "mengutip; yang tidak bisa hanyalah menggambar jaringannya "
                   "(lihat view cna_uji).\n"
                   "Disaring >=100 sitasi. Tertinggi 877.",
        "cypher": """
MATCH (p:Publikasi) WHERE p.sitasi >= 100
MATCH (p)-[m:MEMBAHAS]->(t:Topik)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN p, m, t, td, k LIMIT 120
""",
        "bangun": bangun_dari_node,
    },

    "asjc_dua_tingkat": {
        "judul": "Dua tingkat ASJC: kategori induk dan subkategori 4-digit",
        "params": [],
        "catatan": "Memperlihatkan lapisan 4-digit yang dibangun dari Scopus "
                   "Source List. Tiap topik menggantung pada SATU kategori "
                   "induk dan SATU subkategori -- pemetaannya memang berlabel "
                   "tunggal, bukan ganda.",
        "cypher": """
MATCH (t:Topik)-[a:TERMASUK_DALAM]->(k27:Kategori {skema:'ASJC'})
MATCH (t)-[b:TERMASUK_DALAM]->(k4:Kategori {skema:'ASJC-4'})
RETURN t, a, k27, b, k4 LIMIT 200
""",
        "bangun": bangun_dari_node,
    },

    # Dimensi waktu dan mutu.
    # `tahun` terisi pada 98,9% publikasi (6.537 dari 6.607).
    "kolaborasi_terkini": {
        "judul": "Kolaborasi lewat publikasi terbaru",
        "params": ["sejak_tahun"],
        "catatan": "Bedanya dgn author_network: yang itu memakai relasi "
                   "KOLABORASI yang TIDAK menyimpan tahun, jadi ia menjumlah "
                   "seluruh sejarah. View ini menelusuri lewat publikasinya, "
                   "sehingga bisa disaring waktu -- dan publikasinya IKUT "
                   "digambar sbg bukti, bukan cuma garis antarorang.\n"
                   "Terukur sejak 2023: 1.752 pasangan-publikasi. Dibatasi 400 "
                   "baris; naikkan $sejak_tahun untuk mempersempit, jangan "
                   "menaikkan LIMIT -- yang bikin sesak adalah garisnya.",
        "cypher": """
MATCH (a:Peneliti {is_internal:true})-[ma:MENULIS]->(pub:Publikasi)
      <-[mb:MENULIS]-(b:Peneliti {is_internal:true})
WHERE pub.tahun >= $sejak_tahun AND a.local_id < b.local_id
RETURN a, ma, pub, mb, b LIMIT 400
""",
        "bangun": bangun_dari_node,
    },

    "topik_naik_daun": {
        "judul": "Topik yang sedang menanjak",
        "params": ["sejak_tahun"],
        "catatan": "Topik yang jumlah publikasinya SEJAK $sejak_tahun sudah "
                   "melampaui seluruh tahun sebelumnya digabung -- pertumbuhan, "
                   "bukan sekadar ukuran. Ambang >=10 dokumen memblokir topik "
                   "kecil yang naik dari 1 ke 2 dan terlihat seperti lonjakan.\n"
                   "Terukur pada 2023: 8 topik lolos. Terkuat 'intelligence "
                   "technology, teknologi kecerdasan' -- 15 dokumen sebelum "
                   "2023, 51 sesudahnya.\n"
                   "Peneliti yang berkeahlian di topik itu ikut digambar, jadi "
                   "terbaca SIAPA yang menggerakkannya.",
        "cypher": """
MATCH (t:Topik)<-[:MEMBAHAS]-(pub:Publikasi) WHERE pub.tahun IS NOT NULL
WITH t, sum(CASE WHEN pub.tahun >= $sejak_tahun THEN 1 ELSE 0 END) AS baru,
        sum(CASE WHEN pub.tahun < $sejak_tahun THEN 1 ELSE 0 END) AS lama
WHERE baru > lama AND baru >= 10
MATCH (t)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
MATCH (p:Peneliti {is_internal:true})-[bk:BIDANG_KEAHLIAN]->(t)
RETURN t, td, k, p, bk LIMIT 300
""",
        "bangun": bangun_dari_node,
    },

    "jurnal_bereputasi": {
        "judul": "Publikasi di jurnal Q1/Q2 dan penulisnya",
        "params": [],
        "catatan": "Satu-satunya view yang memakai `kuartil`. BACA BATASNYA "
                   "SEBELUM MELAPORKAN: kuartil hanya terisi pada 80 dari 4.539 "
                   "venue (1,8%), jadi ini BUKAN 'semua terbitan bermutu' -- ini "
                   "'terbitan bermutu yang venue-nya sempat terlabeli'. Angka di "
                   "sini adalah batas bawah, bukan hitungan lengkap.\n"
                   "Terukur: 89 publikasi (39 Q1, 50 Q2) di 51 venue, ditulis 62 "
                   "dari 96 peneliti internal (65%). 170 baris, tidak terpotong.",
        "cypher": """
MATCH (p:Peneliti {is_internal:true})-[m:MENULIS]->(pub:Publikasi)-[tb:TERBIT_DI]->(v:Venue)
WHERE v.kuartil IN ['Q1','Q2']
MATCH (pub)-[mb:MEMBAHAS]->(t:Topik)-[td:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
RETURN p, m, pub, tb, v, mb, t, td, k LIMIT 400
""",
        "bangun": bangun_dari_node,
    },

    # Match profiling.
    # kolaborasi_potensial bekerja per unit dan kolaborasi_2 per pasangan.
    # mirip_dengan menjawab "siapa yang mirip dengan orang ini".
    "mirip_dengan": {
        "judul": "Peneliti yang mirip dengan satu orang",
        "params": ["local_id"],
        "catatan": "Match profiling berpusat pada SATU peneliti: siapa saja yang "
                   "berbagi bidang dengannya, dan TOPIK APA yang membuat mereka "
                   "mirip -- topiknya ikut digambar sbg penjelas, bukan cuma "
                   "garis antarorang.\n"
                   "Yang paling berguna adalah pasangan berskor tinggi yang "
                   "BELUM pernah berkolaborasi. Terukur untuk PRSDI016: Rezzy "
                   "Eko Caraka berskor tertinggi (0,67, 8 topik sama) dan belum "
                   "pernah menulis bersama -- itu calon kolaborasi, bukan "
                   "sekadar kemiripan.\n"
                   "BACA BATASNYA: SEBIDANG dihitung dari topik BERTopic, jadi "
                   "ia mengukur kemiripan BAHASA abstrak, bukan kesamaan "
                   "masalah penelitian. Rata-rata peneliti internal punya 24 "
                   "kembaran bidang (paling sedikit 0, paling banyak 56), jadi "
                   "angka besar di sini biasa saja -- yang bermakna adalah "
                   "peringkatnya, bukan ada-tidaknya.",
        "cypher": """
MATCH (p:Peneliti {local_id: $local_id})-[s:SEBIDANG]-(lain:Peneliti)
MATCH (p)-[ba:BIDANG_KEAHLIAN]->(t:Topik)<-[bb:BIDANG_KEAHLIAN]-(lain)
RETURN p, ba, t, bb, lain, s LIMIT 300
""",
        "bangun": bangun_dari_node,
    },

    "peneliti_lintas_disiplin": {
        "judul": "Peneliti yang bidangnya paling melebar",
        "params": [],
        "catatan": "CATATAN PENTING: yang lintas disiplin adalah ORANGNYA, "
                   "bukan topiknya. Setiap topik dipetakan ke tepat satu "
                   "kategori ASJC -- klasifikasinya berlabel tunggal, jadi "
                   "'topik lintas kategori' tidak ada sama sekali. Keluasan "
                   "baru muncul di tingkat peneliti, yang menggarap banyak "
                   "topik dari kategori berbeda.\n"
                   "Sebaran sebenarnya (96 peneliti internal): 1 orang "
                   "menyentuh 13 kategori, 4 orang 12, dan menurun sampai 10 "
                   "orang yang hanya 2. Ambang di bawah dipasang 10 supaya "
                   "gambarnya terbaca; turunkan di kotak query kalau ingin "
                   "cakupan lebih luas.",
        "cypher": """
MATCH (p:Peneliti {is_internal:true})-[:BIDANG_KEAHLIAN]->(:Topik)
      -[:TERMASUK_DALAM]->(k:Kategori {skema:'ASJC'})
WITH p, count(DISTINCT k) AS lebar WHERE lebar >= 10
MATCH (p)-[bk:BIDANG_KEAHLIAN]->(t:Topik)
      -[td:TERMASUK_DALAM]->(k2:Kategori {skema:'ASJC'})
RETURN p, bk, t, td, k2 LIMIT 900
""",
        "bangun": bangun_dari_node,
    },
}


def ke_vis(nodes, edges):
    return {"nodes": list(nodes.values()), "edges": edges}


def ke_gephi(nodes, edges, dasar):
    """Dua CSV siap impor Gephi: <dasar>_node.csv dan <dasar>_edge.csv.

    Gephi hanya dipakai untuk tata letak (ForceAtlas2 dengan Prevent Overlap dan
    Label Adjust). Yang diambil kembali hanya koordinat x,y.
    Nama kolom Id/Label dan Source/Target/Weight wajib berhuruf besar di awal.
    """
    import csv

    # Ukuran diskalakan ke rentang gambar akhir (12-45 px) supaya
    # 'Prevent Overlap' di Gephi menyisakan ruang yang sesuai.
    mentah = [n["value"] for n in nodes.values() if n.get("value")]
    lo, hi = (min(mentah), max(mentah)) if mentah else (1, 1)

    def ukuran(n):
        if not n.get("value"):
            return n.get("size", 12)          # sudah dalam satuan piksel
        if hi == lo:
            return 28
        return round(12 + (n["value"] - lo) * 33 / (hi - lo))

    fn = f"{dasar}_node.csv"
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Id", "Label", "tipe", "kategori", "supergroup", "warna", "ukuran"])
        for n in nodes.values():
            w.writerow([n["id"], n.get("label", ""), n.get("tipe", ""),
                        n.get("kategori", ""), n.get("supergroup", ""),
                        n.get("color", ""), ukuran(n)])
    fe = f"{dasar}_edge.csv"
    with open(fe, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Source", "Target", "Weight", "Type", "jenis"])
        for e in edges:
            # relasi simetris harus Undirected agar metrik komunitas Gephi benar
            arah = "Undirected" if e.get("jenis") in TAK_BERARAH else "Directed"
            w.writerow([e["from"], e["to"], e.get("value", 1), arah, e.get("jenis", "")])
    return fn, fe


def ke_cytoscape(nodes, edges):
    """Format Cytoscape.js: data saja, gaya ditentukan stylesheet halaman.

    Properti khas vis dibuang atau diganti:
        color:{color,highlight}  -> `warna` (string datar)
        dashes:true              -> `berarah:false`
        shape:'dot'              -> tidak dikirim

    Yang dikirim untuk selector:
        node: tipe, kategori, supergroup, bobot, warna, ukuran
        edge: jenis, keluarga, bobot, berarah, warna
    """
    keluar_node = []
    for n in nodes.values():
        # Warna bisa berupa string atau objek {background, border, ...};
        # bentuk objek dipakai node ambigu (tepi merah).
        w = n.get("color")
        if isinstance(w, dict):
            warna = w.get("background") or WARNA_LAIN
            tepi = w.get("border")
        else:
            warna, tepi = (w if isinstance(w, str) else WARNA_LAIN), None
        d = {
            "id": n["id"],
            "label": n.get("label", ""),
            "tipe": n.get("tipe", ""),
            "warna": warna,
            # satu medan ukuran saja: `value` (Topik) & `size` (tetap) disatukan
            "ukuran": n.get("value") or n.get("size") or 12,
        }
        if tepi:
            d["warna_tepi"] = tepi
        for k in ("kategori", "supergroup", "komunitas", "ambigu",
                  "warna_dari_kategori", "asjc_dominan", "warna_asjc"):
            if n.get(k):
                d[k] = n[k]
        if n.get("value"):
            d["bobot"] = n["value"]
        butir = {"data": d}
        # Koordinat Gephi diletakkan sejajar `data`, bukan di dalamnya;
        # di dalam `data` ia diabaikan tanpa peringatan.
        if n.get("x") is not None and n.get("y") is not None:
            butir["position"] = {"x": n["x"], "y": n["y"]}
        keluar_node.append(butir)

    keluar_edge = []
    for i, e in enumerate(edges):
        jenis = e.get("jenis", "")
        keluarga, warna = GAYA_RELASI.get(jenis, ("struktur", WARNA_LAIN))
        keluar_edge.append({"data": {
            "id": f"e{i}",
            "source": e["from"],
            "target": e["to"],
            "jenis": jenis,
            "keluarga": keluarga,
            "warna": warna,
            "bobot": e.get("value", 1),
            # relasi simetris (KOLABORASI, SEBIDANG) -> tanpa panah
            "berarah": jenis not in TAK_BERARAH,
        }})
    return {"nodes": keluar_node, "edges": keluar_edge}


def periksa_contoh():
    """Uji tiap nilai CONTOH_PARAM ke basis data.

    Nilai contoh membusuk diam-diam: node bisa digabung atau dihapus, dan
    contoh yang menunjuk node hilang akan mengembalikan 0 baris -- persis
    seperti view yang rusak. Pemeriksaan ini membuat pembusukan itu berisik.
    """
    UJI = {
        "local_id":  "MATCH (p:Peneliti {local_id:$v}) RETURN p.nama AS n",
        "a":         "MATCH (p:Peneliti {local_id:$v}) RETURN p.nama AS n",
        "b":         "MATCH (p:Peneliti {local_id:$v}) RETURN p.nama AS n",
        "kode":      "MATCH (u:UnitKerja {kode:$v}) RETURN u.kode AS n",
        "unit_a":    "MATCH (u:UnitKerja {kode:$v}) RETURN u.kode AS n",
        "unit_b":    "MATCH (u:UnitKerja {kode:$v}) RETURN u.kode AS n",
        "institusi": "MATCH (i:Institusi {nama:$v}) RETURN i.nama AS n",
        "institusi_a": "MATCH (i:Institusi {nama:$v}) RETURN i.nama AS n",
        "institusi_b": "MATCH (i:Institusi {nama:$v}) RETURN i.nama AS n",
        "kategori":  "MATCH (k:Kategori {skema:'ASJC', nama:$v}) RETURN k.nama AS n",
        "kata":      "MATCH (t:Topik) WHERE t.nama CONTAINS $v "
                     "RETURN count(t) AS n",
    }
    print("Periksa nilai contoh parameter:\n")
    gagal = 0
    for nama, nilai in CONTOH_PARAM.items():
        if nama not in UJI:
            # Angka murni tidak perlu diuji. Teks yang tidak terdaftar di UJI
            # berarti lupa ditambahkan, jadi disebutkan.
            jenis = "angka, dilewati" if isinstance(nilai, (int, float)) \
                    else "TEKS BELUM DIUJI -- tambahkan ke UJI"
            print(f"  {nama:11} {str(nilai):22} ({jenis})")
            continue
        hasil = jalankan(UJI[nama], {"v": nilai})
        ada = bool(hasil) and hasil[0].get("n") not in (None, 0)
        gagal += not ada
        tanda = "ok  " if ada else "HILANG"
        ket = f"-> {hasil[0]['n']}" if ada else "tidak ada di basis data"
        print(f"  {tanda} {nama:11} {str(nilai):22} {ket}")
    print()
    if gagal:
        raise SystemExit(f"{gagal} nilai contoh sudah tidak sah. Perbarui "
                         f"CONTOH_PARAM di scripts/ekspor_visual.py.")
    print("  Semua nilai contoh sah.")


def periksa_view(jalan=True):
    """Periksa seluruh VIEW: bentuknya, lalu jalankan sungguhan.

    View yang salah tidak melempar galat; ia mengembalikan 0 baris. Tiga
    penyebab yang pernah terjadi:
      1. `params` tidak cocok dengan $penanda di Cypher.
      2. Parameter tanpa nilai contoh.
      3. Nilai contoh salah isi ($institusi = "Universitas Brawijaya",
         padahal nodenya bernama "UB").

    Setiap view dijalankan dan jumlah barisnya dilaporkan. View yang memang
    seharusnya kosong (cna_uji) dikecualikan.
    """
    # cna_uji membuktikan tidak ada relasi Publikasi->Publikasi:
    # 0 baris adalah hasil yang benar.
    BOLEH_KOSONG = {"cna_uji"}

    print(f"Periksa {len(VIEW)} view\n")
    salah, waswas = [], []
    terdaftar = {n for _, isi in KELOMPOK for n in isi}
    for nama in VIEW:
        if nama not in VIEW_EN:
            waswas.append(f"{nama}: belum ada di VIEW_EN -- judul tampil dlm bahasa Indonesia")

    for nama, v in VIEW.items():
        for medan in ("judul", "cypher", "params"):
            if medan not in v:
                salah.append(f"{nama}: medan '{medan}' tidak ada")
        if "bangun" not in v:
            salah.append(f"{nama}: tidak punya 'bangun'")
        if not v.get("catatan"):
            waswas.append(f"{nama}: tanpa catatan -- panel keterangan akan kosong")
        if nama not in terdaftar:
            waswas.append(f"{nama}: belum ada di KELOMPOK, akan masuk 'Lainnya'")

        di_cypher = set(re.findall(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", v.get("cypher", "")))
        didaftar = set(v.get("params", []))
        for p in di_cypher - didaftar:
            salah.append(f"{nama}: $%s dipakai di Cypher tapi tidak ada di params" % p)
        for p in didaftar - di_cypher:
            salah.append(f"{nama}: params punya '{p}' tapi Cypher tidak memakainya")
        for p in didaftar & di_cypher:
            if p not in CONTOH_PARAM:
                salah.append(f"{nama}: parameter '{p}' tidak punya nilai contoh")

    if not jalan:
        print("  (tidak dijalankan -- hanya periksa bentuk)")
    else:
        lebar = max(len(n) for n in VIEW)
        for nama, v in VIEW.items():
            par = {p: CONTOH_PARAM[p] for p in v["params"] if p in CONTOH_PARAM}
            if len(par) != len(v["params"]):
                print(f"  {'lewat':7} {nama:{lebar}}  parameter tak lengkap")
                continue
            try:
                baris = len(jalankan(v["cypher"], par or None))
            except Exception as e:
                salah.append(f"{nama}: gagal dijalankan -- {str(e)[:120]}")
                print(f"  {'GAGAL':7} {nama:{lebar}}  {str(e)[:60]}")
                continue
            if baris == 0 and nama not in BOLEH_KOSONG:
                salah.append(f"{nama}: 0 baris (periksa nilai contoh & Cypher-nya)")
                tanda = "KOSONG"
            elif baris == 0:
                tanda = "kosong"          # memang begitu seharusnya
            else:
                tanda = "ok"
            ket = " (memang seharusnya kosong)" if nama in BOLEH_KOSONG else ""
            print(f"  {tanda:7} {nama:{lebar}}  {baris:6} baris{ket}")

    print()
    for w in waswas:
        print(f"  catatan: {w}")
    if salah:
        print()
        for x in salah:
            print(f"  MASALAH: {x}")
        raise SystemExit(f"\n{len(salah)} masalah ditemukan.")
    print(f"  {len(VIEW)} view sehat.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daftar", action="store_true", help="tampilkan view yang tersedia")
    ap.add_argument("--palet", action="store_true",
                    help="tulis halaman contoh warna ke visual/palet.html")
    ap.add_argument("--view", help="nama view")
    ap.add_argument("--format", choices=["vis", "cytoscape", "gephi"], default="vis")
    ap.add_argument("--keluaran", default=KELUARAN_BAKU)
    # Opsi parameter dibuat dari CONTOH_PARAM, jadi parameter baru cukup
    # ditambahkan di satu tempat.
    for nama in CONTOH_PARAM:
        ap.add_argument("--" + nama.replace("_", "-"))
    ap.add_argument("--periksa-contoh", action="store_true",
                    help="uji semua nilai CONTOH_PARAM ke basis data")
    ap.add_argument("--periksa-view", action="store_true",
                    help="periksa bentuk SEMUA view lalu jalankan satu per satu")
    ap.add_argument("--tanpa-jalan", action="store_true",
                    help="dgn --periksa-view: hanya periksa bentuk, jangan dijalankan")
    args = ap.parse_args()

    if args.periksa_contoh:
        return periksa_contoh()
    if args.periksa_view:
        return periksa_view(jalan=not args.tanpa_jalan)

    if args.palet:
        tulis_palet(os.path.join(VISUAL, "palet.html"))
        return

    if args.daftar or not args.view:
        print("View tersedia:\n")
        for nama, v in VIEW.items():
            par = f" (butuh: {', '.join('--' + p.replace('_','-') for p in v['params'])})" \
                  if v["params"] else ""
            print(f"  {nama:22} {v['judul']}{par}")
            if v.get("catatan"):
                print(f"  {'':22} {v['catatan']}")
            print()
        return

    if args.view not in VIEW:
        raise SystemExit(f"View '{args.view}' tidak ada. Pakai --daftar.")
    v = VIEW[args.view]

    params, dipakai_contoh = {}, []
    for p in v["params"]:
        nilai = getattr(args, p.replace("-", "_"), None)
        if not nilai:
            # Pakai nilai contoh alih-alih galat; nilai yang sah sulit ditebak.
            nilai = CONTOH_PARAM.get(p)
            if nilai is None:
                raise SystemExit(f"View '{args.view}' butuh --{p.replace('_','-')} "
                                 f"dan tidak ada contohnya di CONTOH_PARAM.")
            dipakai_contoh.append(f"--{p.replace('_','-')} {nilai}")
        params[p] = nilai
    if dipakai_contoh:
        print(f"      pakai nilai contoh: {', '.join(dipakai_contoh)}")

    print(f"[1/3] Jalankan view '{args.view}' -- {v['judul']}")
    rows = jalankan(v["cypher"], params or None)
    print(f"      {len(rows)} baris hasil.")
    if not rows:
        raise SystemExit("Kosong. Periksa parameternya (mis. local_id / kode unit).")

    print("[2/3] Bentuk node & relasi...")
    # Dict berkunci id: satu topik muncul di banyak baris, dan id ganda
    # membuat vis.DataSet galat.
    nodes, edges = v["bangun"](rows)
    per_tipe = {}
    for n in nodes.values():
        per_tipe[n["tipe"]] = per_tipe.get(n["tipe"], 0) + 1
    print(f"      {len(nodes)} node ({', '.join(f'{k} {x}' for k, x in per_tipe.items())}), "
          f"{len(edges)} relasi.")
    if len(nodes) > 800:
        print("      PERINGATAN: >800 node akan berat & sulit dibaca. "
              "Persempit cakupannya.")

    print(f"[3/3] Tulis format '{args.format}'...")
    if args.format == "gephi":
        dasar = os.path.splitext(args.keluaran)[0]
        os.makedirs(os.path.dirname(args.keluaran), exist_ok=True)
        fn, fe = ke_gephi(nodes, edges, dasar)
        print(f"      -> {fn}\n      -> {fe}")
        print("\n  Di Gephi: File > Import spreadsheet, muat NODE dulu (as Nodes")
        print("  table), lalu EDGE (as Edges table, centang 'Append to existing').")
        print("  Lalu: Layout > ForceAtlas2, centang 'Prevent Overlap', Run.")
        print("  Setelah rapi: Layout > Label Adjust. Ekspor lewat")
        print("  File > Export > Graph file (.gexf) -- posisinya ikut tersimpan.")
        return
    data = ke_vis(nodes, edges) if args.format == "vis" else ke_cytoscape(nodes, edges)
    os.makedirs(os.path.dirname(args.keluaran), exist_ok=True)
    with open(args.keluaran, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"      -> {args.keluaran}")


if __name__ == "__main__":
    main()
