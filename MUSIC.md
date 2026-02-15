# Music / Audio notes (Legend of Kyrandia 1)

This repo contains multiple *alternative* music banks for different audio devices. The executable selects one of three formats by file extension:

- `*.XMI` — XMIDI music bank (used by the MIDI/MT-32/LAPC-1 style driver)
- `*.ADL` — AdLib-style bank
- `*.SND` — non-AdLib driver bank (used by the `PCSOUND.DRV` path; despite the name, this is not a standard `VOC`/`WAV` container)

You can see the extension strings and the filename template in the data segment:
- `MAIN.EXE` runtime analysis (data segment strings: `"ADL"`, `"SND"`, `"XMI"`, and `"%s.%s"`)

## Where the executable chooses ADL vs XMI vs SND

The loader that builds `<basename>.<ext>` and dispatches into the currently loaded audio driver is `sub_2960D`:
- `MAIN.EXE` runtime analysis (`sub_2960D`)

Key behavior (high-level):
- ADL/SND path: for certain `word_40FAA` values, it selects either `"ADL"` (if `word_40FAA == 1`) or `"SND"` (otherwise).
  - `MAIN.EXE` runtime analysis (`sub_2960D`, ADL/SND branch)
- XMI path: otherwise, it selects `"XMI"`.
  - `MAIN.EXE` runtime analysis (`sub_2960D`, XMI branch)

The caller that picks the *basename* (e.g. `KYRA1A`, `INTRO`, `KYRAMISC`, …) and then calls `sub_2960D` is `sub_20202`:
- `MAIN.EXE` runtime analysis (`sub_20202`)

## Which *driver module* is selected (more concrete)

There are two distinct steps:

1) `sub_205DF` reads setup state (from `WWSETUP.CFG`) into a few `byte_4428x` fields and maps them to audio “mode” globals like `word_40EA5` / `word_40EA7`.
  - `MAIN.EXE` runtime analysis (`sub_205DF`)
2) `sub_2918F` turns those mode values into the actual bank-selection globals `word_40FAA` / `word_40FA8` using small tables in the data segment.
  - `MAIN.EXE` runtime analysis (`sub_2918F`)

From there, the code loads one (or more) external driver blobs and stores an entrypoint in `dword_40FAE`.

### MIDI / XMIDI path (`*.XMI`)

When the MIDI path is enabled (`word_40FA8 == 1` after `sub_2918F`), the game loads `MT32MPU.ADV` via `sub_29FEE`.
- Loader: `MAIN.EXE` runtime analysis (`sub_29FEE`)
- Driver filename string: `MAIN.EXE` runtime analysis (driver filename table)

This is a Miles Design driver blob (its header contains `"Miles Design, Inc."`), which matches the fact that `*.XMI` here is true XMIDI.

### AdLib/FM path (`*.ADL`)

When `word_40FAA == 1`, `sub_2960D` selects the `"ADL"` extension and the music driver is loaded by `sub_2A2AE` using the filename `ALFX.DRV`.
- Format choice (`ADL` vs `SND`): `MAIN.EXE` runtime analysis (`sub_2960D`)
- Driver loader + filename switch: `MAIN.EXE` runtime analysis (`sub_2A2AE`)
- Driver filename string: `MAIN.EXE` runtime analysis (driver filename table)

### “SND bank” path (`*.SND`)

When `word_40FAA == 2` or `== 3`, `sub_2960D` still goes down the ADL/SND bank path but picks the `"SND"` extension.
- `MAIN.EXE` runtime analysis (`sub_2960D`)

In these modes, `sub_2A2AE` loads `PCSOUND.DRV` (and if `word_40FAA == 3` there is an extra initialization call into the driver).
- `MAIN.EXE` runtime analysis (`sub_2A2AE`)
- Driver filename string: `MAIN.EXE` runtime analysis (driver filename table)

## Observed bank basenames

These basenames exist as `ADL/XMI/SND` triplets in `original/`:

