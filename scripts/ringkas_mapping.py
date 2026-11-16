#!/usr/bin/env python3
"""ringkas_mapping.py — Ringkasan graf yang sedang tampil, bahan Brainstorm.

UNTUK APA
Mode "Summarize this graph": brainstorming atas graf yang sedang tampil (dari
view, kotak Cypher, atau Ask). Graf bisa berisi ribuan node, jadi diringkas
menjadi fakta yang dihitung program: siapa yang paling terhubung, topik dan
unit yang terlibat, pasangan yang layak dibandingkan, dan maksimal 5 abstrak.

ID NODE TIDAK SERAGAM
Graf di sre.html dibentuk dua cara (visual/server.py):
  - bentuk_dari_hasil: id = @rid ArcadeDB ("#8:0") -- kotak Cypher, Ask, dan
    sebagian besar view
  - pola skalar (bentuk_dari_skalar): id = kunci per tipe (local_id peneliti,
    topik_id, nama kategori, kode unit, dedup_key publikasi)
Keduanya diterjemahkan ke kunci per tipe. @rid dicari lewat PETA seluruh
node (~0,2 detik), bukan `WHERE id(n) IN [...]` (11,7 detik untuk 3.000
publikasi).
Peta disimpan per proses: sesudah basis data dibangun ulang, restart server.

BACA-SAJA, dgn parameter. Tidak memanggil LLM.

Pemakaian (menguji lewat jalur yang SAMA dgn halaman):
    .venv/bin/python scripts/ringkas_mapping.py --view pakar_topik
    .venv/bin/python scripts/ringkas_mapping.py --cypher "MATCH (p:Peneliti)-[r:MENULIS]->(k) RETURN p, r, k LIMIT 50"
    .venv/bin/python scripts/ringkas_mapping.py --view unit_kerja --json
    .venv/bin/python scripts/ringkas_mapping.py --uji        # 9 view + 2 Cypher
"""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

AKAR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AKAR / "scripts"))

from banding import _judul_kunci, _judul_sama, _q  # noqa: E402

MAKS_NODE = 3000          # graf lebih besar: yang diringkas 3.000 node paling terhubung
MAKS_TOKOH = 5
MAKS_TOPIK = 5
MAKS_SARAN = 3
MAKS_KANDIDAT_SARAN = 30  # peneliti paling terhubung yang dicarikan pasangan
MAKS_ABSTRAK = 5
POTONG_ABSTRAK = 800

KUNCI = {"Peneliti": "local_id", "Publikasi": "dedup_key", "Topik": "topik_id",
         "Kategori": "nama", "UnitKerja": "kode", "Institusi": "nama", "Venue": "nama"}

_PETA_RID = None


def peta_rid():
    """{@rid: (tipe, kunci)} untuk SEMUA node. Sekali per proses."""
    global _PETA_RID
    if _PETA_RID is None:
        _PETA_RID = {}
        for tipe, k in KUNCI.items():
            for r in _q(f"MATCH (n:{tipe}) RETURN elementId(n) AS e, n.{k} AS k"):
                _PETA_RID[r["e"]] = (tipe, r["k"])
    return _PETA_RID


def kanonik(nodes):
    """node graf -> {id_graf: (tipe, kunci)}; yang tak dikenali dikembalikan terpisah."""
    peta, hasil, asing = None, {}, []
    for n in nodes:
        nid, tipe = str(n.get("id")), n.get("tipe")
        if nid.startswith("#"):
            peta = peta or peta_rid()
            if nid in peta:
                hasil[nid] = peta[nid]
            else:
                asing.append(nid)
        elif tipe in KUNCI:
            hasil[nid] = (tipe, nid)
        else:
            asing.append(nid)
    return hasil, asing


