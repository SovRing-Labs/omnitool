# Shard format 1.0 (omni-ring / vsa-core slots)

**Licence of this specification:** Creative Commons Attribution 4.0 International (CC BY 4.0). Anyone may implement it.

## Slot
A slot is **2,560 bytes**. The same bytes have two readings ("faces"):

### Ternary face (10,240 dimensions, values −1 / 0 / +1)
* Bytes 0–1279: **sign plane**, 160 unsigned 64-bit little-endian words. Bytes 1280–2559: **active plane**, 160 words.
* Dimension `i` lives in word `i // 64`, bit `i % 64` (bit 0 = least significant) of each plane.
* `active = 0` → value 0. `active = 1, sign = 0` → +1. `active = 1, sign = 1` → −1. (`active = 0, sign = 1` is invalid.)

### Phase face (5,120 dimensions, 16 levels)
* Phase index `p ∈ {0,…,15}` represents angle `2π·p/16`. Dimension `2k` is the **low** nibble and `2k+1` the **high** nibble
  of byte `k`.
* Ternary → phase: each ternary pair `(t[2k], t[2k+1])` maps to one phase:
  `(1,1)→2, (1,0)→0, (1,−1)→14, (0,1)→4, (0,0)→0, (0,−1)→12, (−1,1)→6, (−1,0)→8, (−1,−1)→10`.

## Rings (omni-ring coordinates)
* Information rings, in order: moduli **3, 5, 7, 13** (coordinates `x ∈ [0, 1365)`); check ring: **17**.
* Codebook seed convention (reference implementation): for seed `s`, NumPy `default_rng(s)` (PCG64) draws, for each
  modulus `m` in the order 3, 5, 7, 13, 17, one array `integers(0, m, 5120)`; ring `m`'s angle per dimension is
  `j · 2π/m`. Position `k` of ring `m` is `exp(i · k · angle)`. A mark `x` is the elementwise product over rings of position
  `x mod m`. Readers must use the same seed as the writer; record it alongside the shard.
* A decoded coordinate is **legal** iff the CRT reconstruction of all five residues is `< 1365`.

## Sidecar metadata (recommended, JSON)
`{"format": "omniring-shard/1.0", "face": "phase|ternary", "codebook_seed": s, "dimension": 5120, "rings": [3,5,7,13],
"check": 17, "licence": "CC-BY-4.0|CC0-1.0|…", "created": "ISO-8601", "creator": "…"}`