- `INTRO.(ADL|XMI|SND)`
- `KYRA1A.(ADL|XMI|SND)`
- `KYRA1B.(ADL|XMI|SND)`
- `KYRA2A.(ADL|XMI|SND)`
- `KYRA3A.(ADL|XMI|SND)`
- `KYRA4A.(ADL|XMI|SND)`
- `KYRA4B.(ADL|XMI|SND)`
- `KYRA5A.(ADL|XMI|SND)`
- `KYRA5B.(ADL|XMI|SND)`
- `KYRAMISC.(ADL|XMI|SND)`

## File format signatures (quick)

The EXE mostly **does not parse these formats itself**. It chooses a basename + extension, builds a filename, then calls into the currently loaded audio driver (via `sub_2960D`).

### `*.XMI` (XMIDI)

These are standard IFF-style `FORM` containers with XMIDI chunks.

Confirmed properties (from `original/INTRO.XMI` / `original/KYRAMISC.XMI`):

- File begins with ASCII `FORM`.
- The 32-bit length in the header is big-endian (IFF style).
- The top-level structure includes an `XDIR` section and one or more `XMID` forms.
- Both `TIMB` and `EVNT` chunks occur in the embedded XMIDI data.

Practical structure (standard XMIDI / Miles tooling conventions):

- The file is an IFF container: a sequence of chunks `(<id:4><len:u32_be><payload:len>)`.
  - Chunks are padded to an even boundary (IFF rule).
- In many XMIDI files, `XDIR` is a directory/header region:
  - `INFO` typically describes how many sequences exist and/or their offsets.
  - A `CAT ` chunk often groups multiple embedded `FORM XMID` sequences.
- Each `FORM XMID` commonly contains:
  - `TIMB`: timbre/patch data used by the driver.
  - `EVNT`: the event stream (delta-times + MIDI-like events in Miles’ encoding).

Concrete examples:

- `original/INTRO.XMI` is 28720 bytes and includes readable metadata strings (e.g. `Westwood Studios`) among binary data.
- `original/KYRAMISC.XMI` is 37068 bytes and shows repeated `FORM`/`XMID`/`EVNT` patterns.

Driver/backend notes (from analysis of the MT-32/MPU module):

- The MT-32/MPU backend stores an MPU base I/O port in `word_43F` and uses `word_441 = word_43F + 1` as the status port.
- The byte-send routine busy-waits on status bits (`0x40` / `0x80`) and then outputs the byte to the base port.
- The init/command routine writes `0xFF` and waits for an `0xFE` acknowledge byte (classic MPU-401 style handshake).

Implication:

- This module is a low-level MIDI output backend. It does not look like the place where `FORM`/`XMID` parsing happens; XMIDI parsing is likely handled by the higher-level Miles/XMIDI logic that feeds MIDI bytes into this backend.

#### MIDI byte stream “internal format” (what the backend transmits)

Once XMIDI has been parsed and scheduled, the output that ultimately goes to an MPU-401 / MT-32 interface is just a **standard MIDI byte stream**: a sequence of 8-bit bytes.

Byte classes:

- **Status bytes**: `0x80..0xFF` (high bit set). Start a new MIDI message and define its type/channel.
- **Data bytes**: `0x00..0x7F` (high bit clear). Parameters to the current status.

Channel Voice messages (status high nibble = type, low nibble = channel `0..15`):

- `0x8n` Note Off: 2 data bytes (`note`, `velocity`)
- `0x9n` Note On: 2 data bytes (`note`, `velocity`; velocity 0 is commonly treated as Note Off)
- `0xAn` Polyphonic Key Pressure: 2 data bytes
- `0xBn` Control Change: 2 data bytes (`controller`, `value`)
- `0xCn` Program Change: 1 data byte (`program`)
- `0xDn` Channel Pressure: 1 data byte
- `0xEn` Pitch Bend: 2 data bytes (`lsb`, `msb`), forming a 14-bit value `lsb | (msb<<7)` with center at 8192.

Running status:

- If multiple consecutive messages use the same status (common in MIDI streams), the status byte may be omitted; subsequent messages provide only the data bytes and implicitly reuse the last status.

System messages:

- **SysEx**: `0xF0` … data bytes (`0x00..0x7F`) … `0xF7`. MT-32-specific programming (timbres/patches) is typically done via SysEx.
- Real-time bytes (`0xF8..0xFF` excluding `0xF0`/`0xF7`) are 1-byte messages and can appear “in between” other messages.

Important distinction:

- The `EVNT` chunk inside `*.XMI` is a **MILES XMIDI event stream** (delta-times + compact/event-packed encoding). It is not byte-for-byte the same as the final raw MIDI byte stream.
- The MPU backend code shown above only concerns itself with outputting already-decoded MIDI bytes; the XMIDI → MIDI conversion happens at a higher layer.

Temperament note:

- This MIDI backend does not define pitch temperament. Temperament/tuning is determined by the external MIDI device/synth (e.g. MT-32), and can in principle be altered via SysEx depending on the target device.

### `*.ADL` (AdLib driver bank)

These are *not* IFF/RIFF containers. For Kyrandia 1 they are a driver-defined **bank** format consumed by `ALFX.DRV`.

Below is a practical byte-level specification for the parts that the driver actually dereferences.

#### ADL “bank view” base

For all shipped `original/*.ADL` in this repo, the driver-relevant structures begin at file offset `0x78`.

Define:

- `base = file + 0x78`

Everything in the tables below uses **little-endian 16-bit offsets relative to `base`**, i.e. a pointer is computed as `base + u16`.

#### ADL layout (from `base`)

1) **Song/stream directory** at `base + 0x0000`:

- `u16_le songOfs[250]` (size `0x1f4` bytes)
- Entry value `0x0000` or `0xffff` means “unused”.

The driver indexes this table with an 8-bit id and does:

- `ptr = base + songOfs[id]`

Evidence: ALFX.DRV analysis (`lds si, cs:dword_39E` → `add si, [bx+si]`).

2) **Instrument/patch directory** at `base + 0x01F4`:

- `u16_le instOfs[250]` (size `0x1F4` bytes)
- Same sentinel rules (`0x0000` / `0xFFFF` = unused).

The driver indexes this second table and does:

- `ptr = base + instOfs[id]`

Evidence: ALFX.DRV analysis (`add si, [bx+si+1F4h]`).

3) **Data region** begins at `base + 0x03E8`.

This follows mechanically from the two 0x1F4-byte tables: `0x1F4 + 0x1F4 = 0x3E8`, and the first `songOfs[0]` observed in shipped banks is `0x03E8`.

#### ADL song record (stream entry)

At `base + songOfs[id]` the record begins with:

- `u8 channelId` — expected range `0..9` (driver iterates channels 9..0).
- `u8 priority` — compared against the current channel priority to decide whether to steal/replace.
- followed by a **command stream**.

Evidence that the record starts with these two bytes: ALFX.DRV analysis (`lodsb` → channel selection, then `lodsb` → priority compare, then stores the remaining pointer as the stream position).

#### ADL command stream encoding

The stream is a sequence of **little-endian 16-bit words** read via `lodsw`.

- If `(lowByte & 0x80) == 0`: the word is treated as a **note event**.
- If `(lowByte & 0x80) != 0`: the low byte selects a **command** and the command handler may consume additional immediate bytes/words.

Evidence: ALFX.DRV analysis (`lodsw` then `js` branches to the command dispatcher).

#### ADL pitch mapping / temperament

Pitch is encoded as “octave/block + semitone” and converted to OPL2 frequency using a fixed 12-entry **F-number** table (one entry per semitone), then the octave/block is applied via the OPL block bits.

- The semitone table is the 12 little-endian words starting at the driver’s `cs:[...+6A5h]` base and is visible in the driver blob data bytes (ALFX.DRV analysis).
- The playback code indexes it with `note % 12` and uses it to form the `A0`/`B0` frequency registers (ALFX.DRV analysis).

This table matches **12-tone equal temperament (12‑TET)** closely (small per-step rounding error from integer F-numbers; within a few cents for the shipped driver).

##### ADL reference pitch table (FNUM → Hz)