def ringkas_mapping(nodes, edges=None, sumber=None):
    """nodes: [{id, tipe}], edges: [{source, target}] -- bentuk data sre.html."""
    t0 = time.time()
    edges = edges or []
    derajat = Counter()
    for e in edges:
        derajat[str(e.get("source"))] += 1
        derajat[str(e.get("target"))] += 1

    terlalu_besar = len(nodes) > MAKS_NODE
    if terlalu_besar:
        nodes = sorted(nodes, key=lambda n: -derajat[str(n.get("id"))])[:MAKS_NODE]

    peta, asing = kanonik(nodes)
    per_tipe = defaultdict(dict)                # tipe -> {kunci: derajat di graf}
    for nid, (tipe, kunci) in peta.items():
        per_tipe[tipe][kunci] = per_tipe[tipe].get(kunci, 0) + derajat[nid]

    # Peneliti
    pen = per_tipe.get("Peneliti", {})
    info = {r["id"]: r for r in _q("""
MATCH (p:Peneliti) WHERE p.local_id IN $ids
OPTIONAL MATCH (u:UnitKerja)-[:MEMILIKI]->(p)
OPTIONAL MATCH (u)-[:AFILIASI]->(i:Institusi)
RETURN p.local_id AS id, p.nama AS nama, p.is_internal AS internal,
       u.kode AS unit, i.nama AS inst""", {"ids": list(pen)})} if pen else {}
    internal = [i for i in pen if info.get(i, {}).get("internal")]
    cacah_pub = {r["id"]: r["n"] for r in _q("""
MATCH (p:Peneliti)-[:MENULIS]->(k:Publikasi) WHERE p.local_id IN $ids
RETURN p.local_id AS id, count(k) AS n""", {"ids": internal})} if internal else {}
    urut_internal = sorted(internal, key=lambda i: (-pen[i], -cacah_pub.get(i, 0)))
    tokoh = [{"id": i, "nama": info[i]["nama"], "unit": info[i]["unit"],
              "institusi": info[i]["inst"], "koneksi_di_graf": pen[i],
              "publikasi": cacah_pub.get(i, 0)} for i in urut_internal[:MAKS_TOKOH]]

    unit = Counter(info[i]["unit"] for i in internal if info[i]["unit"])
    inst = Counter(info[i]["inst"] for i in internal if info[i]["inst"])
    for k in per_tipe.get("UnitKerja", {}):
        unit.setdefault(k, 0)
    for k in per_tipe.get("Institusi", {}):
        inst.setdefault(k, 0)

    # Topik dan kategori
    top = per_tipe.get("Topik", {})
    topik, kategori = [], Counter()
    if top:
        rows = {r["id"]: r for r in _q("""
MATCH (t:Topik) WHERE t.topik_id IN $ids
OPTIONAL MATCH (t)-[:TERMASUK_DALAM]->(k:Kategori {skema: 'ASJC'})
RETURN t.topik_id AS id, t.nama AS nama, t.jumlah_dok AS dok, k.nama AS kat""",
                                        {"ids": list(top)})}
        for t in sorted(top, key=lambda t: (-top[t], -(rows.get(t, {}).get("dok") or 0)))[:MAKS_TOPIK]:
            r = rows.get(t, {})
            topik.append({"id": t, "nama": r.get("nama"), "kategori": r.get("kat"),
                          "koneksi_di_graf": top[t], "publikasi": r.get("dok")})
        kategori.update(r["kat"] for r in rows.values() if r.get("kat"))
    for k in per_tipe.get("Kategori", {}):
        kategori[k] += 0

    # Saran perbandingan: SEBIDANG yang belum pernah menulis bersama
    saran = []
    kandidat = urut_internal[:MAKS_KANDIDAT_SARAN]
    if len(kandidat) >= 2:
        sebidang = {frozenset((r["a"], r["b"])): r["s"] for r in _q("""
MATCH (a:Peneliti)-[r:SEBIDANG]-(b:Peneliti)
WHERE a.local_id IN $ids AND b.local_id IN $ids
RETURN a.local_id AS a, b.local_id AS b, r.skor AS s""", {"ids": kandidat})}
        kolab = {frozenset((r["a"], r["b"])) for r in _q("""
MATCH (a:Peneliti)-[:KOLABORASI]-(b:Peneliti)
WHERE a.local_id IN $ids AND b.local_id IN $ids
RETURN a.local_id AS a, b.local_id AS b""", {"ids": kandidat})}
        calon = []
        for a, b in combinations(kandidat, 2):
            s = sebidang.get(frozenset((a, b)))
            if s is None or frozenset((a, b)) in kolab:
                continue
            lintas = info[a]["inst"] != info[b]["inst"]
            calon.append((lintas, s, a, b))
        # Lintas institusi didahulukan karena paling jarang tergarap.
        # Tiap orang paling banyak sekali, agar saran tidak dikuasai satu orang.
        dipakai = set()
        terpilih = []
        for c in sorted(calon, key=lambda c: (-c[0], -c[1])):
            if c[2] in dipakai or c[3] in dipakai:
                continue
            dipakai.update((c[2], c[3]))
            terpilih.append(c)
            if len(terpilih) >= MAKS_SARAN:
                break
        for lintas, s, a, b in terpilih:
            alasan = [f"similar topic profile {s:.2f}", "not yet co-authored"]
            if lintas:
                alasan.append("cross-institution")
            elif info[a]["unit"] != info[b]["unit"]:
                alasan.append("cross-unit")
            saran.append({"ids": [a, b], "nama": [info[a]["nama"], info[b]["nama"]],
                          "alasan": ", ".join(alasan)})

    # Abstrak: publikasi paling sentral di graf
    pubs = per_tipe.get("Publikasi", {})
    pilih, sumber_abstrak = [], None
    if pubs:
        sumber_abstrak = "publications_in_mapping"
        rows = _q("""
MATCH (k:Publikasi) WHERE k.dedup_key IN $ids AND k.abstrak IS NOT NULL
RETURN k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun, k.sitasi AS sitasi""",
                  {"ids": list(pubs)})
        rows.sort(key=lambda r: (-pubs[r["kunci"]], -(r["sitasi"] or 0), -(r["tahun"] or 0)))
    elif urut_internal:
        sumber_abstrak = "researchers_in_mapping"
        tids = [t["id"] for t in topik]
        rows = _q("""
MATCH (p:Peneliti)-[:MENULIS]->(k:Publikasi)-[m:MEMBAHAS]->(t:Topik)
WHERE p.local_id IN $ids AND k.abstrak IS NOT NULL AND (size($tids) = 0 OR t.topik_id IN $tids)
RETURN p.local_id AS id, k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun,
       m.skor AS skor""", {"ids": urut_internal[:MAKS_TOKOH], "tids": tids})
        # bergiliran antar tokoh, yang paling dekat ke topiknya lebih dulu
        antre = defaultdict(list)
        for r in sorted(rows, key=lambda r: -(r["skor"] or 0)):
            antre[r["id"]].append(r)
        rows = [r for grup in zip(*antre.values()) for r in grup] + \
               [r for g in antre.values() for r in g]
    elif topik:
        # Graf tanpa peneliti & publikasi (mis. asjc_dua_tingkat): publikasi
        # yang paling dekat ke topik-topik di graf, bergiliran antar topik.
        sumber_abstrak = "topics_in_mapping"
        antre = defaultdict(list)
        for r in sorted(_q("""
MATCH (k:Publikasi)-[m:MEMBAHAS]->(t:Topik)
WHERE t.topik_id IN $tids AND k.abstrak IS NOT NULL
RETURN t.topik_id AS t, k.dedup_key AS kunci, k.judul AS judul, k.tahun AS tahun,
       m.skor AS skor""", {"tids": [t["id"] for t in topik]}), key=lambda r: -(r["skor"] or 0)):
            antre[r["t"]].append(r)
        rows = [r for grup in zip(*antre.values()) for r in grup]
    else:
        rows = []
    for r in rows:
        if len(pilih) >= MAKS_ABSTRAK:
            break
        if r["kunci"] in [p["kunci"] for p in pilih] or _judul_sama(
                _judul_kunci(r["judul"]), {_judul_kunci(p["judul"]) for p in pilih}):
            continue
        pilih.append(r)
    teks = {r["kunci"]: r["abstrak"] for r in _q(
        "MATCH (k:Publikasi) WHERE k.dedup_key IN $k RETURN k.dedup_key AS kunci, k.abstrak AS abstrak",
        {"k": [p["kunci"] for p in pilih]})} if pilih else {}
    abstrak = [{"kunci": p["kunci"], "judul": p["judul"], "tahun": p["tahun"],
                "abstrak": (teks.get(p["kunci"]) or "")[:POTONG_ABSTRAK]} for p in pilih]

    return {
        "sumber": sumber,
        "jumlah_node": len(peta) + len(asing), "jumlah_relasi": len(edges),
        "terlalu_besar": terlalu_besar,
        "per_tipe": {t: len(v) for t, v in sorted(per_tipe.items(), key=lambda x: -len(x[1]))},
        "peneliti_internal": len(internal), "peneliti_eksternal": len(pen) - len(internal),
        "tokoh": tokoh, "unit": dict(unit.most_common()), "institusi": dict(inst.most_common()),
        "topik": topik, "kategori": dict(kategori.most_common(5)),
        "saran_perbandingan": saran,
        "abstrak": abstrak, "sumber_abstrak": sumber_abstrak,
        "node_tak_dikenal": len(asing),
        "detik": round(time.time() - t0, 2),
    }


