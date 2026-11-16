#!/usr/bin/env python3
"""banding.py — Bukti kolaborasi untuk 2-3 peneliti, bahan fitur Brainstorm.

LLM selalu bisa mengarang "peluang kolaborasi" untuk dua orang mana pun. Karena
itu bukti dihitung program dari graf, dan LLM hanya menalar di atasnya.
Cakupan dikunci oleh peneliti yang dipilih pengguna.

Tidak ada pasangan yang ditolak; tingkat buktinya yang berbeda:
  direct       pernah menulis bersama, ATAU tercatat SEBIDANG, ATAU ada topik
               yang sama-sama dikerjakan (>= 2 publikasi di tiap sisi)
  bridge       bukan direct, tapi ada publikasi yang mirip isinya, peneliti
               perantara (berbagi topik dgn keduanya), atau jalur perkenalan
  exploratory  tidak ada bukti di graf; brainstorming tetap boleh, dilabeli

PUBLIKASI MIRIP (maks 5)
Dari vektor abstrak (data/vektor_abstrak.npy) yang DIPUSATKAN (dikurangi
rata-rata korpus). Tanpa pemusatan, pasangan peneliti acak pun bernilai 0,71
karena banyak abstrak berpola sama. Dengan pemusatan, AUC pasangan rekan
penulis vs acak naik dari 0,69 ke 0,75. Satu pasangan mirip tetap hanya
bukti jembatan, bukan penentu.

Semua kueri lewat endpoint baca-saja dengan parameter, jadi nama dari
pengguna tidak bisa menyusup ke kueri.

Pemakaian:
    .venv/bin/python scripts/banding.py PRSDI016 PRSDI043
    .venv/bin/python scripts/banding.py --nama "Uun Hariyanti" "Rio Nurtantyana"
    .venv/bin/python scripts/banding.py PRSDI016 PRSDI043 --json
    .venv/bin/python scripts/banding.py --kalibrasi      # cek ambang kemiripan
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np
import requests

AKAR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AKAR / "scripts"))

from lib.tanya_graf import AUTH, URL_BACA  # noqa: E402

MAKS_PENELITI = 3
MAKS_MIRIP = 5
MAKS_MIRIP_LEMAH = 2      # bila tak ada yg >= moderate: tampilkan yg terdekat saja
MAKS_ABSTRAK = 8          # dari publikasi mirip; 8 x 800 huruf ~ 2.000 token
POTONG_ABSTRAK = 800
MAKS_TOPIK_KHAS = 5
MAKS_PERANTARA = 5
MAKS_REKAN = 5
BERKAS_VEKTOR = AKAR / "data" / "vektor_abstrak.npy"

# Ambang pada vektor yang dipusatkan, dari 200.000 pasangan publikasi acak:
# moderate = p99 (0,39), strong = p99,9 (0,57). Cek ulang: --kalibrasi.
AMBANG_KUAT = 0.57
AMBANG_SEDANG = 0.39

_VEKTOR = None            # dimuat sekali per proses; server WAJIB restart
                          # sesudah vektor abstrak dibangun ulang


def _q(cypher, params=None, timeout=120):
    r = requests.post(URL_BACA, auth=AUTH, timeout=timeout,
                      json={"language": "opencypher", "command": cypher,
                            "params": params or {}})
    if r.status_code >= 400:
        raise RuntimeError(f"ArcadeDB {r.status_code}: {r.text[:200]}")
    return [{k: v for k, v in b.items() if not k.startswith("@")}
            for b in r.json().get("result", [])]


def _judul_kunci(judul):
    return re.sub(r"[^a-z0-9]", "", (judul or "").lower())


def _judul_sama(kunci, dipakai, awal=40):
    """40 huruf awal judul yang satu muncul di judul lain = karya yang sama.
    Duplikat di data ini berbeda di ekor (keterangan sitasi ditempel) DAN di
    awal (versi pracetak "A Survey on ..."). Judul < 40 huruf harus persis."""
    for d in dipakai:
        if kunci == d:
            return True
        if min(len(kunci), len(d)) >= awal and (kunci[:awal] in d or d[:awal] in kunci):
            return True
    return False


def muat_vektor():
    """(matriks dipusatkan, {dedup_key: baris}) atau None bila berkas tak ada."""
    global _VEKTOR
    if _VEKTOR is None:
        meta = BERKAS_VEKTOR.with_suffix(".json")
        if not (BERKAS_VEKTOR.exists() and meta.exists()):
            _VEKTOR = False
        else:
            V = np.load(BERKAS_VEKTOR).astype(np.float32)
            V = V - V.mean(axis=0)
            V /= np.linalg.norm(V, axis=1, keepdims=True)
            kunci = json.loads(meta.read_text())["kunci"]
            _VEKTOR = (V, {k: i for i, k in enumerate(kunci)})
    return _VEKTOR or None


def label_kemiripan(s):
    return "strong" if s >= AMBANG_KUAT else ("moderate" if s >= AMBANG_SEDANG else "weak")


def cari_id(nama_list):
    """Nama lengkap (tak peka huruf) -> local_id peneliti internal."""
    kecil = [n.strip().lower() for n in nama_list]
    hasil = _q("MATCH (p:Peneliti {is_internal: true}) WHERE toLower(p.nama) IN $n "
               "RETURN p.local_id AS id, toLower(p.nama) AS n", {"n": kecil})
    peta = {b["n"]: b["id"] for b in hasil}
    hilang = [n for n, k in zip(nama_list, kecil) if k not in peta]
    return [peta[k] for k in kecil if k in peta], hilang


# Komponen bukti
def _publikasi_mirip(ids, karya, bersama_kunci, vek):
    V, idx = vek
    kandidat = []
    for a, b in combinations(ids, 2):
        ka = [k for k in karya[a] if k in idx and k not in bersama_kunci]
        kb = [k for k in karya[b] if k in idx and k not in bersama_kunci]
        if not ka or not kb:
            continue
        S = V[[idx[k] for k in ka]] @ V[[idx[k] for k in kb]].T
        n = min(S.size, 60)
        for f in np.argpartition(-S.ravel(), n - 1)[:n]:
            i, j = divmod(int(f), S.shape[1])
            kandidat.append((float(S[i, j]), a, ka[i], b, kb[j]))
    kandidat.sort(key=lambda c: -c[0])

    hasil, kunci_dipakai, judul_dipakai = [], set(), set()

    def boleh(c):
        _, a, ka, b, kb = c
        if ka in kunci_dipakai or kb in kunci_dipakai:
            return False                    # tiap publikasi maksimal sekali
        ja, jb = _judul_kunci(karya[a][ka]["judul"]), _judul_kunci(karya[b][kb]["judul"])
        if _judul_sama(ja, {jb}):
            return False                    # karya sama berkunci beda (duplikat)
        return not (_judul_sama(ja, judul_dipakai) or _judul_sama(jb, judul_dipakai))

    def ambil(c):
        s, a, ka, b, kb = c
        kunci_dipakai.update((ka, kb))
        judul_dipakai.update((_judul_kunci(karya[a][ka]["judul"]),
                              _judul_kunci(karya[b][kb]["judul"])))
        hasil.append({"skor": round(s, 3), "label": label_kemiripan(s),
                      "a": {"id": a, "kunci": ka, **karya[a][ka]},
                      "b": {"id": b, "kunci": kb, **karya[b][kb]}})

    if len(ids) == 3:
        # Putaran 1: tiap pasangan orang dapat satu tempat bila minimal sedang,
        # supaya lima tempat tidak dihabiskan satu pasangan saja.
        for a, b in combinations(ids, 2):
            for c in kandidat:
                if (c[1], c[3]) == (a, b) and c[0] >= AMBANG_SEDANG and boleh(c):
                    ambil(c)
                    break
    for c in kandidat:
        if len(hasil) >= MAKS_MIRIP:
            break
        if boleh(c):
            ambil(c)
    hasil.sort(key=lambda x: -x["skor"])
    # Skor rendah (mis. 0,02) bukan "mirip"; saring sebelum ditampilkan.
    layak = [h for h in hasil if h["label"] != "weak"]
    return layak if layak else hasil[:MAKS_MIRIP_LEMAH]


def _perantara(a, b, ids, profil):
    """Peneliti internal lain yang berbagi topik dgn KEDUANYA.

    Skor = kemiripan profil yang LEBIH LEMAH dari dua sisi, dgn kosinus
    himpunan |A n C| / sqrt(|A| |C|). Ukuran profil C ikut membagi: tanpa itu
    perantara selalu orang yang menyentuh hampir semua topik. Terukur 17 Sep
    2026 (200 pasangan acak, hitungan irisan mentah): Bayu Rahayudi teratas di
    57 pasangan, Satrio Agung Wicaksono 41 -- keduanya 64 topik, median 19.
    """
    ta, tb = profil[a][2], profil[b][2]
    hasil = []
    for c, (nama, unit, topik) in profil.items():
        if c in ids or not topik:
            continue
        na, nb = len(topik & ta), len(topik & tb)
        if na and nb and ta and tb:
            skor = min(na / (len(ta) * len(topik)) ** 0.5,
                       nb / (len(tb) * len(topik)) ** 0.5)
            hasil.append({"id": c, "nama": nama, "unit": unit, "skor": round(skor, 3),
                          "topik_dengan": {a: na, b: nb}})
    hasil.sort(key=lambda x: -x["skor"])
    return hasil[:MAKS_PERANTARA]


# Rekan eksternal bernama inisial ("Hadi A.") tidak bisa dipastikan orang yang
# sama di dua publikasi, jadi tidak dipakai sebagai bukti perkenalan.
POLA_INISIAL = re.compile(r"(?:^|\s)(?:[A-Z]\.)+$")    # BERAKHIR inisial; "M. Yudhi Rezaldi" lolos


def _bisa_dipercaya(nama, internal):
    return bool(internal) or not POLA_INISIAL.search(nama or "")


def _jalur_perkenalan(a, b):
    """Rekan penulis bersama; kalau tidak ada, satu jalur 3 langkah."""
    rekan = _q("""
