#!/usr/bin/env python3
"""uji_paket_ab.py — Uji endpoint pencarian dan Compare publikasi, tanpa LLM.

    /api/cari                 cari judul publikasi & nama peneliti di basis data
    /api/publikasi_peneliti   semua publikasi satu peneliti ("Show all")
    /api/node                 detail node, termasuk rid + kunci "Latest publications"
    /api/banding_publikasi    Compare 2-3 publikasi

Server harus sudah jalan. Pemakaian:
    .venv/bin/python scripts/uji_paket_ab.py
    .venv/bin/python scripts/uji_paket_ab.py --port 8001
"""
import argparse
import sys

import requests

KASUS_PUB = ["doi:10.11591/eei.v14i3.7929", "doi:10.1109/ic3ina68387.2025.11325615",
             "doi:10.46799/ajesh.v4i12.710"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    url = f"http://localhost:{ap.parse_args().port}"
    gagal = 0

    def cek(nama, jalur, muatan, harap_kode, syarat=lambda d: True):
        nonlocal gagal
        try:
            r = requests.post(url + jalur, json=muatan, timeout=60)
            d = r.json()
            ok = r.status_code == harap_kode and syarat(d)
        except Exception as e:
            ok, r, d = False, None, str(e)
        gagal += not ok
        print(f"  {'OK   ' if ok else 'GAGAL'} {nama:44} {r.status_code if r is not None else '-'}")
        if not ok:
            print(f"        -> {str(d)[:160]}")
        return d

    print("cari")
    cek("judul: 'deep learning tea' (semua kata)", "/api/cari", {"teks": "deep learning tea"}, 200,
        lambda d: d["total_publikasi"] >= 1 and "Tea" in d["publikasi"][0]["judul"])
    cek("nama peneliti: 'devi'", "/api/cari", {"teks": "devi"}, 200,
        lambda d: any(p["nama"] == "Devi Munandar" for p in d["peneliti"]))
    cek("dibatasi 50", "/api/cari", {"teks": "an"}, 200,
        lambda d: len(d["publikasi"]) <= d["batas"] == 50 and d["total_publikasi"] > 50)
    cek("tolak: 1 huruf", "/api/cari", {"teks": "x"}, 400)
    cek("tolak: > 100 huruf", "/api/cari", {"teks": "a" * 101}, 400)
    cek("kutip tidak menyusup", "/api/cari", {"teks": "x' OR 1=1 //"}, 200,
        lambda d: d["total_publikasi"] == 0)

    print("publikasi peneliti")
    cek("Ana Hadiana = 93", "/api/publikasi_peneliti", {"id": "PRSDI004"}, 200,
        lambda d: len(d["publikasi"]) == 93 and d["publikasi"][0]["rid"].startswith("#"))
    cek("tolak: tidak ada", "/api/publikasi_peneliti", {"id": "TIDAKADA"}, 404)
    cek("tolak: tanpa id", "/api/publikasi_peneliti", {}, 400)

    print("detail node")
    cek("peneliti: rid + kunci terbaru", "/api/node", {"id": "PRSDI004"}, 200,
        lambda d: d["rid"] and all(x.get("kunci") for x in d["daftar"][3]["isi"]))

    print("banding publikasi")
    cek("3 publikasi: bridge, terkait <= 5", "/api/banding_publikasi", {"kunci": KASUS_PUB}, 200,
        lambda d: d["bukti"]["tingkat"] == "bridge" and len(d["bukti"]["pasangan"]) == 3
        and len(d["bukti"]["publikasi_terkait"]) <= 5 and len(d["rid"]) == 3 and d["graf"]["nodes"])
    cek("2 publikasi penulis sama: direct", "/api/banding_publikasi", {"kunci": KASUS_PUB[:2]}, 200,
        lambda d: d["bukti"]["tingkat"] == "direct"
        and d["bukti"]["pasangan"][0]["penulis_bersama"])
    cek("tolak: 1 publikasi", "/api/banding_publikasi", {"kunci": KASUS_PUB[:1]}, 400)
    cek("tolak: 4 publikasi", "/api/banding_publikasi", {"kunci": KASUS_PUB + ["a"]}, 400)
    cek("tolak: kunci tidak ada", "/api/banding_publikasi", {"kunci": ["doi:tidak", KASUS_PUB[0]]}, 400)
    cek("tolak: bukan daftar", "/api/banding_publikasi", {"kunci": "x"}, 400)

    print("=" * 60)
    print("  LULUS" if not gagal else f"  GAGAL {gagal}")
    print("=" * 60)
    sys.exit(1 if gagal else 0)


if __name__ == "__main__":
    main()
