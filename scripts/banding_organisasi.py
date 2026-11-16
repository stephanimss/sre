#!/usr/bin/env python3
"""banding_organisasi.py — Bukti kolaborasi dua organisasi (unit / institusi).

Membandingkan dua unit kerja atau institusi: topik apa yang diteliti masing-
masing, irisan mana yang sudah ada, peneliti mana yang sudah berkolaborasi
lintas organisasi, dan pasangan mana yang paling layak dipertemukan.

BACA-SAJA. Tidak memanggil LLM.

Pemakaian:
    .venv/bin/python scripts/banding_organisasi.py PTI PRSDI
    .venv/bin/python scripts/banding_organisasi.py PTI PRSDI --json
    .venv/bin/python scripts/banding_organisasi.py "UB" "BRIN" --institusi
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

AKAR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AKAR / "scripts"))

from banding import (AMBANG_SEDANG, _judul_kunci, _judul_sama, _q,  # noqa: E402
                      label_kemiripan, muat_vektor)

MAKS_PENELITI_TAMPIL = 10
MAKS_KOLABORASI = 10
MAKS_TOPIK_BERSAMA = 10
MAKS_TOPIK_KHAS = 5
MAKS_PUB_MIRIP = 5
MAKS_PUB_MIRIP_LEMAH = 2
MAKS_SARAN = 5
MAKS_ABSTRAK = 5
POTONG_ABSTRAK = 800


def _peneliti_org(kode, mode):
    """Peneliti internal di satu unit kerja atau institusi."""
    if mode == "unit":
        return _q("""
MATCH (u:UnitKerja {kode: $k})-[:MEMILIKI]->(p:Peneliti {is_internal: true})
OPTIONAL MATCH (u)-[:AFILIASI]->(i:Institusi)
RETURN p.local_id AS id, p.nama AS nama, u.kode AS unit, i.nama AS institusi""",
                  {"k": kode})
    return _q("""
MATCH (u:UnitKerja)-[:AFILIASI]->(i:Institusi {nama: $k}),
      (u)-[:MEMILIKI]->(p:Peneliti {is_internal: true})
RETURN p.local_id AS id, p.nama AS nama, u.kode AS unit, i.nama AS institusi""",
              {"k": kode})


def _profil_topik(ids):
    """Profil topik untuk sekumpulan peneliti -> {id: {tid: jumlah}}."""
    if not ids:
        return {}
    profil = defaultdict(dict)
    for r in _q("""
MATCH (p:Peneliti)-[r:BIDANG_KEAHLIAN]->(t:Topik)
WHERE p.local_id IN $ids
RETURN p.local_id AS id, t.topik_id AS tid, t.nama AS topik, r.jumlah AS jumlah""",
                {"ids": ids}):
        profil[r["id"]][r["tid"]] = r["jumlah"] or 0
    return profil


def _info_topik(tids):
    """topik_id -> {nama, kategori}."""
    if not tids:
        return {}
    info = {}
    for r in _q("""