MATCH (a:Peneliti {local_id: $a})-[:KOLABORASI]-(c:Peneliti)-[:KOLABORASI]-(b:Peneliti {local_id: $b})
RETURN DISTINCT c.local_id AS id, c.nama AS nama, c.is_internal AS internal LIMIT 20""",
               {"a": a, "b": b})
    rekan = [r for r in rekan if _bisa_dipercaya(r["nama"], r["internal"])]
    if rekan:
        rekan.sort(key=lambda r: not r["internal"])       # internal didahulukan
        return {"rekan_bersama": rekan[:MAKS_REKAN], "jalur": None}
    try:
        jalur = _q("""
MATCH (a:Peneliti {local_id: $a})-[:KOLABORASI]-(c:Peneliti)-[:KOLABORASI]-(d:Peneliti)-[:KOLABORASI]-(b:Peneliti {local_id: $b})
WHERE c.local_id <> $b AND d.local_id <> $a AND c <> d
RETURN c.nama AS c, c.is_internal AS ci, d.nama AS d, d.is_internal AS di LIMIT 50""",
                   {"a": a, "b": b}, timeout=30)
    except (requests.Timeout, RuntimeError):
        jalur = []
    jalur = [j for j in jalur if _bisa_dipercaya(j["c"], j["ci"]) and _bisa_dipercaya(j["d"], j["di"])]
    if not jalur:
        return {"rekan_bersama": [], "jalur": None}
    j = max(jalur, key=lambda r: bool(r["ci"]) + bool(r["di"]))
    return {"rekan_bersama": [], "jalur": [j["c"], j["d"]]}


# Utama
def banding(ids):
    ids = list(dict.fromkeys(ids))
    if not 2 <= len(ids) <= MAKS_PENELITI:
        raise ValueError(f"Pilih 2 sampai {MAKS_PENELITI} peneliti (sekarang {len(ids)}).")

    orang = {b["id"]: b for b in _q("""
