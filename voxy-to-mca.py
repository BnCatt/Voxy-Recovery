"""
Voxy RocksDB -> Minecraft .mca Converter
-----------------------------------------
Converts Voxy mod's RocksDB storage into standard Minecraft region (.mca) files.

References (voxy-dev source):
  - WorldEngine.java     : key encoding/decoding
  - SaveLoadSystem3.java : binary section layout
  - Mapper.java          : block ID / biome ID encoding

Performance:
  - Sections are decoded with numpy (no per-block Python loops)
  - Region files are written directly in the 1.18+ chunk format
    (no anvil-parser2, no per-block set_block calls)
  - Regions are built in parallel on all CPU cores

Known limitations:
  - Thin/special blocks (slabs, signs, fences, banners, fluids) may be missing or incorrect
  - Block entities (chest contents, signs text, ...) and entities are not stored by Voxy
  - Voxy LOD data is intended for visual rendering, not full survival restoration
"""

import argparse, gzip, io, os, struct, sys, time, zlib
from multiprocessing import Pool, freeze_support

try:
    import numpy as np
    import zstandard as zstd
    from rocksdict import Rdict, Options, AccessType
except ImportError as e:
    print(f"[-] Missing library ({e.name}): pip install rocksdict zstandard numpy")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
SECTION_VOLUME = 32 * 32 * 32   # 32768 blocks per Voxy section
LOD_LEVEL      = 0               # 0 = highest detail (full resolution)
MC_SEC_Y_MIN   = -4              # Y = -64
MC_SEC_Y_MAX   = 19              # Y = 319
DATA_VERSION   = 3955            # 1.21.1; newer game versions upgrade it on load
AIR_NAMES      = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
# ─────────────────────────────────────────────────────────────────────────────


def banner():
    print("=" * 60)
    print("  Voxy RocksDB -> Minecraft MCA Converter (fast)")
    print("  LOD level: 0 (full resolution)")
    print()
    print("  WARNING: Slabs, signs, fences, banners and fluids")
    print("  may be missing or incorrect. Block entities are lost.")
    print("=" * 60)
    print()


def parse_args():
    p = argparse.ArgumentParser(description="Convert Voxy RocksDB storage to .mca region files")
    p.add_argument("storage", nargs="?", help="path to the Voxy storage folder")
    p.add_argument("output", nargs="?", help="output folder for .mca files (default: region)")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                   help="number of worker processes (default: all CPU cores)")
    p.add_argument("--data-version", type=int, default=DATA_VERSION,
                   help=f"chunk DataVersion to write (default: {DATA_VERSION} = 1.21.1)")
    p.add_argument("--no-properties", action="store_true",
                   help="write block names only, drop block state properties")
    return p.parse_args()


def ask_paths(args):
    """Ask the user for the Voxy storage path and output folder (unless given as arguments)."""
    db_path = args.storage
    if not db_path:
        print("[?] Enter the path to your Voxy storage folder:")
        print("    (e.g. C:\\Users\\Name\\AppData\\Roaming\\.minecraft\\voxy\\...\\storage)")
        db_path = input("    > ").strip().strip('"').strip("'")

    if not os.path.isdir(db_path):
        print(f"[-] Directory not found: {db_path}")
        sys.exit(1)

    out = args.output
    if out is None:
        print()
        print("[?] Enter the output folder for .mca files:")
        print("    (Leave blank to use 'region')")
        out = input("    > ").strip().strip('"').strip("'")
    if not out:
        out = "region"

    return db_path, out


def decode_key(raw8: bytes):
    """
    WorldEngine.java::getWorldSectionId:
      ((long)lvl<<60) | ((long)(y&0xFF)<<52) | ((long)(z&0xFFFFFF)<<28) | ((long)(x&0xFFFFFF)<<4)

    Python integers are unbounded, so Java-style signed shifts don't work here.
    We read as unsigned and manually sign-extend each field.
    """
    val   = struct.unpack('>Q', raw8)[0]
    level = (val >> 60) & 0xF
    x_raw = (val >> 4)  & 0xFFFFFF
    y_raw = (val >> 52) & 0xFF
    z_raw = (val >> 28) & 0xFFFFFF
    x = x_raw if x_raw < (1 << 23) else x_raw - (1 << 24)
    y = y_raw if y_raw < (1 << 7)  else y_raw - (1 << 8)
    z = z_raw if z_raw < (1 << 23) else z_raw - (1 << 24)
    return level, x, y, z