MATCH (t:Topik) WHERE t.topik_id IN $tids
OPTIONAL MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema: 'ASJC'})
RETURN t.topik_id AS tid, t.nama AS topik, k.nama AS kategori""",
                {"tids": tids}):
        info[r["tid"]] = {"topik": r["topik"], "kategori": r["kategori"]}
    return info


def _pub_mirip_lintas(karya_a, karya_b, vek):
    """Publikasi paling mirip antara dua grup (bukan di dalam satu grup)."""
    V, idx = vek
    ka = [k for k in karya_a if k in idx]
    kb = [k for k in karya_b if k in idx]
    if not ka or not kb:
        return []
    S = V[[idx[k] for k in ka]] @ V[[idx[k] for k in kb]].T
    n = min(S.size, 100)
    kandidat = []
    for f in np.argpartition(-S.ravel(), min(n - 1, S.size - 1))[:n]:
        i, j = divmod(int(f), S.shape[1])
        kandidat.append((float(S[i, j]), ka[i], kb[j]))
    kandidat.sort(key=lambda c: -c[0])

    hasil, kunci_dipakai, judul_dipakai = [], set(), set()
    for s, k1, k2 in kandidat:
        if len(hasil) >= MAKS_PUB_MIRIP:
            break
        if k1 in kunci_dipakai or k2 in kunci_dipakai:
            continue
        j1 = _judul_kunci(karya_a[k1]["judul"])
        j2 = _judul_kunci(karya_b[k2]["judul"])
        if _judul_sama(j1, {j2}):
            continue
        if _judul_sama(j1, judul_dipakai) or _judul_sama(j2, judul_dipakai):
            continue
        kunci_dipakai.update((k1, k2))
        judul_dipakai.update((j1, j2))
        hasil.append({"skor": round(s, 3), "label": label_kemiripan(s),
                      "a": {"kunci": k1, **karya_a[k1]},
                      "b": {"kunci": k2, **karya_b[k2]}})
    layak = [h for h in hasil if h["label"] != "weak"]
    return layak if layak else hasil[:MAKS_PUB_MIRIP_LEMAH]


def banding_organisasi(kode_a, kode_b, mode="unit"):
    """Bandingkan dua organisasi. mode = 'unit' atau 'institusi'."""
    if kode_a == kode_b:
        raise ValueError("Pilih dua organisasi yang berbeda.")

    # Peneliti di masing-masing
    orang_a = _peneliti_org(kode_a, mode)
    orang_b = _peneliti_org(kode_b, mode)
    if not orang_a:
        raise ValueError(f"Organisasi tidak ditemukan atau kosong: {kode_a}")
    if not orang_b:
        raise ValueError(f"Organisasi tidak ditemukan atau kosong: {kode_b}")

    ids_a = [p["id"] for p in orang_a]
    ids_b = [p["id"] for p in orang_b]
    semua_ids = ids_a + ids_b

    # Kolaborasi lintas organisasi
    kolab = _q("""
MATCH (a:Peneliti)-[r:KOLABORASI]-(b:Peneliti)
WHERE a.local_id IN $a AND b.local_id IN $b
RETURN a.local_id AS a, b.local_id AS b, r.jumlah AS jumlah
ORDER BY r.jumlah DESC""", {"a": ids_a, "b": ids_b})
    kolab_pasangan = []
    sudah = set()
    for r in kolab:
        pair = frozenset((r["a"], r["b"]))
        if pair not in sudah:
            sudah.add(pair)
            nama_a = next((p["nama"] for p in orang_a if p["id"] == r["a"]), r["a"])
            nama_b = next((p["nama"] for p in orang_b if p["id"] == r["b"]), r["b"])
            kolab_pasangan.append({
                "a": r["a"], "b": r["b"],
                "nama_a": nama_a, "nama_b": nama_b,
                "jumlah": r["jumlah"] or 0,
            })
    kolab_pasangan = kolab_pasangan[:MAKS_KOLABORASI]

    # Profil topik
    profil_a = _profil_topik(ids_a)
    profil_b = _profil_topik(ids_b)

    semua_tid = set()
    for p in list(profil_a.values()) + list(profil_b.values()):
        semua_tid.update(p.keys())
    info = _info_topik(list(semua_tid))

    # topik per organisasi: tid -> total jumlah, jumlah peneliti
    def ringkas_topik(profil):
        topik = defaultdict(lambda: {"total": 0, "peneliti": 0})
        for orang in profil.values():
            for tid, j in orang.items():
                topik[tid]["total"] += j
                topik[tid]["peneliti"] += 1
        return topik

    topik_a = ringkas_topik(profil_a)
    topik_b = ringkas_topik(profil_b)

    # topik bersama
    bersama_tids = set(topik_a) & set(topik_b)
    topik_bersama = sorted([{
        "topik_id": t,
        "topik": info.get(t, {}).get("topik", t),
        "kategori": info.get(t, {}).get("kategori"),
        "bobot_a": topik_a[t]["total"], "peneliti_a": topik_a[t]["peneliti"],
        "bobot_b": topik_b[t]["total"], "peneliti_b": topik_b[t]["peneliti"],
    } for t in bersama_tids],
        key=lambda x: -(min(x["bobot_a"], x["bobot_b"])))[:MAKS_TOPIK_BERSAMA]

    # topik khas masing-masing
    def topik_khas(topik_org, topik_lain):
        khas = sorted(
            [{"topik_id": t, "topik": info.get(t, {}).get("topik", t),
              "kategori": info.get(t, {}).get("kategori"),
              "total": v["total"], "peneliti": v["peneliti"]}
             for t, v in topik_org.items() if t not in topik_lain],
            key=lambda x: -x["total"])
        return khas[:MAKS_TOPIK_KHAS]

    khas_a = topik_khas(topik_a, topik_b)
    khas_b = topik_khas(topik_b, topik_a)

    # Publikasi
    def muat_karya(ids):
        karya = {}
        for r in _q("""