MATCH (p:Peneliti) WHERE p.local_id IN $ids
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
OPTIONAL MATCH (u)-[:AFILIASI]->(i:Institusi)
RETURN p.local_id AS id, p.nama AS nama, u.kode AS unit, i.nama AS institusi,
       p.is_internal AS internal""", {"ids": ids})}
    hilang = [i for i in ids if i not in orang]
    if hilang:
        raise ValueError(f"Peneliti tidak ditemukan: {', '.join(hilang)}")
    luar = [orang[i]["nama"] for i in ids if not orang[i]["internal"]]
    if luar:
        raise ValueError(f"{', '.join(luar)}: rekan penulis eksternal -- data profilnya "
                         "terlalu tipis untuk dibandingkan.")

    # Publikasi tiap orang (sekaligus hitungan per tahun)
    karya = defaultdict(dict)
    for b in _q("""
MATCH (p:Peneliti)-[:MENULIS]->(k:Publikasi) WHERE p.local_id IN $ids
RETURN p.local_id AS id, k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun""",
                {"ids": ids}):
        karya[b["id"]][b["kunci"]] = {"judul": b["judul"], "tahun": b["tahun"]}

    # Profil topik semua peneliti internal (untuk topik bersama dan perantara)
    profil, topik, info_topik = {}, defaultdict(dict), {}
    for b in _q("""