The semitone lookup table (12 entries) is stored in the driver blob itself:

- `ALFX.DRV` semitone F-number table is at file offset `0x6A5`.

To convert an OPL2 pitch (`block`, `fnum`) into an approximate frequency in Hz, you need the OPL2 clock. Under the common YM3812/OPL2 clock assumption:

- `f_opl = 14_318_180 / 288 ≈ 49_715.903 Hz`
- Reference conversion:

  `f_hz = fnum * 2^block * (f_opl / 2^20)`

Using `block = 4` as a reference octave gives these semitone frequencies.

For comparison, the 12‑TET column below is anchored so semitone idx `11` corresponds to **A4 = 440 Hz**:

Interpreting the difference:

- The per-row `err (cents)` values here include any **global tuning offset** from assumptions like the exact OPL2 clock and the choice of what this table’s “A” should be.
- In practice the rows are all offset by roughly the same amount (a few cents). That’s consistent with “12‑TET intervals, but tuned slightly sharp/flat” rather than a different temperament.
  - Example: semitone idx `11` comes out as 441.508 Hz in the reference conversion above, i.e. about +5.9 cents vs 440 Hz. If you instead treated that pitch as “A4”, you’d effectively be using **A4 ≈ 441.5 Hz** (or, equivalently, scaling the assumed chip clock down by about 0.34%).

| semitone idx | fnum (hex) | fnum (dec) | ref freq (Hz) | 12-TET (Hz) | err (cents) | 12-TET fnum (ideal, frac) |
|---:|:---------:|----------:|-------------:|-----------:|-----------:|------------------------:|
| 0 | `0x0134` | 308 | 233.650 | 233.082 | +4.216 | 307.251 |
| 1 | `0x0147` | 327 | 248.064 | 246.942 | +7.848 | 325.521 |
| 2 | `0x015A` | 346 | 262.477 | 261.626 | +5.626 | 344.877 |
| 3 | `0x016F` | 367 | 278.408 | 277.183 | +7.636 | 365.385 |
| 4 | `0x0184` | 388 | 294.339 | 293.665 | +3.967 | 387.112 |
| 5 | `0x019C` | 412 | 312.545 | 311.127 | +7.873 | 410.131 |
| 6 | `0x01B4` | 436 | 330.752 | 329.628 | +5.893 | 434.518 |
| 7 | `0x01CE` | 462 | 350.475 | 349.228 | +6.171 | 460.356 |
| 8 | `0x01E9` | 489 | 370.958 | 369.994 | +4.501 | 487.730 |
| 9 | `0x0207` | 519 | 393.716 | 391.995 | +7.581 | 516.732 |
| 10 | `0x0225` | 549 | 416.474 | 415.305 | +4.867 | 547.459 |
| 11 | `0x0246` | 582 | 441.508 | 440.000 | +5.922 | 580.012 |

Note: this is a “reference Hz” conversion; exact tuning depends on the actual chip clock (or emulator settings) and any per-channel transposition the driver applies.

##### Note event word

When `(lowByte & 0x80) == 0`:

- `lowByte` encodes pitch as two nibbles:
  - high nibble: octave/block (transposed by per-channel state)
  - low nibble: note within octave (normalized modulo 12)
- `highByte` is used as the channel “delay/ticks” value (the driver stores it into its countdown field).

Evidence:

- Pitch decode uses `lowByte` split into `0xF0` / `0x0F` and normalizes around `0x0C` semitones (ALFX.DRV analysis).
- `highByte` is used as the tick/countdown source (ALFX.DRV analysis).

##### Command word

When `(lowByte & 0x80) != 0`:

- `cmdId = lowByte & 0x7F` (clamped by the driver to `0..0x4A`)
- dispatch is `off_18ED[cmdId]`
- the operand format depends on the specific handler (`lodsb`, `lodsw`, etc.).

The command table is visible in the driver blob (ALFX.DRV analysis).

#### ADL instrument/patch record

An instrument referenced by `instOfs[id]` is exactly **11 bytes**, interpreted as values to write into OPL2 registers:

1) `reg 0x20 + op` (operator 1)  
2) `reg 0x23 + op` (operator 2)  
3) `reg 0xC0 + ch` (feedback/connection)  
4) `reg 0xE0 + op` (waveform 1)  
5) `reg 0xE3 + op` (waveform 2)  
6) `reg 0x40 + op` (KSL/level 1)  
7) `reg 0x43 + op` (KSL/level 2)  
8) `reg 0x60 + op` (attack/decay 1)  
9) `reg 0x63 + op` (attack/decay 2)  
10) `reg 0x80 + op` (sustain/release 1)  
11) `reg 0x83 + op` (sustain/release 2)

Evidence: ALFX.DRV analysis (`sub_D19` has 11 `lodsb` and writes exactly these registers).

Notes:

- The driver may apply per-channel volume scaling before writing the `0x40` / `0x43` values (that’s processing behavior, not part of the on-disk patch bytes).
- The meaning of `op`/`ch` selection is driver state (which channel is being programmed), but the **byte order and register mapping** above is fixed by the format.

### `*.SND` (Sound Blaster driver bank)

These are also *not* standard `VOC`/`WAV` containers. In Kyrandia 1 they are a compact bank format consumed by `PCSOUND.DRV`.

#### SND “bank view” base

The driver indexes a u16 offset table (directory) from a caller-provided far pointer `dword_1BB`.

For the shipped `original/*.SND` banks here, the directory consistently begins at file offset `0x78`.

Define:

- `base = file + 0x78`

All offsets in the directory are **little-endian 16-bit offsets relative to `base`**, i.e. `ptr = base + u16`.

Evidence for the directory semantics: PCSOUND.DRV analysis (`lds si, cs:dword_1BB` then `add si, [bx+si]`).

#### SND layout (from `base`)

1) **Directory** at `base + 0x0000`:

- `u16_le entryOfs[64]` (128 bytes)
- Each entry points at a byte-coded stream.

2) **Data region** begins at `base + 0x03E8` in shipped banks (the first directory entry is typically `0x03E8`).

#### SND stream encoding

Each stream is a sequence of bytes. The driver is essentially a tiny bytecode interpreter.

##### Note/rest opcode

- `0x00..0x5F`: note index (looked up in a driver-internal frequency/divisor table)
- `0x60..0x7F`: treated as rest (converted to `0x00` by the driver)

After the note/rest byte, the next byte is always a **duration**:

- `u8 dur` → `ticks = (dur + 1) * tickMul`

where `tickMul` is a per-stream multiplier.

Evidence: PCSOUND.DRV analysis (note byte → optional tone output, then reads `dur` and stores `(dur+1)*tickMul`).

#### SND pitch mapping / temperament

The `PCSOUND.DRV` stream uses **note indices**, not explicit frequencies, for the common `0x00..0x5F` path:

- For the PC speaker (PIT) path, `noteIndex` is turned into a PIT channel-2 divisor via a u16 lookup table accessed as `cs:[bx+6]` (PCSOUND.DRV analysis).
- For the alternate DMA-based output path, a second u16 lookup table is used (`cs:[bx+0C6h]`) (PCSOUND.DRV analysis).
- The table data itself is stored inline near the start of the driver (u16 pairs) (PCSOUND.DRV analysis).

##### SND reference pitch table (PIT divisor → Hz)

For the PC speaker path, the driver converts `noteIndex` into a PIT channel-2 divisor from the first u16 table and the resulting square-wave frequency is:

`f_hz = 1193180 / divisor`

Extracted from `PCSOUND.DRV` at file offset `0x0006` (96 entries, note bytes `0x00..0x5F`).

For comparison, the 12‑TET column below is anchored so note index `56` (byte `0x38`, closest to 440 Hz in this table) corresponds to **A4 = 440 Hz**:

| idx | note byte | divisor (u16) | freq (Hz) | 12-TET (Hz) | err (cents) |
|---:|:---------:|-------------:|----------:|-----------:|-----------:|
| 0 | `0x00` | 0 |  |  |  |
| 1 | `0x01` | 65023 | 18.350 | 18.354 | -0.370 |
| 2 | `0x02` | 61346 | 19.450 | 19.445 | +0.407 |
| 3 | `0x03` | 57921 | 20.600 | 20.602 | -0.134 |
| 4 | `0x04` | 54658 | 21.830 | 21.827 | +0.251 |
| 5 | `0x05` | 51608 | 23.120 | 23.125 | -0.344 |
| 6 | `0x06` | 48701 | 24.500 | 24.500 | +0.028 |
| 7 | `0x07` | 45962 | 25.960 | 25.957 | +0.240 |
| 8 | `0x08` | 43388 | 27.500 | 27.500 | +0.015 |
| 9 | `0x09` | 40946 | 29.140 | 29.135 | +0.303 |
| 10 | `0x0A` | 38652 | 30.870 | 30.868 | +0.118 |
| 11 | `0x0B` | 36489 | 32.700 | 32.703 | -0.184 |
| 12 | `0x0C` | 34435 | 34.650 | 34.648 | +0.119 |
| 13 | `0x0D` | 32503 | 36.710 | 36.708 | +0.082 |
| 14 | `0x0E` | 30681 | 38.890 | 38.891 | -0.045 |
| 15 | `0x0F` | 28961 | 41.200 | 41.203 | -0.164 |
| 16 | `0x10` | 27335 | 43.650 | 43.654 | -0.129 |
| 17 | `0x11` | 25798 | 46.251 | 46.249 | +0.059 |
| 18 | `0x12` | 24351 | 48.999 | 48.999 | -0.007 |
| 19 | `0x13` | 22986 | 51.909 | 51.913 | -0.137 |
| 20 | `0x14` | 21694 | 55.000 | 55.000 | +0.015 |
| 21 | `0x15` | 20477 | 58.269 | 58.270 | -0.035 |
| 22 | `0x16` | 19326 | 61.740 | 61.735 | +0.118 |
| 23 | `0x17` | 18242 | 65.408 | 65.406 | +0.053 |
| 24 | `0x18` | 17218 | 69.298 | 69.296 | +0.069 |
| 25 | `0x19` | 16251 | 73.422 | 73.416 | +0.136 |
| 26 | `0x1A` | 15323 | 77.869 | 77.782 | +1.931 |
| 27 | `0x1B` | 14479 | 82.408 | 82.407 | +0.015 |
| 28 | `0x1C` | 13666 | 87.310 | 87.307 | +0.061 |
| 29 | `0x1D` | 12899 | 92.502 | 92.499 | +0.059 |
| 30 | `0x1E` | 12175 | 98.002 | 97.999 | +0.064 |
| 31 | `0x1F` | 11492 | 103.827 | 103.826 | +0.014 |
| 32 | `0x20` | 10847 | 110.001 | 110.000 | +0.015 |
| 33 | `0x21` | 10238 | 116.544 | 116.541 | +0.049 |
| 34 | `0x22` | 9664 | 123.466 | 123.471 | -0.061 |
| 35 | `0x23` | 9121 | 130.817 | 130.813 | +0.053 |
| 36 | `0x24` | 8609 | 138.597 | 138.591 | +0.069 |
| 37 | `0x25` | 8126 | 146.835 | 146.832 | +0.029 |
| 38 | `0x26` | 7670 | 155.565 | 155.563 | +0.012 |
| 39 | `0x27` | 7240 | 164.804 | 164.814 | -0.104 |
| 40 | `0x28` | 6833 | 174.620 | 174.614 | +0.061 |
| 41 | `0x29` | 6450 | 184.989 | 184.997 | -0.075 |
| 42 | `0x2A` | 6088 | 195.989 | 195.998 | -0.079 |
| 43 | `0x2B` | 5746 | 207.654 | 207.652 | +0.014 |
| 44 | `0x2C` | 5424 | 219.982 | 220.000 | -0.145 |
| 45 | `0x2D` | 5119 | 233.088 | 233.082 | +0.049 |
| 46 | `0x2E` | 4832 | 246.933 | 246.942 | -0.061 |
| 47 | `0x2F` | 4561 | 261.605 | 261.626 | -0.137 |
| 48 | `0x30` | 4305 | 277.161 | 277.183 | -0.132 |
| 49 | `0x31` | 4063 | 293.670 | 293.665 | +0.029 |
| 50 | `0x32` | 3835 | 311.129 | 311.127 | +0.012 |
| 51 | `0x33` | 3620 | 329.608 | 329.628 | -0.104 |
| 52 | `0x34` | 3417 | 349.189 | 349.228 | -0.193 |
| 53 | `0x35` | 3225 | 369.978 | 369.994 | -0.075 |
| 54 | `0x36` | 3044 | 391.978 | 391.995 | -0.079 |
| 55 | `0x37` | 2873 | 415.308 | 415.305 | +0.014 |
| 56 | `0x38` | 2712 | 439.963 | 440.000 | -0.145 |
| 57 | `0x39` | 2560 | 466.086 | 466.164 | -0.289 |
| 58 | `0x3A` | 2416 | 493.866 | 493.883 | -0.061 |
| 59 | `0x3B` | 2280 | 523.325 | 523.251 | +0.243 |
| 60 | `0x3C` | 2152 | 554.452 | 554.365 | +0.270 |
| 61 | `0x3D` | 2032 | 587.195 | 587.330 | -0.397 |
| 62 | `0x3E` | 1918 | 622.096 | 622.254 | -0.440 |
| 63 | `0x3F` | 1810 | 659.215 | 659.255 | -0.104 |
| 64 | `0x40` | 1708 | 698.583 | 698.456 | +0.314 |
| 65 | `0x41` | 1612 | 740.186 | 739.989 | +0.461 |
| 66 | `0x42` | 1522 | 783.955 | 783.991 | -0.079 |
| 67 | `0x43` | 1437 | 830.327 | 830.609 | -0.589 |
| 68 | `0x44` | 1356 | 879.926 | 880.000 | -0.145 |
| 69 | `0x45` | 1280 | 932.172 | 932.328 | -0.289 |
| 70 | `0x46` | 1208 | 987.732 | 987.767 | -0.061 |
| 71 | `0x47` | 1140 | 1046.649 | 1046.502 | +0.243 |
| 72 | `0x48` | 1076 | 1108.903 | 1108.731 | +0.270 |
| 73 | `0x49` | 1016 | 1174.390 | 1174.659 | -0.397 |
| 74 | `0x4A` | 959 | 1244.192 | 1244.508 | -0.440 |
| 75 | `0x4B` | 898 | 1328.708 | 1318.510 | +13.339 |
| 76 | `0x4C` | 854 | 1397.166 | 1396.913 | +0.314 |
| 77 | `0x4D` | 806 | 1480.372 | 1479.978 | +0.461 |
| 78 | `0x4E` | 761 | 1567.911 | 1567.982 | -0.079 |
| 79 | `0x4F` | 718 | 1661.811 | 1661.219 | +0.617 |
| 80 | `0x50` | 678 | 1759.853 | 1760.000 | -0.145 |
| 81 | `0x51` | 640 | 1864.344 | 1864.655 | -0.289 |
| 82 | `0x52` | 604 | 1975.464 | 1975.533 | -0.061 |
| 83 | `0x53` | 570 | 2093.298 | 2093.005 | +0.243 |
| 84 | `0x54` | 538 | 2217.807 | 2217.461 | +0.270 |
| 85 | `0x55` | 508 | 2348.780 | 2349.318 | -0.397 |
| 86 | `0x56` | 479 | 2490.981 | 2489.016 | +1.366 |
| 87 | `0x57` | 452 | 2639.779 | 2637.020 | +1.810 |
| 88 | `0x58` | 427 | 2794.333 | 2793.826 | +0.314 |
| 89 | `0x59` | 403 | 2960.744 | 2959.955 | +0.461 |
| 90 | `0x5A` | 380 | 3139.947 | 3135.963 | +2.198 |
| 91 | `0x5B` | 359 | 3323.621 | 3322.438 | +0.617 |
| 92 | `0x5C` | 339 | 3519.705 | 3520.000 | -0.145 |
| 93 | `0x5D` | 320 | 3728.688 | 3729.310 | -0.289 |
| 94 | `0x5E` | 302 | 3950.927 | 3951.066 | -0.061 |
| 95 | `0x5F` | 285 | 4186.596 | 4186.009 | +0.243 |

