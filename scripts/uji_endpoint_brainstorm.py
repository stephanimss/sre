#!/usr/bin/env python3
"""uji_endpoint_brainstorm.py — Uji endpoint Brainstorm (tanpa memanggil model bahasa).

Server harus sudah berjalan.

Pemakaian:
    .venv/bin/python scripts/uji_endpoint_brainstorm.py [port]
"""
import json, sys, time, urllib.request, urllib.error
sys.path.insert(0, "scripts")
from ekspor_visual import VIEW, CONTOH_PARAM
from prompt_graf import _isi_contoh

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASIS = f"http://127.0.0.1:{PORT}"
gagal = 0

def get(p):
    with urllib.request.urlopen(BASIS + p, timeout=120) as r:
        return r.status, json.load(r)

def post(p, body):
    req = urllib.request.Request(BASIS + p, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.load(r), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), time.time() - t

def cek(nama, ok, ket=""):
    global gagal
    gagal += not ok
    print(f"  {'OK   ' if ok else 'GAGAL'} {nama:44} {ket}")

try:
    s, d = get("/api/peneliti")
except Exception as e:
    sys.exit(f"Server di port {PORT} tidak merespons ({e}). Jalankan dulu: cd visual && ../.venv/bin/python server.py")
cek("GET /api/peneliti", s == 200 and len(d) == 96 and all(k in d[0] for k in ("id", "rid", "nama", "unit", "institusi")),
    f"{len(d)} peneliti, contoh {d[0]['nama']}")

for ids, harap in ((["PRSDI016", "PRSDI043"], "direct"), (["PRSDI006", "PRSDI015"], "bridge"),
                   (["PRSDI016", "PRSDI043", "PTIUB013"], "bridge")):
    s, d, dt = post("/api/banding", {"ids": ids})
    if s != 200:
        cek(f"POST /api/banding {'+'.join(ids)}", False, str(d)[:80]); continue
    g = d["graf"]; node_id = {n["data"]["id"] for n in g["nodes"]}
    ok = (d["bukti"]["tingkat"] == harap and set(d["rid"]) == set(ids)
          and all(r in node_id for r in d["rid"].values()) and dt < 8)
    tipe = {}
    for n in g["nodes"]:
        tipe[n["data"]["tipe"]] = tipe.get(n["data"]["tipe"], 0) + 1
    cek(f"POST /api/banding {'+'.join(ids)}", ok, f"{d['bukti']['tingkat']} | graf {tipe} | {dt:.1f} dtk")

for body, lab, kode, isi in (({"ids": ["PRSDI016"]}, "1 peneliti", 400, "Pilih 2 sampai"),
                             ({"ids": ["PRSDI016", "TIDAKADA"]}, "id tidak ada", 400, "tidak ditemukan"),
                             ({"ids": "PRSDI016"}, "ids bukan daftar", 400, "butuh ids")):
    s, d, _ = post("/api/banding", body)
    cek(f"tolak banding: {lab}", s == kode and isi in d.get("error", ""), f"{s} {d.get('error')}")

for v, besar in (("pakar_topik", False), ("unit_kerja", False), ("individu", False), ("ontologi_penuh", True)):
    s, g, _ = post("/api/query", {"cypher": _isi_contoh(str(VIEW[v]["cypher"]), CONTOH_PARAM), "format": "cytoscape"})
    nodes = [n["data"] for n in g["nodes"]]; edges = [e["data"] for e in g["edges"]]
    s2, h, dt = post("/api/ringkas", {"nodes": nodes, "edges": edges, "sumber": {"view": v}})
    ok = (s2 == 200 and h["node_tak_dikenal"] == 0 and h["terlalu_besar"] == besar
          and len(h["abstrak"]) == 5 and dt < 5)
    cek(f"POST /api/ringkas {v}", ok, f"{len(nodes):,} node | tokoh {len(h.get('tokoh', []))} "
        f"saran {len(h.get('saran_perbandingan', []))} abstrak {len(h.get('abstrak', []))} | {dt:.1f} dtk"
        + (" | BESAR" if h.get("terlalu_besar") else ""))

for body, lab, kode in (({"nodes": []}, "graf kosong", 400), ({"nodes": "x"}, "nodes bukan daftar", 400),
                        ({"nodes": [{"id": "#1:1", "tipe": "X"}] * 20001}, "terlalu besar", 413)):
    s, d, _ = post("/api/ringkas", body)
    cek(f"tolak ringkas: {lab}", s == kode, f"{s} {d.get('error')}")

# /api/brainstorm: kasus yang ditolak sebelum memanggil LLM
for body, lab, kode in (({"pesan": "ideas?"}, "tanpa sid & tanpa bukti", 400),
                        ({"pesan": "ideas?", "mode": "lain", "bukti": {}}, "mode tak dikenal", 400),
                        ({"sid": "tidak-ada", "pesan": "ideas?"}, "sid tak dikenal", 410),
                        ({"mode": "compare", "bukti": {"peneliti": []}, "pesan": ""}, "pesan kosong", 400),
                        ({"mode": "compare", "bukti": {"peneliti": []}, "pesan": "x" * 601}, "pesan > 600 huruf", 400)):
    s, d, _ = post("/api/brainstorm", body)
    cek(f"tolak brainstorm: {lab}", s == kode, f"{s} {d.get('error')}")

s, d = get("/api/view");                          cek("endpoint lama: GET /api/view", s == 200 and len(d) == 26, f"{len(d)} view")
s, d, _ = post("/api/node", {"id": "PRSDI016"});  cek("endpoint lama: POST /api/node", s == 200 and d.get("nama") == "Devi Munandar")
s, d, _ = post("/api/query", {"cypher": "MATCH (p:Peneliti {local_id:'PRSDI016'}) RETURN p", "format": "cytoscape"})
cek("endpoint lama: POST /api/query", s == 200 and len(d["nodes"]) == 1)
s, d, _ = post("/api/tidak-ada", {});             cek("rute tak dikenal -> 404", s == 404 and "/api/brainstorm" in d.get("error", ""))

print(f"\n{'=' * 60}\n  {'LULUS' if not gagal else f'{gagal} GAGAL'}\n{'=' * 60}")
sys.exit(1 if gagal else 0)
