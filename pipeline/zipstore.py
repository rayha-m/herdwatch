"""Read the event videos straight out of the Google-Drive ZIP parts (no full extraction).

The Drive export is ~150 GB of ZIP parts; extracting doubles disk use. Instead:
  * build_index() walks every part once and records, for each AVI entry, the part file,
    the offset of its deflate stream, its compressed length and whether the CRC verified.
    Parts may still be named `Unconfirmed NNN.crdownload` or renamed to .zip later;
    entries are re-resolved by scanning if a part path disappears.
  * stream(name) yields the decompressed bytes (fed to ffmpeg via stdin for bulk decoding).
  * materialize(name) extracts one file into work/video_cache (LRU, capped) for tools that
    need random access (agent zoom / time look-ups / evidence clips).

    python -m pipeline.zipstore --src ~/Downloads/hackathon      # build/refresh index
"""
import argparse, glob, json, os, shutil, struct, zlib, time
from pathlib import Path
from .config import WORK

INDEX = WORK / "zip_index.json"
CACHE = WORK / "video_cache"
CACHE_CAP_GB = float(os.environ.get("BV_CACHE_GB", "12"))
CHUNK = 1 << 20


def _walk(path):
    """Yield entry dicts for every complete entry in a (possibly partial) zip part."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        off = 0
        while off + 30 <= size:
            fh.seek(off); h = fh.read(30)
            if h[:4] != b"PK\x03\x04":
                return
            flag, meth = struct.unpack("<HH", h[6:10])
            crc_h, csz_h = struct.unpack("<II", h[14:22])
            nl, el = struct.unpack("<HH", h[26:30])
            name = fh.read(nl).decode("utf-8", "replace")
            data_off = off + 30 + nl + el
            fh.seek(data_off)
            if meth != 8:
                if meth == 0 and not (flag & 8):
                    off = data_off + csz_h; continue
                return
            d = zlib.decompressobj(-15); crc = 0; consumed = 0; ended = False
            is_video = name.lower().endswith(".avi")
            while True:
                buf = fh.read(CHUNK)
                if not buf:
                    break
                out = d.decompress(buf)
                if is_video:
                    crc = zlib.crc32(out, crc)
                if d.eof:
                    consumed += len(buf) - len(d.unused_data); ended = True; break
                consumed += len(buf)
            if not ended:
                return
            end = data_off + consumed
            fh.seek(end); dd = fh.read(16)
            if flag & 8:
                if dd[:4] == b"PK\x07\x08":
                    crc_d = struct.unpack("<I", dd[4:8])[0]; dlen = 16
                else:
                    crc_d = struct.unpack("<I", dd[0:4])[0]; dlen = 12
            else:
                crc_d, dlen = crc_h, 0
            if is_video:
                yield dict(name=name, part=str(path), data_off=data_off, comp=consumed, crc_ok=(crc == crc_d))
            off = end + dlen


def build_index(src):
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {"parts": {}, "entries": {}}
    parts = sorted(glob.glob(os.path.join(src, "*.crdownload")) + glob.glob(os.path.join(src, "*.zip")))
    for p in parts:
        sz = os.path.getsize(p)
        if idx["parts"].get(p) == sz:
            continue                      # unchanged since last scan
        t = time.time(); n = 0
        for e in _walk(p):
            idx["entries"][e["name"]] = e; n += 1
        idx["parts"][p] = sz
        print(f"  {os.path.basename(p)}: {n} videos ({time.time()-t:.0f}s)", flush=True)
        INDEX.write_text(json.dumps(idx))
    INDEX.write_text(json.dumps(idx))
    ok = sum(e["crc_ok"] for e in idx["entries"].values())
    print(f"indexed videos: {len(idx['entries'])}  CRC ok: {ok}")
    return idx


_idx = None


def entries():
    global _idx
    if _idx is None:
        _idx = json.loads(INDEX.read_text())["entries"]
    return _idx


def _resolve(e):
    if os.path.exists(e["part"]):
        return e
    # part renamed (e.g. Chrome finished: .crdownload -> .zip): rescan the folder
    src = os.path.dirname(e["part"])
    build_index(src)
    global _idx; _idx = None
    return entries()[e["name"]]


def stream(name):
    e = _resolve(entries()[name])
    d = zlib.decompressobj(-15); left = e["comp"]
    with open(e["part"], "rb") as fh:
        fh.seek(e["data_off"])
        while left > 0:
            buf = fh.read(min(CHUNK * 4, left)); left -= len(buf)
            if not buf:
                break
            out = d.decompress(buf)
            if out:
                yield out
        tail = d.flush()
        if tail:
            yield tail


_locks = {}
_locks_guard = __import__("threading").Lock()


def materialize(name):
    """Extract one entry to the LRU cache and return its path (thread-safe: one extraction per file)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    dst = CACHE / name.replace("/", "__")
    with _locks_guard:
        lock = _locks.setdefault(name, __import__("threading").Lock())
    with lock:
        if dst.exists():
            os.utime(dst); return str(dst)
        tmp = f"{dst}.{os.getpid()}.{__import__('threading').get_ident()}.part"
        with open(tmp, "wb") as f:
            for b in stream(name):
                f.write(b)
        os.replace(tmp, dst)
    files = sorted(CACHE.glob("*.Avi"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while total > CACHE_CAP_GB * 2**30 and len(files) > 1:
        p = files.pop(0); total -= p.stat().st_size; p.unlink()
    return str(dst)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--src", required=True); a = ap.parse_args()
    build_index(a.src)