MATCH (p:Peneliti {is_internal: true})-[r:BIDANG_KEAHLIAN]->(t:Topik)
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
OPTIONAL MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema: 'ASJC'})
RETURN p.local_id AS id, p.nama AS nama, u.kode AS unit, t.topik_id AS tid,
       t.nama AS topik, r.jumlah AS jumlah, k.nama AS kategori"""):
        profil.setdefault(b["id"], (b["nama"], b["unit"], set()))[2].add(b["tid"])
        info_topik[b["tid"]] = (b["topik"], b["kategori"])
        if b["id"] in ids:
            topik[b["tid"]][b["id"]] = b["jumlah"] or 0
    for i in ids:
        profil.setdefault(i, (orang[i]["nama"], orang[i]["unit"], set()))

    bersama = [{"topik": info_topik[t][0], "kategori": info_topik[t][1], "topik_id": t,
                "jumlah": {i: topik[t].get(i, 0) for i in ids}}
               for t in topik if len(topik[t]) >= 2]
    bersama.sort(key=lambda x: (-min(v for v in x["jumlah"].values() if v),
                                -sum(x["jumlah"].values())))
    khas = {i: [{"topik": info_topik[t][0], "jumlah": topik[t][i]}
                for t in sorted((t for t in topik if list(topik[t]) == [i]),
                                key=lambda t: -topik[t][i])[:MAKS_TOPIK_KHAS]]
            for i in ids}
    kat = defaultdict(Counter)
    for t, per in topik.items():
        for i, j in per.items():
            if info_topik[t][1]:
                kat[info_topik[t][1]][i] += j
    kategori_bersama = sorted(({"kategori": k, "bobot": dict(c)}
                               for k, c in kat.items() if len(c) >= 2),
                              key=lambda x: -min(x["bobot"].values()))

    # Publikasi bersama dan publikasi mirip
    bersama_kunci = set()
    publikasi_bersama = []
    for a, b in combinations(ids, 2):
        for k in sorted(set(karya[a]) & set(karya[b]),
                        key=lambda k: -(karya[a][k]["tahun"] or 0)):
            bersama_kunci.add(k)
            if len(publikasi_bersama) < MAKS_REKAN:
                publikasi_bersama.append({"a": a, "b": b, "kunci": k, **karya[a][k]})
    vek = muat_vektor()
    mirip = _publikasi_mirip(ids, karya, bersama_kunci, vek) if vek else []
    tanpa_vektor = sum(1 for i in ids for k in karya[i] if vek and k not in vek[1])

    # Tiap pasangan: hubungan, tingkat, jembatan
    sebidang = {(r["a"], r["b"]): r for r in _q("""