`idx=0` is treated as silence/off by the driver.

##### SND DMA-path table (raw)

For the DMA output path, the driver uses a second 96-entry u16 table at `PCSOUND.DRV` file offset `0x00C6` (accessed as `cs:[bx+0C6h]`). Its unit is **driver/hardware-specific** (the code uses a different conversion constant than the PIT path), so the most reliable “reference” is the raw values:

- Indices `1..31` are clamped to `0x03FF` (1023) in the shipped driver.
- The first non-clamped note index is `0x20` (32).

Extracted u16 values (96 entries, index `0..95`):

```
00..11: 0x0000 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF
12..23: 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF
24..35: 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03FF 0x03F9 0x03C0 0x038A 0x0357
36..47: 0x0327 0x02FA 0x02CF 0x02A7 0x0281 0x025D 0x023B 0x021B 0x01FC 0x01E0 0x01C5 0x01AC
48..59: 0x0194 0x017D 0x0168 0x0153 0x0140 0x012E 0x011D 0x010D 0x00FE 0x00F0 0x00E2 0x00D6
60..71: 0x00CA 0x00BE 0x00B4 0x00AA 0x00A0 0x0097 0x008F 0x0087 0x007F 0x0078 0x0071 0x006B
72..83: 0x0065 0x005F 0x005A 0x0054 0x0050 0x004C 0x0047 0x0043 0x0040 0x003C 0x0039 0x0035
84..95: 0x0032 0x0030 0x002D 0x002A 0x0028 0x0026 0x0024 0x0022 0x0020 0x001E 0x001C 0x001B
```