MATCH (p:Peneliti)-[:MENULIS]->(k:Publikasi) WHERE p.local_id IN $ids
RETURN k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun, p.local_id AS pid""",
                    {"ids": ids}):
            if r["kunci"] not in karya:
                karya[r["kunci"]] = {"judul": r["judul"], "tahun": r["tahun"],
                                     "penulis": []}
            karya[r["kunci"]]["penulis"].append(r["pid"])
        return karya

    karya_a = muat_karya(ids_a)
    karya_b = muat_karya(ids_b)

    # publikasi bersama (ada penulis dari kedua organisasi)
    bersama_kunci = set(karya_a) & set(karya_b)
    pub_bersama = sorted(
        [{"kunci": k, "judul": karya_a[k]["judul"], "tahun": karya_a[k]["tahun"]}
         for k in bersama_kunci],
        key=lambda x: -(x["tahun"] or 0))[:10]

    # Publikasi mirip lintas organisasi
    vek = muat_vektor()
    mirip = []
    if vek:
        ka_excl = {k: v for k, v in karya_a.items() if k not in bersama_kunci}
        kb_excl = {k: v for k, v in karya_b.items() if k not in bersama_kunci}
        mirip = _pub_mirip_lintas(ka_excl, kb_excl, vek)

    # Saran pasangan peneliti
    saran = []
    ids_a_sudah_kolab = {r["a"] for r in kolab_pasangan}
    ids_b_sudah_kolab = {r["b"] for r in kolab_pasangan}
    for ia in ids_a:
        if ia in ids_a_sudah_kolab:
            continue
        ta = set(profil_a.get(ia, {}))
        if not ta:
            continue
        for ib in ids_b:
            if ib in ids_b_sudah_kolab:
                continue
            tb = set(profil_b.get(ib, {}))
            irisan = ta & tb
            if len(irisan) >= 2:
                na = next(p["nama"] for p in orang_a if p["id"] == ia)
                nb = next(p["nama"] for p in orang_b if p["id"] == ib)
                topik_desc = [info.get(t, {}).get("topik", t) for t in
                              sorted(irisan, key=lambda t: -(profil_a[ia].get(t, 0) +
                                                             profil_b[ib].get(t, 0)))[:3]]
                saran.append({
                    "a": ia, "b": ib, "nama_a": na, "nama_b": nb,
                    "topik_bersama": len(irisan),
                    "topik": topik_desc,
                    "alasan": f"{len(irisan)} shared topics, e.g. {', '.join(topik_desc[:2])}",
                })
    saran.sort(key=lambda x: -x["topik_bersama"])
    saran = saran[:MAKS_SARAN]

    # Tingkat keseluruhan
    if kolab_pasangan:
        tingkat = "direct"
        alasan = (f"{len(kolab_pasangan)} cross-organization collaboration(s), "
                  f"{len(topik_bersama)} shared topics, "
                  f"{len(pub_bersama)} joint publications")
    elif topik_bersama or mirip:
        tingkat = "bridge"
        bukti = []
        if topik_bersama:
            bukti.append(f"{len(topik_bersama)} shared topics")
        if mirip:
            terbaik = max(m["skor"] for m in mirip)
            bukti.append(f"similar publications ({terbaik:.2f} {label_kemiripan(terbaik)})")
        alasan = "; ".join(bukti)
    else:
        tingkat = "exploratory"
        alasan = "no shared topics or similar publications between these organizations"

    # Abstrak
    pilih_kunci = []
    for m in mirip:
        for sisi in ("a", "b"):
            if m[sisi]["kunci"] not in pilih_kunci and len(pilih_kunci) < MAKS_ABSTRAK:
                pilih_kunci.append(m[sisi]["kunci"])
    teks = {}
    if pilih_kunci:
        teks = {r["kunci"]: r["abstrak"] for r in _q(
            "MATCH (k:Publikasi) WHERE k.dedup_key IN $k RETURN k.dedup_key AS kunci, k.abstrak AS abstrak",
            {"k": pilih_kunci})}
    abstrak = [{"kunci": k, "judul": (karya_a.get(k) or karya_b.get(k, {})).get("judul", ""),
                "tahun": (karya_a.get(k) or karya_b.get(k, {})).get("tahun"),
                "abstrak": (teks.get(k) or "")[:POTONG_ABSTRAK]}
               for k in pilih_kunci]

    # Kategori ASJC
    def kategori_org(topik_org):
        kat = Counter()
        for tid, v in topik_org.items():
            k = info.get(tid, {}).get("kategori")
            if k:
                kat[k] += v["total"]
        return dict(kat.most_common(10))

    return {
        "org_a": {"kode": kode_a, "mode": mode, "peneliti": len(ids_a),
                  "kategori": kategori_org(topik_a),
                  "daftar": sorted([{"id": p["id"], "nama": p["nama"], "unit": p["unit"]}
                                    for p in orang_a], key=lambda x: x["nama"])[:MAKS_PENELITI_TAMPIL]},
        "org_b": {"kode": kode_b, "mode": mode, "peneliti": len(ids_b),
                  "kategori": kategori_org(topik_b),
                  "daftar": sorted([{"id": p["id"], "nama": p["nama"], "unit": p["unit"]}
                                    for p in orang_b], key=lambda x: x["nama"])[:MAKS_PENELITI_TAMPIL]},
        "tingkat": tingkat,
        "alasan": alasan,
        "kolaborasi": kolab_pasangan,
        "topik_bersama": topik_bersama,
        "topik_khas_a": khas_a,
        "topik_khas_b": khas_b,
        "publikasi_bersama": pub_bersama,
        "publikasi_mirip": mirip,
        "saran": saran,
        "abstrak": abstrak,
        "vektor": {"tersedia": bool(vek)},
    }


if __name__ == "__main__":
    a = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("org_a", help="kode unit / nama institusi pertama")
    a.add_argument("org_b", help="kode unit / nama institusi kedua")
    a.add_argument("--institusi", action="store_true",
                   help="bandingkan sebagai institusi (bawaan: unit kerja)")
    a.add_argument("--json", action="store_true", help="keluaran JSON")
    p = a.parse_args()

    if not muat_vektor():
        print(f"PERINGATAN: vektor abstrak tidak ada ({AKAR / 'data' / 'vektor_abstrak.npy'})",
              file=sys.stderr)

    mode = "institusi" if p.institusi else "unit"
    try:
        h = banding_organisasi(p.org_a, p.org_b, mode)
    except ValueError as e:
        sys.exit(str(e))

    if p.json:
        print(json.dumps(h, ensure_ascii=False, indent=2))
    else:
        print(f"\n{h['org_a']['kode']} ({h['org_a']['peneliti']} peneliti) "
              f"vs {h['org_b']['kode']} ({h['org_b']['peneliti']} peneliti)")
        print(f"Tingkat: {h['tingkat']} — {h['alasan']}\n")
        if h["kolaborasi"]:
            print(f"Kolaborasi lintas organisasi ({len(h['kolaborasi'])}):")
            for k in h["kolaborasi"]:
                print(f"  {k['nama_a']} × {k['nama_b']} ({k['jumlah']} publikasi)")
        if h["topik_bersama"]:
            print(f"\nTopik bersama ({len(h['topik_bersama'])}):")
            for t in h["topik_bersama"]:
                print(f"  {t['topik']} (A:{t['bobot_a']}, B:{t['bobot_b']})")
        if h["saran"]:
            print(f"\nSaran pasangan ({len(h['saran'])}):")
            for s in h["saran"]:
                print(f"  {s['nama_a']} × {s['nama_b']} — {s['alasan']}")
        if h["publikasi_mirip"]:
            print(f"\nPublikasi mirip ({len(h['publikasi_mirip'])}):")
            for m in h["publikasi_mirip"]:
                print(f"  {m['skor']:.2f} {m['label']}: {m['a']['judul'][:50]} × {m['b']['judul'][:50]}")