# CLI
def graf_dari(view=None, cypher=None):
    """Bentuk graf lewat jalur YANG SAMA dgn halaman (visual/server.py)."""
    sys.path.insert(0, str(AKAR / "visual"))
    import requests
    from ekspor_visual import ARCADEDB_AUTH, ARCADEDB_URL, CONTOH_PARAM, VIEW
    from prompt_graf import _isi_contoh
    from server import graf_cytoscape
    if view:
        if view not in VIEW:
            sys.exit(f"View tidak dikenal: {view}")
        cypher = _isi_contoh(str(VIEW[view]["cypher"]), CONTOH_PARAM)
    r = requests.post(ARCADEDB_URL, auth=ARCADEDB_AUTH, timeout=300,
                      json={"language": "opencypher", "command": cypher})
    r.raise_for_status()
    g = graf_cytoscape(r.json().get("result", [])) or {"nodes": [], "edges": []}
    return ([n["data"] for n in g["nodes"]], [e["data"] for e in g["edges"]])


UJI_VIEW = ["pakar_topik", "kolaborasi_lintas_institusi", "unit_kerja", "individu",
            "semua_internal", "ontologi_penuh", "author_network", "venue_unit",
            "asjc_dua_tingkat"]
UJI_CYPHER = [
    "MATCH (p:Peneliti {is_internal:true})-[r:MENULIS]->(pub:Publikasi) "
    "WHERE toLower(pub.judul) CONTAINS 'machine learning' RETURN p, r, pub LIMIT 300",
    "MATCH (a:Peneliti {local_id:'PRSDI016'})-[r:KOLABORASI]-(b:Peneliti) RETURN a, r, b LIMIT 100",
]