Musical temperament:

- The PIT divisor table is essentially a chromatic scale in **12‑TET** (equal temperament) over a wide range, with small tuning error from integer divisor rounding.
- The DMA-path table appears to be a more heavily quantized/limited mapping (it is clamped for low notes and then follows a roughly 12‑TET-like progression for higher notes).

Also note that the stream has an explicit-frequency opcode:

- `0xE6 <u16 freqHz>` bypasses the note table and plays a tone derived from `freqHz` (the driver converts it to a hardware-specific period/divisor) (PCSOUND.DRV analysis).

##### Control opcodes (>= 0x80)

Control opcodes are single-byte opcodes followed by 0/1/2 bytes of operands. Known ones:

- `0xCD <u8 count>`: set loop counter A (`count==0` is treated as 1)
- `0xCE <u16 back>`: loop back by `back` bytes while counter A is non-zero; when the counter reaches 0, skips the 2-byte operand
- `0xD1 <u8 count>`: set loop counter B (`count==0` is treated as 1)
- `0xD2 <u16 back>`: loop back by `back` bytes while counter B is non-zero; when the counter reaches 0, skips the 2-byte operand
- `0xE2 <u8 mul>`: set `tickMul` (`mul==0` is treated as 1)
- `0xE8 <u8 delta>`: `tickMul = max(1, tickMul - delta)` (saturates)
- `0xE9 <u8 delta>`: `tickMul = min(0xFF, tickMul + delta)` (saturates)
- `0xE6 <u16 freqHz>`: play a tone derived from `1193180 / freqHz` (PIT-based path; the DMA path uses a different constant)

Evidence for the above opcodes and operand sizes: PCSOUND.DRV analysis.

Any other control opcode (>=0x80) falls through to “stop”: it turns the speaker/DMA output off for that stream and ends it.

#### On-disk header before `0x78`

Bytes `0x00..0x77` exist on disk but are not dereferenced by the `PCSOUND.DRV` directory selector shown above; they can be treated as a bank header/metadata area for now.
