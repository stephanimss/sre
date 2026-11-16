#!/usr/bin/env python3
"""banding_publikasi.py — Bukti perbandingan 2-3 PUBLIKASI, bahan Brainstorm.

Pola sama dengan banding.py: bukti dihitung program, LLM hanya menalar di
atasnya. Tidak ada yang ditolak; tingkat buktinya yang berbeda:
  direct       tiap pasangan berkaitan: penulis sama, topik sama, ATAU
               kemiripan abstrak >= moderate
  bridge       sebagian pasangan berkaitan
  exploratory  tidak ada pasangan yang berkaitan; ide wajib dilabeli

Kemiripan memakai vektor abstrak YANG DIPUSATKAN dan ambang yang sama dgn
banding.py (moderate 0,39 = p99, strong 0,57 = p99,9 pasangan acak).

PUBLIKASI TERKAIT (maks 5)
Publikasi lain di graf yang paling mirip dgn salah satu pilihan: strong, ATAU
moderate dgn topik yang sama -- dan punya penulis internal. Ini "related work"
untuk brainstorming, sekaligus menunjukkan SIAPA lagi yang meneliti hal serupa.

Baca-saja dan berparameter.

Pemakaian:
    .venv/bin/python scripts/banding_publikasi.py doi:10.xxx doi:10.yyy
    .venv/bin/python scripts/banding_publikasi.py doi:10.xxx doi:10.yyy --json
"""
import argparse
import json
import sys
from itertools import combinations
from pathlib import Path

import numpy as np

AKAR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AKAR / "scripts"))

from banding import (AMBANG_KUAT, AMBANG_SEDANG, POTONG_ABSTRAK, _judul_kunci,  # noqa: E402
                     _judul_sama, _q, label_kemiripan, muat_vektor)

MAKS_PUBLIKASI = 3
MAKS_TERKAIT = 5
MAKS_PENULIS = 15          # per publikasi di bukti; hyperauthor dipotong


def _terkait(kunci, info, vek):
    """Publikasi lain yang mirip dgn salah satu pilihan (lihat docstring modul)."""
    V, idx = vek
    ada = [k for k in kunci if k in idx]
    if not ada:
        return []
    S = V @ V[[idx[k] for k in ada]].T               # (n_publikasi, n_pilihan)
    terbaik = S.max(axis=1)
    asal = S.argmax(axis=1)
    urut = np.argsort(-terbaik)[:400]
    balik = {i: k for k, i in idx.items()}
    calon = [(float(terbaik[i]), balik[i], ada[int(asal[i])]) for i in urut
             if terbaik[i] >= AMBANG_SEDANG and balik[i] not in kunci]
    if not calon:
        return []
    meta = {b["kunci"]: b for b in _q("""
MATCH (k:Publikasi) WHERE k.dedup_key IN $k
OPTIONAL MATCH (p:Peneliti {is_internal: true})-[:MENULIS]->(k)
OPTIONAL MATCH (k)-[:MEMBAHAS]->(t:Topik)
RETURN k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun, t.topik_id AS topik_id,
       collect(DISTINCT {id: p.local_id, nama: p.nama}) AS internal""",
                                      {"k": [c[1] for c in calon]})}
    dipakai = {_judul_kunci(info[k]["judul"]) for k in kunci}
    hasil = []
    for s, k, dari in calon:
        m = meta.get(k)
        internal = [p for p in (m or {}).get("internal", []) if p.get("id")]
        if not m or not internal:
            continue                              # tanpa penulis internal: bukan rekan
        # Di seluruh korpus, moderate saja meloloskan ~66 kecocokan acak per
        # pencarian. Syaratnya: strong, atau moderate dengan topik yang sama.
        if s < AMBANG_KUAT and m.get("topik_id") != info[dari]["topik_id"]:
            continue
        jk = _judul_kunci(m["judul"])
        if _judul_sama(jk, dipakai):
            continue                              # duplikat pilihan / sesamanya
        dipakai.add(jk)
        hasil.append({"kunci": k, "judul": m["judul"], "tahun": m["tahun"],
                      "skor": round(s, 3), "label": label_kemiripan(s),
                      "mirip_dengan": dari, "penulis_internal": internal[:4]})
        if len(hasil) >= MAKS_TERKAIT:
            break
    return hasil