def uji():
    """Graf nyata dari view & Cypher, lewat jalur yang sama dgn halaman."""
    gagal = 0
    kasus = [("view", v) for v in UJI_VIEW] + [("cypher", c) for c in UJI_CYPHER]
    for jenis, isi in kasus:
        nodes, edges = graf_dari(isi if jenis == "view" else None, isi if jenis == "cypher" else None)
        h = ringkas_mapping(nodes, edges, {jenis: isi[:40]})
        salah = []
        if h["node_tak_dikenal"]:
            salah.append(f"{h['node_tak_dikenal']} node tak dikenali")
        if len(nodes) > MAKS_NODE and not h["terlalu_besar"]:
            salah.append("graf besar tidak ditandai")
        orang = [x for s in h["saran_perbandingan"] for x in s["ids"]]
        if len(orang) != len(set(orang)):
            salah.append("satu orang muncul >1x di saran")
        kunci = [a["kunci"] for a in h["abstrak"]]
        if len(kunci) > MAKS_ABSTRAK or len(kunci) != len(set(kunci)):
            salah.append("abstrak melebihi maks / ganda")
        if any(len(a["abstrak"]) > POTONG_ABSTRAK for a in h["abstrak"]):
            salah.append("abstrak tidak dipotong")
        if (h["tokoh"] or h["topik"]) and not h["abstrak"]:
            salah.append("ada tokoh/topik tapi tanpa abstrak")
        if h["detik"] > 3:
            salah.append(f"lambat {h['detik']} dtk")
        gagal += bool(salah)
        print(f"  {'OK   ' if not salah else 'GAGAL'} {jenis:6} {isi[:44]:44} "
              f"{h['jumlah_node']:5,} node | tokoh {len(h['tokoh'])} | saran {len(h['saran_perbandingan'])} "
              f"| abstrak {len(h['abstrak'])} ({h['sumber_abstrak']}) | {h['detik']} dtk"
              + (" | BESAR" if h["terlalu_besar"] else ""))
        for s in salah:
            print(f"          !! {s}")
    print(f"\n{'=' * 60}\n  {'LULUS' if not gagal else f'{gagal} GAGAL'}\n{'=' * 60}")
    sys.exit(1 if gagal else 0)


