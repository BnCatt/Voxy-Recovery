# Voxy-Recovery
If you accidently removed/overwrite your world and still have the chunks loaded from using the Voxy mod you can use this converter to recover them to .mca files. I did NOT put much effort in to this and this is probably not the most effective way to do it but its still pretty good. Detailed info down below.


# Voxy-Recovery-(Voxy-to-mca)

Convert [Voxy](https://modrinth.com/mod/voxy) mod's RocksDB LOD storage into standard Minecraft `.mca` region files.

> Voxy caches chunk data for its LOD rendering system. This tool extracts that cached block data and converts it into regular Minecraft region files you can load in any world.

---

## ⚠️ Limitations

Voxy stores blocks for **visual rendering only**, not full world state. This means:

- **Block states are not preserved** — facing direction, slab type, waterlogged, powered, etc. are all lost
- **Slabs, signs, fences, banners, fluids** may be missing or placed as full blocks
- **Entities** (chests, spawners, etc.) are not stored by Voxy at all
- This is best used to recover the **rough shape and location** of structures, not a full survival restore

---

## Requirements

```
pip install rocksdict zstandard anvil-parser2
```

Python 3.10+ recommended.

---

## Usage

```
python voxy_to_mca.py
```

The script will ask you for:
1. **Path to your Voxy storage folder**
   - Usually found at: `.minecraft/voxy/<world_id>/storage`
   - Or at: `.minecraft/saves/<world>/<world_id>/storage` depending on your setup
2. **Output folder** for the `.mca` files (default: `region`)

At the end it will print the **exact `/tp` command** to teleport to where your blocks are.

---

## Loading into Minecraft

1. Run the script and note the output folder
2. Go to your Minecraft world folder: `saves/YourWorldName/region/`
3. Copy the **non-empty** `.mca` files from the output folder into the world's `region` folder
4. Launch Minecraft and use the `/tp` command printed by the script

> Only copy files larger than 0 KB. Empty region files can be ignored.

---

## How it works

Voxy stores chunk data in a [RocksDB](https://rocksdb.org/) database with two column families:

| Column Family | Contents |
|---|---|
| `world_sections` | 32×32×32 block sections, zstd-compressed |
| `id_mappings` | Block ID → NBT block state mapping |

### Key format (`WorldEngine.java`)
```
bits 63-60 : LOD level (0 = full res)
bits 59-52 : section Y (signed 8-bit)
bits 51-28 : section Z (signed 24-bit)
bits 27-4  : section X (signed 24-bit)
```

### Section binary layout (`SaveLoadSystem3.java`)
```
[0..7]                     key (int64)
[8..15]                    metadata (low 16 bits = LUT count)
[16..16+32768*2]           block indices (uint16 each)
[16+32768*2..]             LUT entries (int64 each, Mapper encoding)
```

### Block ID encoding (`Mapper.java`)
```
bits 63-56 : light level
bits 55-47 : biome ID
bits 46-27 : block state ID  <-- what we extract
bits 26-0  : unused
```

---

## Technical notes

- Keys are stored **big-endian** in RocksDB
- Section data is **little-endian**
- Python's unbounded integers mean Java-style signed bit shifts (`<<`/`>>`) don't work directly — sign extension must be done manually
- The original bug that caused coordinates like `r.17592181850111.1048575.mca` was due to this exact issue in `decode_key()`

---

## License

MIT
