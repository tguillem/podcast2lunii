"""Generate 320x240 4-bit (16-color) BMPs for the Lunii/STUdio pack format.

The device screen is low quality; assets in reference packs are 320x240,
4bpp, palette of 16 colors, stored either BI_RGB (uncompressed) or BI_RLE4.
This module builds either, from any source image, with optional numbered
badge overlay for per-episode covers.
"""
import io
import struct
from PIL import Image, ImageDraw, ImageFont

W, H = 320, 240


def fit_cover(img, w=W, h=H, bg=(0, 0, 0)):
    """Resize `img` to fill wxh (cover crop), RGB."""
    img = img.convert("RGB")
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = round(src_w * scale), round(src_h * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _load_font(size, bold=True):
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default()


def add_number_badge(img, number, corner="br"):
    """Draw a big circular number badge on an RGB 320x240 image."""
    img = img.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    d = 92                      # badge diameter
    m = 12                      # margin from edges
    if corner == "br":
        x0, y0 = W - d - m, H - d - m
    elif corner == "bl":
        x0, y0 = m, H - d - m
    else:
        x0, y0 = W - d - m, m
    x1, y1 = x0 + d, y0 + d
    # solid disc with contrasting ring
    draw.ellipse([x0 - 4, y0 - 4, x1 + 4, y1 + 4], fill=(255, 255, 255))
    draw.ellipse([x0, y0, x1, y1], fill=(210, 40, 40))
    font = _load_font(64)
    s = str(number)
    bb = draw.textbbox((0, 0), s, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((x0 + (d - tw) / 2 - bb[0], y0 + (d - th) / 2 - bb[1]),
              s, font=font, fill=(255, 255, 255))
    return img


def quantize16(img):
    """Return (indices bytes row-major top-down, palette list of (r,g,b) len<=16)."""
    img = img.convert("RGB")
    # Adaptive 16-color palette, no dithering keeps runs long for RLE.
    p = img.quantize(
        colors=16,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    pal_raw = p.getpalette()[:16 * 3]
    palette = [tuple(pal_raw[i:i + 3]) for i in range(0, len(pal_raw), 3)]
    while len(palette) < 16:
        palette.append((0, 0, 0))
    idx = p.tobytes()           # one byte per pixel, top-down
    return idx, palette


def _rows_bottom_up(idx, w, h):
    for y in range(h - 1, -1, -1):
        yield idx[y * w:(y + 1) * w]


def _encode_rle4(idx, w, h):
    """BI_RLE4 encode. idx: top-down index bytes. Returns compressed bytes."""
    out = bytearray()
    for row in _rows_bottom_up(idx, w, h):
        i = 0
        n = len(row)
        while i < n:
            # encoded run of a single color
            j = i
            while j < n and row[j] == row[i] and (j - i) < 255:
                j += 1
            run = j - i
            if run >= 3 or j == n or True:
                # emit as encoded run (count, (c<<4)|c) — always valid
                c = row[i]
                # but combine literal region for better ratio when run<3
                if run < 3:
                    # gather a literal absolute run until a >=3 run appears
                    lit_start = i
                    k = i
                    while k < n:
                        # look ahead for a run of >=3
                        m = k
                        while m < n and row[m] == row[k] and (m - k) < 255:
                            m += 1
                        if (m - k) >= 3:
                            break
                        k = m if (m - k) >= 1 else k + 1
                        if k - lit_start >= 254:
                            break
                    litlen = k - lit_start
                    if litlen >= 3:
                        # absolute mode
                        out += bytes((0x00, litlen))
                        packed = bytearray()
                        for t in range(litlen):
                            v = row[lit_start + t]
                            if t % 2 == 0:
                                packed.append(v << 4)
                            else:
                                packed[-1] |= v
                        # pad to even byte count (word alignment)
                        if len(packed) % 2:
                            packed.append(0)
                        out += packed
                        i = k
                        continue
                    # else fall through to short encoded run
                out += bytes((run, (c << 4) | c))
                i = j
        out += bytes((0x00, 0x00))      # end of line
    out += bytes((0x00, 0x01))          # end of bitmap
    return bytes(out)


def _encode_uncompressed4(idx, w, h):
    """BI_RGB 4bpp, rows padded to 4-byte boundary, bottom-up."""
    row_bytes = (w + 1) // 2
    pad = (-row_bytes) % 4
    out = bytearray()
    for row in _rows_bottom_up(idx, w, h):
        packed = bytearray()
        for t in range(0, w, 2):
            hi = row[t]
            lo = row[t + 1] if t + 1 < w else 0
            packed.append((hi << 4) | lo)
        packed += b"\x00" * pad
        out += packed
    return bytes(out)


def build_bmp(img, rle=True):
    """img: PIL image (any). Returns 4-bit BMP bytes, 320x240."""
    idx, palette = quantize16(fit_cover(img))
    comp = 2 if rle else 0
    bits = _encode_rle4(idx, W, H) if rle else _encode_uncompressed4(idx, W, H)
    # palette: 16 * BGRA
    pal = bytearray()
    for (r, g, b) in palette[:16]:
        pal += bytes((b, g, r, 0))
    bits_off = 14 + 40 + len(pal)
    info = struct.pack("<IiiHHIIiiII",
                       40, W, H, 1, 4, comp, len(bits), 0, 0, 0, 16)
    fh = struct.pack("<2sIHHI", b"BM", bits_off + len(bits), 0, 0, bits_off)
    return bytes(fh) + info + bytes(pal) + bits


def build_bmp_from_rgb(rgb_img, rle=True):
    """rgb_img already 320x240 RGB (e.g. with badge). Skip cover-fit."""
    if rgb_img.size != (W, H):
        rgb_img = fit_cover(rgb_img)
    idx, palette = quantize16(rgb_img)
    comp = 2 if rle else 0
    bits = _encode_rle4(idx, W, H) if rle else _encode_uncompressed4(idx, W, H)
    pal = bytearray()
    for (r, g, b) in palette[:16]:
        pal += bytes((b, g, r, 0))
    bits_off = 14 + 40 + len(pal)
    info = struct.pack("<IiiHHIIiiII",
                       40, W, H, 1, 4, comp, len(bits), 0, 0, 0, 16)
    fh = struct.pack("<2sIHHI", b"BM", bits_off + len(bits), 0, 0, bits_off)
    return bytes(fh) + info + bytes(pal) + bits


if __name__ == "__main__":
    import subprocess
    import sys
    # synthetic test image (gradient + shapes to exercise runs + literals)
    test = Image.new("RGB", (600, 600))
    px = test.load()
    for y in range(600):
        for x in range(600):
            px[x, y] = ((x * 255) // 600, (y * 255) // 600,
                        ((x + y) * 255) // 1200)
    badged = add_number_badge(fit_cover(test), 3)
    for rle in (True, False):
        data = build_bmp_from_rgb(badged, rle=rle)
        name = f"selftest_{'rle' if rle else 'raw'}.bmp"
        open(name, "wb").write(data)
        info = subprocess.run(["file", name], capture_output=True, text=True).stdout.strip()
        # decode back with ffmpeg to prove validity
        rc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", name,
                             name + ".png"], capture_output=True, text=True)
        ok = rc.returncode == 0
        print(f"{name}: {len(data)} bytes | {info.split(':',1)[1].strip()} | ffmpeg-decode={'OK' if ok else 'FAIL:'+rc.stderr.strip()}")