# ── NBT reading (block state mappings) ───────────────────────────────────────

def _nbt_read(data, pos, tag):
    if tag == 1:  return data[pos], pos + 1
    if tag == 2:  return struct.unpack_from('>h', data, pos)[0], pos + 2
    if tag == 3:  return struct.unpack_from('>i', data, pos)[0], pos + 4
    if tag == 4:  return struct.unpack_from('>q', data, pos)[0], pos + 8
    if tag == 5:  return struct.unpack_from('>f', data, pos)[0], pos + 4
    if tag == 6:  return struct.unpack_from('>d', data, pos)[0], pos + 8
    if tag in (7, 11, 12):
        n = struct.unpack_from('>i', data, pos)[0]
        size = {7: 1, 11: 4, 12: 8}[tag]
        return None, pos + 4 + n * size
    if tag == 8:
        n = struct.unpack_from('>H', data, pos)[0]
        return data[pos + 2:pos + 2 + n].decode('utf-8', errors='replace'), pos + 2 + n
    if tag == 9:
        sub = data[pos]
        n = struct.unpack_from('>i', data, pos + 1)[0]
        pos += 5
        out = []
        for _ in range(n):
            v, pos = _nbt_read(data, pos, sub)
            out.append(v)
        return out, pos
    if tag == 10:
        out = {}
        while True:
            t = data[pos]
            pos += 1
            if t == 0:
                return out, pos
            n = struct.unpack_from('>H', data, pos)[0]
            name = data[pos + 2:pos + 2 + n].decode('utf-8', errors='replace')
            out[name], pos = _nbt_read(data, pos + 2 + n, t)
    raise ValueError(f"unknown NBT tag {tag}")


def _find_state(node):
    """Find the compound holding a namespaced 'Name' (and optional 'Properties')."""
    if isinstance(node, dict):
        name = node.get('Name')
        if isinstance(name, str) and ':' in name:
            props = node.get('Properties')
            if not isinstance(props, dict):
                props = {}
            return name, {k: str(v) for k, v in props.items() if isinstance(v, str)}
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_state(child)
        if found:
            return found
    return None


def _scan_block_name(data):
    """Fallback: find the first TAG_String named 'Name' -> e.g. 'minecraft:stone'"""
    pos = 0
    while pos < len(data) - 6:
        if data[pos] == 8:  # TAG_String
            name_len = struct.unpack_from('>H', data, pos + 1)[0]
            end      = pos + 3 + name_len
            if end + 2 <= len(data):
                tag_name = data[pos + 3:end].decode('utf-8', errors='ignore')
                val_len  = struct.unpack_from('>H', data, end)[0]
                if tag_name == 'Name' and 0 < val_len < 200:
                    val = data[end + 2:end + 2 + val_len].decode('utf-8', errors='ignore')
                    if ':' in val:
                        return val
        pos += 1
    return None


def parse_block_state(raw_nbt: bytes):
    """gzip-compressed NBT blob -> ('minecraft:oak_stairs', {'facing': 'north', ...}) or None"""
    try:
        with gzip.open(io.BytesIO(raw_nbt), 'rb') as f:
            data = f.read()
    except Exception:
        return None
    try:
        if data[0] == 10:
            n = struct.unpack_from('>H', data, 1)[0]
            root, _ = _nbt_read(data, 3 + n, 10)
            found = _find_state(root)
            if found:
                return found
    except Exception:
        pass
    name = _scan_block_name(data)
    return (name, {}) if name else None


# ── NBT writing (chunks) ─────────────────────────────────────────────────────

def _s(text):
    b = text.encode('utf-8')
    return struct.pack('>H', len(b)) + b


def _tag(tag_type, name):
    return bytes((tag_type,)) + _s(name)


def encode_palette_entry(name, props):
    """Payload of one block_states palette compound (without leading tag/name)."""
    out = _tag(8, 'Name') + _s(name)
    if props:
        out += _tag(10, 'Properties')
        for k in sorted(props):
            out += _tag(8, k) + _s(props[k])
        out += b'\x00'
    return out + b'\x00'


BIOMES_PLAINS = (_tag(10, 'biomes') + _tag(9, 'palette') + b'\x08' + struct.pack('>i', 1)
                 + _s('minecraft:plains') + b'\x00')


