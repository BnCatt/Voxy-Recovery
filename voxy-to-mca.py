"""
Voxy RocksDB -> Minecraft .mca Converter
-----------------------------------------
Converts Voxy mod's RocksDB storage into standard Minecraft region (.mca) files.

References (voxy-dev source):
  - WorldEngine.java     : key encoding/decoding
  - SaveLoadSystem3.java : binary section layout
  - Mapper.java          : block ID / biome ID encoding

Known limitations:
  - Block state properties (facing direction, slab type, waterlogged, etc.) are NOT preserved
  - Thin/special blocks (slabs, signs, fences, banners, fluids) may be missing or incorrect
  - Voxy LOD data is intended for visual rendering, not full survival restoration
"""

import struct, os, io, gzip, sys
import zstandard as zstd
from rocksdict import Rdict, Options, AccessType

try:
    import anvil
except ImportError:
    print("[-] Missing library: pip install anvil-parser2")
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
SECTION_VOLUME = 32 * 32 * 32   # 32768 blocks per section
LOD_LEVEL      = 0               # 0 = highest detail (full resolution)
MC_Y_MIN       = -64
MC_Y_MAX       = 319
# ─────────────────────────────────────────────────────────────────────────────


def banner():
    print("=" * 60)
    print("  Voxy RocksDB -> Minecraft MCA Converter")
    print("  LOD level: 0 (full resolution)")
    print()
    print("  WARNING: Block state info (facing, slab type, etc.)")
    print("  is not preserved. Slabs, signs, fences, banners")
    print("  and fluids may be missing or incorrect.")
    print("=" * 60)
    print()


def ask_paths():
    """Ask the user for the Voxy storage path and output folder."""
    print("[?] Enter the path to your Voxy storage folder:")
    print("    (e.g. C:\\Users\\Name\\AppData\\Roaming\\.minecraft\\voxy\\...\\storage)")
    db_path = input("    > ").strip().strip('"').strip("'")

    if not os.path.isdir(db_path):
        print(f"[-] Directory not found: {db_path}")
        sys.exit(1)

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


def decode_section(data: bytes):
    """
    SaveLoadSystem3.java::serialize binary layout:
      [0..7]                    key (long)
      [8..15]                   metadata: low16=lut_count, bits16-23=nonEmptyChildren
      [16..16+SECTION_VOLUME*2] block indices (uint16, each is a LUT index)
      [16+SECTION_VOLUME*2..]   LUT entries (int64, each is a full mapping id)

    Mapper.java::getBlockId:    (id >> 27) & 0xFFFFF
    WorldSection.getIndex:      ((y&M)<<10)|((z&M)<<5)|(x&M)  -> Y,Z,X layout
    """
    if len(data) < 16 + SECTION_VOLUME * 2:
        return None

    metadata  = struct.unpack_from('<q', data, 8)[0]
    lut_count = metadata & 0xFFFF
    if lut_count == 0:
        return []

    indices   = struct.unpack_from(f'<{SECTION_VOLUME}H', data, 16)
    lut_start = 16 + SECTION_VOLUME * 2

    if len(data) < lut_start + lut_count * 8:
        return None

    lut    = struct.unpack_from(f'<{lut_count}q', data, lut_start)
    blocks = []

    for i, idx in enumerate(indices):
        if idx >= lut_count:
            continue
        raw = lut[idx]
        if raw == 0:
            continue
        block_id = (raw >> 27) & 0xFFFFF
        if block_id == 0:
            continue
        lx = i & 0x1F
        lz = (i >> 5)  & 0x1F
        ly = (i >> 10) & 0x1F
        blocks.append((lx, ly, lz, block_id))

    return blocks


def parse_block_name(raw_nbt: bytes):
    """Read the 'Name' tag from a gzip-compressed NBT blob -> e.g. 'minecraft:stone'"""
    try:
        with gzip.open(io.BytesIO(raw_nbt), 'rb') as f:
            data = f.read()
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
    except Exception:
        pass
    return None