def banding_publikasi(kunci):
    kunci = list(dict.fromkeys(k for k in kunci if isinstance(k, str) and k.strip()))
    if not 2 <= len(kunci) <= MAKS_PUBLIKASI:
        raise ValueError(f"Pilih 2 sampai {MAKS_PUBLIKASI} publikasi (sekarang {len(kunci)}).")

    info = {b["kunci"]: b for b in _q("""
MATCH (k:Publikasi) WHERE k.dedup_key IN $k
OPTIONAL MATCH (k)-[:TERBIT_DI]->(v:Venue)
OPTIONAL MATCH (k)-[m:MEMBAHAS]->(t:Topik)
OPTIONAL MATCH (t)-[:TERMASUK_DALAM]->(c:Kategori {skema: 'ASJC'})
RETURN k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun, k.sitasi AS sitasi,
       k.abstrak AS abstrak, v.nama AS venue, t.topik_id AS topik_id, t.nama AS topik,
       c.nama AS kategori""", {"k": kunci})}
    hilang = [k for k in kunci if k not in info]
    if hilang:
        raise ValueError(f"Publikasi tidak ditemukan: {', '.join(hilang)}")

    penulis = {k: [] for k in kunci}
    for b in _q("""
MATCH (p:Peneliti)-[:MENULIS]->(k:Publikasi) WHERE k.dedup_key IN $k
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
RETURN k.dedup_key AS kunci, p.local_id AS id, p.nama AS nama,
       p.is_internal AS internal, u.kode AS unit ORDER BY p.is_internal DESC, p.nama""",
                {"k": kunci}):
        penulis[b["kunci"]].append({"id": b["id"], "nama": b["nama"],
                                    "internal": b["internal"] is True, "unit": b["unit"]})

    publikasi = []
    for k in kunci:
        i = info[k]
        publikasi.append({
            "kunci": k, "judul": i["judul"], "tahun": i["tahun"], "sitasi": i["sitasi"],
            "venue": i["venue"], "topik": i["topik"], "topik_id": i["topik_id"],
            "kategori": i["kategori"], "jumlah_penulis": len(penulis[k]),
            "penulis": penulis[k][:MAKS_PENULIS]})

    vek = muat_vektor()
    V, idx = vek if vek else (None, {})
    pasangan = []
    for a, b in combinations(kunci, 2):
        ia, ib = info[a], info[b]
        skor = (round(float(V[idx[a]] @ V[idx[b]]), 3)
                if vek and a in idx and b in idx else None)
        ida = {p["id"] for p in penulis[a]}
        sama = [p for p in penulis[b] if p["id"] in ida]
        s = {"a": a, "b": b, "skor": skor,
             "label": label_kemiripan(skor) if skor is not None else None,
             "penulis_bersama": [{"id": p["id"], "nama": p["nama"], "internal": p["internal"]}
                                 for p in sama],
             "topik_sama": ia["topik"] if ia["topik_id"] and ia["topik_id"] == ib["topik_id"] else None,
             "kategori_sama": ia["kategori"] if ia["kategori"] and ia["kategori"] == ib["kategori"] else None,
             "venue_sama": bool(ia["venue"]) and ia["venue"] == ib["venue"]}
        bukti = []
        if sama:
            bukti.append(f"shared author(s): {', '.join(p['nama'] for p in sama[:3])}")
        if s["topik_sama"]:
            bukti.append("same topic")
        if skor is not None and skor >= AMBANG_SEDANG:
            bukti.append(f"{s['label']} abstract similarity ({skor:.2f})")
        if s["venue_sama"]:
            bukti.append("same venue")
        s["berkaitan"] = bool(sama or s["topik_sama"] or (skor is not None and skor >= AMBANG_SEDANG))
        s["alasan"] = "; ".join(bukti) or (
            f"different topics, weak abstract similarity ({skor:.2f})" if skor is not None
            else "different topics")
        pasangan.append(s)

    n_kait = sum(p["berkaitan"] for p in pasangan)
    if n_kait == len(pasangan):
        tingkat, alasan = "direct", "every pair shares authors, a topic, or similar abstracts"
    elif n_kait:
        tingkat, alasan = "bridge", f"{n_kait} of {len(pasangan)} pairs are related"
    else:
        tingkat, alasan = "exploratory", "no shared authors, topics or similar abstracts; ideas must be labelled exploratory"

    terkait = _terkait(kunci, info, vek) if vek else []

    # Peneliti internal yang terlibat: penulis pilihan + penulis publikasi terkait.
    orang = {}
    for p in publikasi:
        for x in p["penulis"]:
            if x["internal"]:
                orang.setdefault(x["id"], {"id": x["id"], "nama": x["nama"], "unit": x["unit"],
                                           "pilihan": [], "terkait": []})["pilihan"].append(p["kunci"])
    for t in terkait:
        for x in t["penulis_internal"]:
            orang.setdefault(x["id"], {"id": x["id"], "nama": x["nama"], "unit": None,
                                       "pilihan": [], "terkait": []})["terkait"].append(t["kunci"])

    # Abstrak untuk LLM: semua pilihan (maks 3), lalu yang terkait (maks 5).
    teks = {b["kunci"]: b["abstrak"] for b in _q(
        "MATCH (k:Publikasi) WHERE k.dedup_key IN $k RETURN k.dedup_key AS kunci, k.abstrak AS abstrak",
        {"k": [t["kunci"] for t in terkait]})} if terkait else {}
    abstrak = ([{"kunci": k, "judul": info[k]["judul"], "tahun": info[k]["tahun"],
                 "abstrak": (info[k]["abstrak"] or "")[:POTONG_ABSTRAK]} for k in kunci] +
               [{"kunci": t["kunci"], "judul": t["judul"], "tahun": t["tahun"],
                 "abstrak": (teks.get(t["kunci"]) or "")[:POTONG_ABSTRAK]} for t in terkait])

    return {
        "publikasi": publikasi, "pasangan": pasangan,
        "tingkat": tingkat, "alasan": alasan,
        "publikasi_terkait": terkait,
        "peneliti": sorted(orang.values(), key=lambda o: (-len(o["pilihan"]), -len(o["terkait"]), o["nama"])),
        "abstrak": abstrak,
        "vektor": {"tersedia": bool(vek),
                   "tanpa_vektor": [k for k in kunci if vek and k not in idx]},
    }