def cetak(h):
    print("=" * 70)
    print(f"  sumber: {h['sumber']} | {h['jumlah_node']:,} node, {h['jumlah_relasi']:,} relasi"
          + (" | TERLALU BESAR: diringkas 3.000 node paling terhubung" if h["terlalu_besar"] else ""))
    print(f"  per tipe: {h['per_tipe']}")
    print(f"  peneliti: {h['peneliti_internal']} internal, {h['peneliti_eksternal']} eksternal"
          f" | unit {h['unit']} | institusi {h['institusi']}")
    for t in h["tokoh"]:
        print(f"    tokoh: {t['nama']} ({t['unit']}/{t['institusi']}) koneksi {t['koneksi_di_graf']}, "
              f"{t['publikasi']} publikasi")
    for t in h["topik"]:
        print(f"    topik: {(t['nama'] or '')[:45]} [{t['kategori']}] koneksi {t['koneksi_di_graf']}")
    print(f"  kategori: {h['kategori']}")
    for s in h["saran_perbandingan"]:
        print(f"    saran banding: {' x '.join(s['nama'])} -- {s['alasan']}")
    print(f"  abstrak ({len(h['abstrak'])}, sumber {h['sumber_abstrak']}):")
    for a in h["abstrak"]:
        print(f"    {a['tahun']} {(a['judul'] or '')[:60]} ({len(a['abstrak'])} huruf)")
    if h["node_tak_dikenal"]:
        print(f"  PERINGATAN: {h['node_tak_dikenal']} node tak dikenali")
    print(f"  waktu ringkas: {h['detik']} dtk")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--uji", action="store_true", help="uji 9 view + 2 Cypher")
    g.add_argument("--view", help="nama view (nilai contoh parameter dipakai)")
    g.add_argument("--cypher", help="Cypher bebas, seperti kotak query / Ask")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.uji:
        return uji()
    nodes, edges = graf_dari(a.view, a.cypher)
    if not nodes:
        sys.exit("Graf kosong -- tidak ada yang diringkas.")
    h = ringkas_mapping(nodes, edges, {"view": a.view} if a.view else {"cypher": a.cypher[:120]})
    print(json.dumps(h, ensure_ascii=False, indent=1) if a.json else "", end="")
    if not a.json:
        cetak(h)


if __name__ == "__main__":
    main()