MATCH (a:Peneliti)-[r:SEBIDANG]-(b:Peneliti)
WHERE a.local_id IN $ids AND b.local_id IN $ids
RETURN a.local_id AS a, b.local_id AS b, r.skor AS skor, r.jumlah_topik_sama AS sama""",
                                                {"ids": ids})}
    pasangan = []
    for a, b in combinations(ids, 2):
        sb = sebidang.get((a, b)) or sebidang.get((b, a)) or {}
        joint = len(set(karya[a]) & set(karya[b]))
        seimbang = [t for t in topik if topik[t].get(a, 0) >= 2 and topik[t].get(b, 0) >= 2]
        terbaik = max((m["skor"] for m in mirip
                       if {m["a"]["id"], m["b"]["id"]} == {a, b}), default=None)
        s = {"a": a, "b": b, "publikasi_bersama": joint,
             "skor_sebidang": sb.get("skor"), "topik_sama": sb.get("sama"),
             "publikasi_mirip_terbaik": terbaik, "perantara": [],
             "rekan_bersama": [], "jalur": None}
        if joint:
            s["tingkat"], s["alasan"] = "direct", f"co-authored {joint} publication(s)"
        elif sb:
            s["tingkat"], s["alasan"] = "direct", f"similar topic profile ({sb['skor']:.2f})"
        elif seimbang:
            s["tingkat"], s["alasan"] = "direct", f"{len(seimbang)} shared topic(s) with 2+ publications each"
        else:
            s["perantara"] = _perantara(a, b, ids, profil)
            s.update(_jalur_perkenalan(a, b))
            bukti = []
            if terbaik is not None and terbaik >= AMBANG_SEDANG:
                bukti.append(f"{label_kemiripan(terbaik)} similar publications ({terbaik:.2f})")
            if s["perantara"]:
                bukti.append(f"{len(s['perantara'])} bridge researcher(s), e.g. {s['perantara'][0]['nama']}")
            if s["rekan_bersama"]:
                bukti.append(f"shared co-author {s['rekan_bersama'][0]['nama']}")
            elif s["jalur"]:
                bukti.append("introduction path via " + " -> ".join(s["jalur"]))
            s["tingkat"] = "bridge" if bukti else "exploratory"
            s["alasan"] = "; ".join(bukti) or "no shared topics, similar publications or bridges in the graph"
        pasangan.append(s)

    # Tingkat keseluruhan
    nama = {i: orang[i]["nama"] for i in ids}
    tk = [p["tingkat"] for p in pasangan]
    if all(t == "direct" for t in tk):
        tingkat, alasan = "direct", "every pair has direct evidence"
    else:
        direct_dgn = Counter(x for p in pasangan if p["tingkat"] == "direct"
                             for x in (p["a"], p["b"]))
        penghubung = [i for i in ids if direct_dgn[i] == len(ids) - 1] if len(ids) == 3 else []
        if penghubung:
            lain = [nama[i] for i in ids if i != penghubung[0]]
            tingkat, alasan = "bridge", f"{nama[penghubung[0]]} links {' and '.join(lain)}"
        elif all(t in ("direct", "bridge") for t in tk):
            tingkat, alasan = "bridge", "connected through bridges or similar publications"
        else:
            tingkat, alasan = "exploratory", "weak evidence in the graph; ideas must be labelled exploratory"

    # Abstrak untuk LLM: publikasi yang ditampilkan sebagai mirip
    pilih = []
    for m in mirip:
        for sisi in ("a", "b"):
            if m[sisi]["kunci"] not in [p["kunci"] for p in pilih] and len(pilih) < MAKS_ABSTRAK:
                pilih.append(m[sisi])
    sumber_abstrak = "similar_publications"
    if pilih and len(pilih) < MAKS_ABSTRAK and not any(m["label"] != "weak" for m in mirip):
        # Bukti lemah: sisa jatah diisi publikasi terbaru tiap orang, bergiliran,
        # supaya brainstorming exploratory tetap membaca isi riset keduanya.
        sumber_abstrak = "similar_publications+recent"
        antre = {i: sorted(({"id": i, "kunci": k, **v} for k, v in karya[i].items()),
                           key=lambda x: -(x["tahun"] or 0)) for i in ids}
        while len(pilih) < MAKS_ABSTRAK and any(antre.values()):
            for i in ids:
                while antre[i] and (antre[i][0]["kunci"] in [p["kunci"] for p in pilih]
                                    or _judul_sama(_judul_kunci(antre[i][0]["judul"]),
                                                   {_judul_kunci(p["judul"]) for p in pilih})):
                    antre[i].pop(0)
                if antre[i] and len(pilih) < MAKS_ABSTRAK:
                    pilih.append(antre[i].pop(0))
    if not pilih:                                   # cadangan: lewat topik bersama
        sumber_abstrak = "shared_topics"
        tids = [x["topik_id"] for x in bersama[:5]]
        if tids:
            for c in sorted(_q("""