def cetak(h):
    print("=" * 70)
    kode = {p["kunci"]: f"P{i}" for i, p in enumerate(h["publikasi"], 1)}
    for p in h["publikasi"]:
        print(f"  [{kode[p['kunci']]}] {p['tahun']} {p['judul'][:60]}")
        print(f"       topik: {(p['topik'] or '-')[:50]} | penulis: {p['jumlah_penulis']} "
              f"({', '.join(x['nama'] for x in p['penulis'] if x['internal'])})")
    print(f"  TINGKAT: {h['tingkat'].upper()} -- {h['alasan']}")
    for s in h["pasangan"]:
        print(f"  - {kode[s['a']]} x {kode[s['b']]}: {'berkaitan' if s['berkaitan'] else 'tidak'} ({s['alasan']})")
    print(f"  terkait ({len(h['publikasi_terkait'])}):")
    for t in h["publikasi_terkait"]:
        print(f"    {t['skor']:.2f} {t['label']:8} ~{kode[t['mirip_dengan']]} {t['tahun']} {t['judul'][:45]} "
              f"-- {', '.join(x['nama'] for x in t['penulis_internal'])}")
    print(f"  peneliti internal: {len(h['peneliti'])} | abstrak utk LLM: {len(h['abstrak'])}")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kunci", nargs="+", help="dedup_key publikasi (2-3)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        h = banding_publikasi(a.kunci)
    except ValueError as e:
        sys.exit(str(e))
    print(json.dumps(h, ensure_ascii=False, indent=1) if a.json else "", end="")
    if not a.json:
        cetak(h)


if __name__ == "__main__":
    main()