def print_coordinate_info(regions: dict):
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
    banner()
    db_path, output_folder = ask_paths()
    os.makedirs(output_folder, exist_ok=True)

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

    dctx = zstd.ZstdDecompressor()

    # ── 1. Build block ID table ───────────────────────────────────────────────
    print("\n[+] Loading block ID table...")
    block_id_to_name = {0: "minecraft:air"}
    mapping_count = 0

    for key_bytes, val_bytes in cf_mappings.items():
        if not isinstance(key_bytes, bytes) or len(key_bytes) != 4:
            continue
        key_int    = struct.unpack('>I', key_bytes)[0]
        entry_type = key_int >> 30
        entry_id   = key_int & ((1 << 30) - 1)
        if entry_type == 1:  # BLOCK_STATE_TYPE
            name = parse_block_name(val_bytes)
            if name:
                block_id_to_name[entry_id] = name
                mapping_count += 1

    print(f"    {mapping_count} block types loaded")
    if mapping_count == 0:
        print("[-] Mapping table is empty, exiting.")
        db.close()
        return

    print("    Sample entries:")
    for bid, bname in list(block_id_to_name.items())[1:6]:
        print(f"      ID {bid:5d} -> {bname}")

    # ── 2. Scan sections ──────────────────────────────────────────────────────
    print(f"\n[+] Scanning LOD {LOD_LEVEL} sections...")
    regions       = {}
    section_count = 0
    block_count   = 0
    skipped       = 0

    for key_bytes, val_bytes in cf_sections.items():
        if not isinstance(key_bytes, bytes) or len(key_bytes) != 8:
            continue
        level, sx, sy, sz = decode_key(key_bytes)
        if level != LOD_LEVEL:
            continue

        try:
            raw = dctx.decompress(val_bytes, max_output_size=700_000)
        except Exception:
            skipped += 1
            continue

        blocks = decode_section(raw)
        if blocks is None:
            skipped += 1
            continue

        section_count += 1

        for (lx, ly, lz, bid) in blocks:
            gx = (sx << 5) + lx
            gy = (sy << 5) + ly
            gz = (sz << 5) + lz

            if gy < MC_Y_MIN or gy > MC_Y_MAX:
                continue

            name = block_id_to_name.get(bid)
            if not name or 'air' in name:
                continue

            cx, cz = gx >> 4, gz >> 4
            rx, rz = cx >> 5, cz >> 5

            if (rx, rz) not in regions:
                regions[(rx, rz)] = {}
                print(f"    -> New region: r.{rx}.{rz}.mca")

            ck = (cx, cz)
            if ck not in regions[(rx, rz)]:
                regions[(rx, rz)][ck] = {}
            regions[(rx, rz)][ck][(gx, gy, gz)] = name
            block_count += 1

        if section_count % 200 == 0:
            print(f"    {section_count} sections | {block_count:,} blocks | {skipped} skipped")

    print(f"\n[+] Scan complete:")
    print(f"    {section_count} sections processed")
    print(f"    {block_count:,} blocks found")
    print(f"    {skipped} sections skipped (corrupt/empty)")
    print(f"    {len(regions)} region file(s) to write")

    # ── 3. Write MCA files ────────────────────────────────────────────────────
    print(f"\n[+] Writing MCA files -> '{output_folder}'")
    success = 0
    fail    = 0

    for (rx, rz), chunks in regions.items():
        region = anvil.EmptyRegion(rx, rz)
        for (cx, cz), block_dict in chunks.items():
            chunk = anvil.EmptyChunk(cx, cz)
            for (gx, gy, gz), full_name in block_dict.items():
                try:
                    ns, name = full_name.split(':', 1)
                    name  = name.split('[')[0]
                    block = anvil.Block(ns, name)
                    chunk.set_block(block, gx & 0xF, gy, gz & 0xF)
                except Exception:
                    pass
            region.add_chunk(chunk)

        out_path = os.path.join(output_folder, f"r.{rx}.{rz}.mca")
        try:
            region.save(out_path)
            size_kb = os.path.getsize(out_path) // 1024
            print(f"    OK  r.{rx}.{rz}.mca  ({len(chunks)} chunks, {size_kb} KB)")
            success += 1
        except Exception as e:
            print(f"    ERR r.{rx}.{rz}.mca : {e}")
            fail += 1

    try:
        db.close()
    except Exception:
        pass

    # ── 4. Final summary & coordinate info ───────────────────────────────────
    print(f"\n[+] DONE! {success} succeeded, {fail} failed")
    print(f"    Output folder: '{os.path.abspath(output_folder)}'")
    print()
    print("[!] To load in Minecraft:")
    print("    Go to: Saves > YourWorldName > region")
    print(f"    Replace the files there with the ones from '{output_folder}'")
    print("    (Only copy non-empty .mca files, skip 0 KB ones)")

    print_coordinate_info(regions)


if __name__ == "__main__":
    main()