MATCH (p:Peneliti)-[:MENULIS]->(k:Publikasi)-[m:MEMBAHAS]->(t:Topik)
WHERE p.local_id IN $ids AND t.topik_id IN $tids AND k.abstrak IS NOT NULL
RETURN p.local_id AS id, k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun,
       m.skor AS skor""", {"ids": ids, "tids": tids}), key=lambda c: -(c["skor"] or 0)):
                if len(pilih) >= MAKS_ABSTRAK:
                    break
                if not _judul_sama(_judul_kunci(c["judul"]),
                                   {_judul_kunci(p["judul"]) for p in pilih}):
                    pilih.append(c)
    teks = {b["kunci"]: b["abstrak"] for b in _q(
        "MATCH (k:Publikasi) WHERE k.dedup_key IN $k RETURN k.dedup_key AS kunci, k.abstrak AS abstrak",
        {"k": [p["kunci"] for p in pilih]})} if pilih else {}
    abstrak = [{"id": p["id"], "kunci": p["kunci"], "judul": p["judul"], "tahun": p["tahun"],
                "abstrak": (teks.get(p["kunci"]) or "")[:POTONG_ABSTRAK]} for p in pilih]

    peneliti = [{"id": i, "nama": orang[i]["nama"], "unit": orang[i]["unit"],
                 "institusi": orang[i]["institusi"], "publikasi": len(karya[i]),
                 "per_tahun": dict(sorted(Counter(str(v["tahun"]) for v in karya[i].values()).items()))}
                for i in ids]
    return {
        "peneliti": peneliti,
        "tingkat": tingkat, "alasan": alasan,
        "pasangan": pasangan,
        "publikasi_mirip": mirip,
        "publikasi_bersama": publikasi_bersama,
        "topik_bersama": bersama,
        "topik_khas": khas,
        "kategori_bersama": kategori_bersama,
        "abstrak": abstrak, "sumber_abstrak": sumber_abstrak,
        "vektor": {"tersedia": bool(vek), "publikasi_tanpa_vektor": tanpa_vektor},
    }


# CLI
def kalibrasi(n=200_000):
    vek = muat_vektor()
    if not vek:
        sys.exit(f"Berkas vektor tidak ada: {BERKAS_VEKTOR}")
    V, _ = vek
    rng = np.random.default_rng(17)
    a, b = rng.integers(0, len(V), n), rng.integers(0, len(V), n)
    m = a != b
    s = np.sum(V[a[m]] * V[b[m]], axis=1)
    p99, p999 = np.percentile(s, 99), np.percentile(s, 99.9)
    print(f"{m.sum():,} pasangan publikasi acak (vektor dipusatkan):")
    print(f"  median {np.median(s):.2f} | p95 {np.percentile(s, 95):.2f} | "
          f"p99 {p99:.2f} | p99,9 {p999:.2f}")
    print(f"  ambang di kode: moderate {AMBANG_SEDANG} (p99), strong {AMBANG_KUAT} (p99,9)")
    if abs(p99 - AMBANG_SEDANG) > 0.03 or abs(p999 - AMBANG_KUAT) > 0.03:
        print("  PERINGATAN: ambang sudah bergeser dari sebaran -- perbarui konstanta.")
    else:
        print("  ambang masih sesuai.")


def cetak(h):
    nama = {p["id"]: p["nama"] for p in h["peneliti"]}
    print("=" * 70)
    for p in h["peneliti"]:
        print(f"  {p['nama']} ({p['id']}) | {p['unit']} {p['institusi']} | {p['publikasi']} publikasi")
    print(f"  TINGKAT: {h['tingkat'].upper()} -- {h['alasan']}")
    for s in h["pasangan"]:
        print(f"  - {nama[s['a']]} x {nama[s['b']]}: {s['tingkat']} ({s['alasan']})")
        for c in s["perantara"][:3]:
            print(f"      perantara: {c['nama']} ({c['unit']}) skor {c['skor']:.2f}, topik {list(c['topik_dengan'].values())}")
        if s["rekan_bersama"]:
            print("      rekan bersama: " + ", ".join(r["nama"] for r in s["rekan_bersama"]))
        if s["jalur"]:
            print(f"      jalur: {nama[s['a']]} -> {' -> '.join(s['jalur'])} -> {nama[s['b']]}")
    print("-" * 70)
    print(f"  publikasi mirip ({len(h['publikasi_mirip'])}):")
    for m in h["publikasi_mirip"]:
        print(f"    {m['skor']:.2f} {m['label']:8} [{nama[m['a']['id']][:12]}] {m['a']['tahun']} {m['a']['judul'][:40]}")
        print(f"    {'':14}<-> [{nama[m['b']['id']][:12]}] {m['b']['tahun']} {m['b']['judul'][:40]}")
    if h["publikasi_bersama"]:
        print(f"  publikasi bersama (maks 5): " + "; ".join(p["judul"][:35] for p in h["publikasi_bersama"]))
    print(f"  topik bersama: {len(h['topik_bersama'])} | kategori bersama: {len(h['kategori_bersama'])}")
    print(f"  abstrak utk LLM: {len(h['abstrak'])} (sumber: {h['sumber_abstrak']}, "
          f"{sum(len(a['abstrak']) for a in h['abstrak']):,} huruf)")
    v = h["vektor"]
    print(f"  vektor: {'tersedia' if v['tersedia'] else 'TIDAK ADA -- data/vektor_abstrak.npy hilang'}"
          + (f" | PERINGATAN {v['publikasi_tanpa_vektor']} publikasi tanpa vektor -- bangun ulang"
             if v["publikasi_tanpa_vektor"] else ""))
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id", nargs="*", help="local_id peneliti (2-3)")
    ap.add_argument("--nama", nargs="+", help="nama lengkap peneliti (2-3)")
    ap.add_argument("--json", action="store_true", help="cetak JSON mentah")
    ap.add_argument("--kalibrasi", action="store_true", help="cek ambang kemiripan")
    a = ap.parse_args()
    if a.kalibrasi:
        return kalibrasi()
    ids = a.id
    if a.nama:
        ids, hilang = cari_id(a.nama)
        if hilang:
            sys.exit(f"Nama tidak ditemukan: {', '.join(hilang)}")
    try:
        h = banding(ids)
    except ValueError as e:
        sys.exit(str(e))
    if a.json:
        print(json.dumps(h, ensure_ascii=False, indent=1))
    else:
        cetak(h)


if __name__ == "__main__":
    main()