def pack_indices(idx, bits):
    """Pack 4096 palette indices into longs (1.16+ format: entries never span two longs)."""
    per    = 64 // bits
    nlongs = -(-4096 // per)
    a = np.zeros(nlongs * per, dtype=np.uint64)
    a[:4096] = idx
    a = a.reshape(nlongs, per) << (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    return nlongs, np.bitwise_or.reduce(a, axis=1).astype('>u8').tobytes()


def encode_section(sec_y, states):
    """states: 4096 global state indices in Y,Z,X order -> section compound payload."""
    palette, local = np.unique(states, return_inverse=True)
    out = _tag(1, 'Y') + struct.pack('>b', sec_y)
    out += _tag(10, 'block_states') + _tag(9, 'palette') + b'\x0a' + struct.pack('>i', len(palette))
    out += b''.join(_W_PALETTE[s] for s in palette.tolist())
    if len(palette) > 1:
        bits = max(4, (len(palette) - 1).bit_length())
        nlongs, data = pack_indices(local.ravel(), bits)
        out += _tag(12, 'data') + struct.pack('>i', nlongs) + data
    out += b'\x00' + BIOMES_PLAINS + b'\x00'
    return out


def encode_chunk(cx, cz, sections, data_version):
    out = b'\x0a' + _s('')
    out += _tag(3, 'DataVersion') + struct.pack('>i', data_version)
    out += _tag(3, 'xPos') + struct.pack('>i', cx)
    out += _tag(3, 'yPos') + struct.pack('>i', MC_SEC_Y_MIN)
    out += _tag(3, 'zPos') + struct.pack('>i', cz)
    out += _tag(8, 'Status') + _s('minecraft:full')
    out += _tag(4, 'LastUpdate') + struct.pack('>q', 0)
    out += _tag(4, 'InhabitedTime') + struct.pack('>q', 0)
    out += _tag(1, 'isLightOn') + b'\x00'  # let the game recompute lighting
    out += _tag(9, 'sections') + b'\x0a' + struct.pack('>i', len(sections))
    out += b''.join(sections[y] for y in sorted(sections))
    out += _tag(9, 'block_entities') + b'\x0a' + struct.pack('>i', 0)
    return out + b'\x00'


def write_region(path, chunks):
    """chunks: {(cx, cz): uncompressed NBT bytes}"""
    header = bytearray(8192)
    body   = bytearray()
    now    = int(time.time())
    for (cx, cz), nbt in chunks.items():
        comp   = zlib.compress(nbt, 6)
        blob   = struct.pack('>IB', len(comp) + 1, 2) + comp
        blob  += b'\x00' * (-len(blob) % 4096)
        offset = 2 + len(body) // 4096
        count  = len(blob) // 4096
        if count > 255:
            continue  # would need an external .mcc file; never happens for Voxy data
        i = ((cx & 31) + (cz & 31) * 32) * 4
        header[i:i + 4] = struct.pack('>I', (offset << 8) | count)
        header[4096 + i:4096 + i + 4] = struct.pack('>I', now)
        body += blob
    with open(path, 'wb') as f:
        f.write(header)
        f.write(body)


# ── Worker ───────────────────────────────────────────────────────────────────

_W_STATE_LUT = None   # numpy: voxy block id -> global state index (0 = air)
_W_PALETTE   = None   # list:  global state index -> encoded palette compound
_W_OUT       = None
_W_DATAVER   = None


def _init_worker(state_lut, palette, output_folder, data_version):
    global _W_STATE_LUT, _W_PALETTE, _W_OUT, _W_DATAVER
    _W_STATE_LUT = state_lut
    _W_PALETTE   = palette
    _W_OUT       = output_folder
    _W_DATAVER   = data_version


def decode_section(dctx, val_bytes):
    """
    SaveLoadSystem3.java::serialize binary layout:
      [0..7]                    key (long)
      [8..15]                   metadata: low16=lut_count, bits16-23=nonEmptyChildren
      [16..16+SECTION_VOLUME*2] block indices (uint16, each is a LUT index)
      [16+SECTION_VOLUME*2..]   LUT entries (int64, each is a full mapping id)

    Mapper.java::getBlockId:    (id >> 27) & 0xFFFFF
    WorldSection.getIndex:      ((y&M)<<10)|((z&M)<<5)|(x&M)  -> Y,Z,X layout

    Returns a (32, 32, 32) [y, z, x] array of global state indices, or None.
    """
    raw = dctx.decompress(val_bytes, max_output_size=700_000)
    lut_start = 16 + SECTION_VOLUME * 2
    if len(raw) < lut_start:
        return None
    lut_count = struct.unpack_from('<q', raw, 8)[0] & 0xFFFF
    if lut_count == 0 or len(raw) < lut_start + lut_count * 8:
        return None

    indices = np.frombuffer(raw, dtype='<u2', count=SECTION_VOLUME, offset=16)
    lut     = np.frombuffer(raw, dtype='<i8', count=lut_count, offset=lut_start)
    bids    = (lut >> 27) & 0xFFFFF
    lut_n   = len(_W_STATE_LUT)
    states  = np.zeros(lut_count + 1, dtype=np.int32)       # extra slot = invalid index -> air
    states[:lut_count] = np.where(bids < lut_n, _W_STATE_LUT[np.minimum(bids, lut_n - 1)], 0)
    return states[np.minimum(indices, lut_count)].reshape(32, 32, 32)


def build_region(job):
    (rx, rz), entries = job
    dctx    = zstd.ZstdDecompressor()
    chunks  = {}   # (cx, cz) -> {sec_y: encoded section}
    skipped = 0
    blocks  = 0

    for sx, sy, sz, val in entries:
        try:
            arr = decode_section(dctx, val)
        except Exception:
            arr = None
        if arr is None:
            skipped += 1
            continue
        for half in (0, 1):
            sec_y = sy * 2 + half
            if sec_y < MC_SEC_Y_MIN or sec_y > MC_SEC_Y_MAX:
                continue
            slab = arr[half * 16:half * 16 + 16]
            if not slab.any():
                continue
            for dz in (0, 1):
                for dx in (0, 1):
                    sub = slab[:, dz * 16:dz * 16 + 16, dx * 16:dx * 16 + 16]
                    nonair = int(np.count_nonzero(sub))
                    if nonair == 0:
                        continue
                    blocks += nonair
                    ck = (sx * 2 + dx, sz * 2 + dz)
                    chunks.setdefault(ck, {})[sec_y] = encode_section(sec_y, sub.ravel())

    if not chunks:
        return rx, rz, 0, 0, blocks, skipped

    nbt = {ck: encode_chunk(ck[0], ck[1], secs, _W_DATAVER) for ck, secs in chunks.items()}
    out_path = os.path.join(_W_OUT, f"r.{rx}.{rz}.mca")
    write_region(out_path, nbt)
    return rx, rz, len(chunks), os.path.getsize(out_path), blocks, skipped


# ── Main ─────────────────────────────────────────────────────────────────────

def print_coordinate_info(regions):
    """Print the in-game coordinates where the converted blocks will appear."""
    if not regions:
        return

    rx_vals = [rx for rx, rz in regions]
    rz_vals = [rz for rx, rz in regions]

    rx_min, rx_max = min(rx_vals), max(rx_vals)
    rz_min, rz_max = min(rz_vals), max(rz_vals)

    bx_min = rx_min * 512
    bx_max = rx_max * 512 + 511
    bz_min = rz_min * 512
    bz_max = rz_max * 512 + 511

    cx = (bx_min + bx_max) // 2
    cz = (bz_min + bz_max) // 2

    print()
    print("=" * 60)
    print("  COORDINATE INFO")
    print("=" * 60)
    print(f"  Block area X: {bx_min} to {bx_max}")
    print(f"  Block area Z: {bz_min} to {bz_max}")
    print()
    print("  Teleport to the center of the converted area:")
    print(f"    /tp @s {cx} 100 {cz}")
    print()
    print("  If you don't see blocks, try adjusting Y:")
    print(f"    /tp @s {cx} 64 {cz}")
    print(f"    /tp @s {cx} 200 {cz}")
    print("=" * 60)


def main():
    args = parse_args()
    banner()
    db_path, output_folder = ask_paths(args)
    os.makedirs(output_folder, exist_ok=True)
    t_start = time.time()

    print(f"\n[+] Opening RocksDB: {db_path}")
    raw_opts = Options(raw_mode=True)
    try:
        db = Rdict(
            db_path,
            options=raw_opts,
            column_families={
                "default":        raw_opts,
                "world_sections": raw_opts,
                "id_mappings":    raw_opts,
            },
            access_type=AccessType.read_only()
        )
        cf_sections = db.get_column_family("world_sections")
        cf_mappings = db.get_column_family("id_mappings")
        print("    [OK] Column families opened")
    except Exception as e:
        print(f"[-] RocksDB error: {e}")
        return

    # ── 1. Build block state table ────────────────────────────────────────────
    print("\n[+] Loading block ID table...")
    palette   = [encode_palette_entry("minecraft:air", {})]   # global state 0 = air
    state_ids = {}                                            # (name, props) -> global state
    voxy_to_state = {}
    for key_bytes, val_bytes in cf_mappings.items():
        if not isinstance(key_bytes, bytes) or len(key_bytes) != 4:
            continue
        key_int    = struct.unpack('>I', key_bytes)[0]
        entry_type = key_int >> 30
        entry_id   = key_int & ((1 << 30) - 1)
        if entry_type != 1:  # BLOCK_STATE_TYPE
            continue
        parsed = parse_block_state(val_bytes)
        if not parsed or parsed[0] in AIR_NAMES:
            continue
        name, props = parsed
        if args.no_properties:
            props = {}
        sk = (name, tuple(sorted(props.items())))
        if sk not in state_ids:
            state_ids[sk] = len(palette)
            palette.append(encode_palette_entry(name, props))
        voxy_to_state[entry_id] = state_ids[sk]

    print(f"    {len(voxy_to_state)} block states loaded")
    if not voxy_to_state:
        print("[-] Mapping table is empty, exiting.")
        db.close()
        return

    state_lut = np.zeros(max(voxy_to_state) + 1, dtype=np.int32)
    for vid, sid in voxy_to_state.items():
        state_lut[vid] = sid

    print("    Sample entries:")
    for (name, props), sid in list(state_ids.items())[:5]:
        p = ",".join(f"{k}={v}" for k, v in props)
        print(f"      {name}" + (f"[{p}]" if p else ""))

    # ── 2. Group LOD 0 sections by region (still compressed) ──────────────────
    print(f"\n[+] Scanning LOD {LOD_LEVEL} sections...")
    regions  = {}
    scanned  = 0
    for key_bytes, val_bytes in cf_sections.items():
        if not isinstance(key_bytes, bytes) or len(key_bytes) != 8:
            continue
        level, sx, sy, sz = decode_key(key_bytes)
        if level != LOD_LEVEL:
            if level > LOD_LEVEL:
                break  # keys are sorted big-endian with the level in the top bits
            continue
        if sy * 2 + 1 < MC_SEC_Y_MIN or sy * 2 > MC_SEC_Y_MAX:
            continue
        # A Voxy section is 32 blocks wide, so it always lies inside one 512-block region
        regions.setdefault((sx >> 4, sz >> 4), []).append((sx, sy, sz, val_bytes))
        scanned += 1
        if scanned % 20000 == 0:
            print(f"    {scanned:,} sections | {len(regions)} regions")

    try:
        db.close()
    except Exception:
        pass

    print(f"    {scanned:,} sections in {len(regions)} region(s)  ({time.time() - t_start:.1f}s)")

    # ── 3. Build and write MCA files in parallel ──────────────────────────────
    jobs_n = max(1, min(args.jobs, len(regions)))
    print(f"\n[+] Writing MCA files -> '{output_folder}'  ({jobs_n} worker processes)")
    success = 0
    block_count = 0
    skipped = 0
    written = {}

    # biggest regions first so workers stay busy until the end
    jobs = sorted(regions.items(), key=lambda kv: -len(kv[1]))
    init = (state_lut, palette, output_folder, args.data_version)
    if jobs_n == 1:
        _init_worker(*init)
        results = map(build_region, jobs)
        pool = None
    else:
        pool = Pool(jobs_n, initializer=_init_worker, initargs=init)
        results = pool.imap_unordered(build_region, jobs)

    for done, (rx, rz, n_chunks, size, blocks, skip) in enumerate(results, 1):
        block_count += blocks
        skipped += skip
        if n_chunks:
            success += 1
            written[(rx, rz)] = True
            print(f"    [{done}/{len(jobs)}] r.{rx}.{rz}.mca  ({n_chunks} chunks, {size // 1024} KB)")

    if pool:
        pool.close()
        pool.join()

    # ── 4. Final summary & coordinate info ───────────────────────────────────
    print(f"\n[+] DONE in {time.time() - t_start:.1f}s! {success} region file(s) written")
    print(f"    {block_count:,} blocks, {skipped} sections skipped (corrupt/empty)")
    print(f"    Output folder: '{os.path.abspath(output_folder)}'")
    print()
    print("[!] To load in Minecraft:")
    print("    Go to: Saves > YourWorldName > region")
    print(f"    Replace the files there with the ones from '{output_folder}'")

    print_coordinate_info(written)


if __name__ == "__main__":
    freeze_support()
    main()
