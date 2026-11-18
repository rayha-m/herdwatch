#!/usr/bin/env python3
"""Extract every *complete* AVI from the Google-Drive ZIP export, even while
the ZIP parts are still downloading (Chrome `.crdownload` files).

Drive writes streaming ZIP entries (deflate + data descriptor), so we walk the
local headers and inflate each entry until its deflate stream ends; the entry
is only kept if the CRC matches. Re-running is idempotent: finished videos are
skipped, so you can run it repeatedly as downloads progress.

    python tools/extract_videos.py --src ~/Downloads/hackathon --dst ../videos
"""
import argparse, glob, os, struct, zlib

CHUNK = 1 << 20


def walk(path, dst, verbose=True):
    done = 0
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        off = 0
        while off + 30 <= size:
            fh.seek(off)
            h = fh.read(30)
            if h[:4] != b"PK\x03\x04":
                break
            flag, meth = struct.unpack("<HH", h[6:10])
            crc_h, csz, usz = struct.unpack("<III", h[14:26])
            nl, el = struct.unpack("<HH", h[26:30])
            name = fh.read(nl).decode("utf-8", "replace")
            fh.seek(off + 30 + nl + el)
            rel = name.split("/", 1)[1] if "/" in name else name  # drop top folder
            out = os.path.join(dst, rel)
            if name.endswith("/"):
                off = off + 30 + nl + el
                continue
            is_video = name.lower().endswith(".avi")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            tmp = out + ".part"
            d = zlib.decompressobj(-15) if meth == 8 else None
            crc = 0
            consumed = 0
            write = is_video and not os.path.exists(out)
            w = open(tmp, "wb") if write else None
            ended = False
            while True:
                buf = fh.read(CHUNK)
                if not buf:
                    break
                if d is None:  # stored
                    raise RuntimeError("stored entries unsupported")
                data = d.decompress(buf)
                if write and data:
                    w.write(data)
                    crc = zlib.crc32(data, crc)
                if d.eof:
                    consumed += len(buf) - len(d.unused_data)
                    ended = True
                    break
                consumed += len(buf)
            if w:
                w.close()
            if not ended:  # entry not fully downloaded yet
                if write and os.path.exists(tmp):
                    os.remove(tmp)
                return done
            data_end = off + 30 + nl + el + consumed
            # data descriptor (optional signature)
            fh.seek(data_end)
            dd = fh.read(16)
            if dd[:4] == b"PK\x07\x08":
                crc_d = struct.unpack("<I", dd[4:8])[0]
                dlen = 16
            else:
                crc_d = struct.unpack("<I", dd[0:4])[0]
                dlen = 12
            if not (flag & 8):
                crc_d, dlen = crc_h, 0
            if write:
                if crc != crc_d:
                    os.remove(tmp)
                    print(f"  CRC mismatch, skipped: {rel}")
                else:
                    os.replace(tmp, out)
                    done += 1
                    if verbose:
                        print(f"  extracted {rel}")
            off = data_end + dlen
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="folder with Drive zip parts (.zip or .crdownload)")
    ap.add_argument("--dst", required=True)
    a = ap.parse_args()
    parts = sorted(glob.glob(os.path.join(a.src, "*.crdownload")) + glob.glob(os.path.join(a.src, "*.zip")))
    total = 0
    for p in parts:
        try:
            total += walk(p, a.dst)
        except Exception as e:  # a part that is not a video zip etc.
            print(f"skip {os.path.basename(p)}: {e}")
    vids = glob.glob(os.path.join(a.dst, "**", "*.[Aa]vi"), recursive=True)
    print(f"new: {total}   total complete videos: {len(vids)}")


if __name__ == "__main__":
    main()
