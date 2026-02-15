# Kyra / EMC2 notes

This repo contains two relevant “parsers” for Kyrandia 1 conversation scripts:

- **Runtime (DOS EXE) side**: the game’s `.EMC` loader in `MAIN.EXE` (runtime analysis).
- **Tooling (Python) side**: the repo’s decompiler/compiler that reads/writes EMC2 (`tools/conversation.py`).
  - For convenience/compat, `tools/kyra.py` can re-export the same API (decompile/compile).

## EMC2 container recap

The conversation scripts are stored as an **IFF-like chunked container**:

- File starts with `FORM` + big-endian total length.
- Payload starts with `EMC2`.
- Then a sequence of chunks: 4-byte tag + big-endian chunk size + chunk payload.
- Chunk payloads are word-aligned (pad 1 byte if size is odd).

Common tags you’ll see in code:

- `TEXT` — NUL-terminated string table
- `ORDR` — order table (entry offsets)
- `DATA` — 16-bit script words (opcodes/params)

### ORDR entrypoints (`globals = [...]`)

The `ORDR` chunk is a list of **u16 word indices into `DATA`**. Each entry is
an exported script entrypoint.

Tooling convention used by this repo:

- The decompiler prints `ORDR` as `globals = [global_0, global_1, ...]`.
- Each `global_N` is a synthetic label placed at the `DATA` word index
  `ORDR[N]`.
- The names are *not* meaningful by themselves; only the **index** matters.

Most room/scene scripts in this repo export **7** entrypoints
(`global_0..global_6`). Some special scripts export fewer (e.g. `_STARTUP.EMC`
exports only `global_0`).

#### Observed slot conventions (empirical)

The DOS runtime is the ultimate source of truth for *when* each entry is
invoked, but across the shipped scripts in this repo the **indices behave like
fixed “slots”** with consistent roles.

Runtime mechanics (confirmed from runtime analysis of `MAIN.EXE`):

- `sub_1EFD9` parses the EMC2 container and locates the `TEXT`/`ORDR`/`DATA`
  chunks by tag (you can see the literal tag constants pushed as `TEXT`,
  `ORDR`, `DATA`). It stores far pointers to these chunks in a “resource”
  struct.
- `sub_1F2AD(ctx, i)` selects an entrypoint by reading `ORDR[i]` and setting
  `ctx.ip = DATA + 2*ORDR[i]` (word addressing).

The notes below are based on scanning `conversations/*.EMC.kyra` (91 files) and
counting recurring patterns within each `global_N` body. Treat these as
high-confidence conventions for this codebase, not a universal guarantee.

- **Prelude ("slot -1") — internal helper library before `global_0`**
  - Many scripts contain **valid code at the start of `DATA` before `ORDR[0]`**.
    This is *not* an `ORDR` entry (the engine does not dispatch it directly),
    but rather a pool of **script-local helper routines**.
  - Tooling renders these helpers as `label func_<pc>` and emits calls as
    `func_<pc>(...)` (implemented via the VM’s call convention:
    `instr_2(2, 0x01)` + `jmp(4, <pc>)`, returning with `instr_8(2, 0x01)`).
  - Empirically (from scanning `original/*.EMC`), **90/91** shipped room scripts
    have `ORDR[0] > 0`; the smallest observed prefix is `ORDR[0] == 74` words.
    Some rooms have much larger prefixes (hundreds to thousands of words),
    effectively embedding a bigger per-room “library”.

- **Slot 0 (`global_0`) — room/setup & UI priming**
  - Rarely speaks (speech appears in 1/91 files).
  - Often performs “setup-ish” native calls and rectangle/mask refresh work.
  - Most frequent calls in this slot include:
    - `0x03` (call id 3): UI/scene helper (see documented call ids)
    - `0x0a` (call id 10): set `0x80` over rectangle
    - `0x28` (call id 40): build interpolation table
    - `0x8f` (call id 143): queue dirty rectangle

- **Slot 1 (`global_1`) — primary interaction / hotspot logic**
  - Speech appears in 48/91 files.
  - In 65/91 files this slot calls the common helper `func_0(...)` which looks
    like a rectangle hit-test (used to branch to different lines/actions).
  - In many scripts it also calls `func_28()` (39/91), a common helper used
    right before dialogue lines/actions.

- **Slot 2 (`global_2`) — location/title caption**
  - This is the most consistent slot.
  - 85/91 files contain the pattern `call_139(u16(0x00b3), <textIndex>)` in this
    slot, where `<textIndex>` points at a short location string like “A bedroom”,
    “The Chasm of Everfall”, etc.
  - Exception: `BURN.EMC` also calls `call_139(u16(0x00b3), 0x00)` from slot 1 to
    show a one-off status caption (“Crystal Ball taken.”) during an interaction,
    while slot 2 still selects the usual location caption.
  - Call id `0x8b` (decimal 139) is documented later as “resolve ptr;
    `sub_1D6BA(ptr, mode)`”; empirically in these scripts it is used as the
    caption/title setter with `mode = 0xB3` (passed as `u16(0x00b3)`) and
    `ptr` resolved from `TEXT[<textIndex>]`.

- **Slot 3 (`global_3`) — secondary interaction path (varies by room)**
  - Speech appears in 35/91 files.
  - Commonly gates on flags (`0x04` test / `0x05` set) and performs room-specific
    effects; call patterns are less uniform than slots 1/2.

- **Slot 4 (`global_4`) — rare/special-case hook**
  - Often empty (many scripts simply return 0).
  - When used, tends to be small and room-specific.

- **Slot 5 (`global_5`) — rare/special-case hook**
  - More likely than slot 4 to contain non-trivial logic (e.g. call id 24 occurs
    frequently here), but the semantic role is not yet pinned down.

- **Slot 6 (`global_6`) — rare/special-case hook (often inventory/state gated)**
  - Used in a minority of rooms; when present it often tests/sets flags and may
    emit a short line after some condition is met.
  - Example pattern (room-specific): test a flag (call id `0x04`), perform an
    effect, then set the flag (call id `0x05`) to prevent repetition.

#### Runtime call-sites (where the engine jumps to slot `i`)

These are concrete `sub_1F2AD(..., i)` call-sites observed in `MAIN.EXE`. They explain
why the `global_N` indices act like stable entrypoints, and also show that the
engine can dispatch to arbitrary slots in some cases.

- `i = 0` is executed immediately after loading a room/scene script (init).
- `i = 2` is executed from UI code that refreshes the location caption/title.
- `i = 3` is executed from input/interaction dispatch code.
- `i = 5` is executed from the main loop (periodic/tick-like hook).
- `i = 1`, `4`, `6` are also used by several overlay/UI routines that set
  parameters in globals like `word_42E5F/word_42E61/word_42E63` before running
  the slot.
- Some native-call handlers don’t use a fixed `i`: they take an index from the
  VM stack and call `sub_1F2AD(..., iFromStack)`, effectively “jumping to
  entrypoint N” at runtime.

Native (engine) call ids are documented later in this file under “Native call table used for conversations”.

### VM state model (as inferred from runtime)

The VM context (`ctx`) is a fixed-size struct (commonly at `dseg:7D9B` in this
build). Important fields (offsets are from the runtime code patterns in
`seg023`):

- `ctx.ip` (far pointer): `ctx+0`/`ctx+2` — current `DATA` word address
- `ctx.res` (far pointer): `ctx+4`/`ctx+6` — pointer to an EMC “resource” struct
- `ctx.acc` (u16): `ctx+8` — accumulator / return value register
- `ctx.fp` (u16): `ctx+0x0A` — frame pointer (index into the internal stack array)
- `ctx.sp` (u16): `ctx+0x0C` — stack pointer (index, grows downward)
- `ctx.stack[]` (u16 array): base `ctx+0x4a` — stack cells addressed as
  `ctx.stack[sp]`, `ctx.stack[fp+N]`, etc.
- `ctx.vars[]` (u16 array): base `ctx+0x0E` — script variable cells (indexed by `di`)

Stack conventions:

- On “push”, the VM does `--sp; stack[sp] = value`.
- On “pop”, the VM reads `value = stack[sp]; ++sp`.
- `sp` is bounded by `0..0x3c` in this build.

### VM instruction ids (0..18)

The interpreter implements instruction ids 0..18 (`cmp bx, 12h` in
`sub_1F3F1`). The shipped Kyrandia 1 scripts in this repo use ids:
`0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17` (id 18 exists but is rare/engine-specific).

Below is the behavior as implemented by the runtime.

#### 0 — `jmp`

Set `ip` to `res.data + 2*arg` (word index jump).

Two practical encodings matter:

- **Long-jump word** (`w & 0x8000`): `arg = w & 0x7fff` (15-bit absolute index).
  - This is what you see as `jmp(4, label_1234)` / `jmp(4, 0x04d2)` in Kyra.
- **Non-long word**: `arg` comes from the normal argument materialization path (sign-extended byte or extra-word immediate depending on bits 14..13).

#### 1 — `set_acc`

`acc = arg`.

Note: in shipped scripts, the `flags==0` form is used here (`arg=0..255`).

#### 2 — `stackctl` (sub-op by `arg`)

This instruction is a small “stack/control” group:

- `arg == 0`: push accumulator
  - `push(acc)`
- `arg == 1`: create a call frame (prologue)
  - pushes a computed return PC (word index) and the previous `fp`
  - updates `fp` to the new frame base

In the shipped scripts, this is typically followed by a long jump to the callee:

- `instr_2(2, 0x01)`
- `jmp(4, label_callee)`

This pattern behaves like a scripted `call label_callee` (execution resumes at the instruction after the `jmp(...)` when the callee returns via `instr_8(2, 0x01)`).
- otherwise: abort execution (sets `ip = 0`)

This pairs with instruction 8 (`ret`) below.

Tooling note (Kyra source sugar):

- The decompiler/compiler supports a sugar form for this pair:
  - `func_229(a, b, ...)`
  - expands to: stack-building pushes for `a, b, ...`, then `instr_2(2, 0x01)` and `jmp(4, 229)`.
    - If `nargs > 0`, the compiler also emits the canonical caller-side cleanup `instr_12(2, nargs)` at the return address (this matches the common patterns in shipped scripts).
- The numeric suffix is the *DATA word index* of the function entry point.
- The argument expressions use the same value-expression subset as `call(func_NNN, ...)` (e.g. `u16(0x1234)`, `var(0xNN)`, `add(...)`, etc).
- This sugar exists purely to avoid confusing “bare jmps” in listings: in this VM, `instr_2(2, 0x01)` + `jmp(4, target)` is a subroutine call, not a plain control-flow `goto`.

#### 3, 4 — `push`

Push immediate argument onto the stack:

- `push(arg)`

(Both ids 3 and 4 share the same runtime behavior.)

#### 5 — `load_var`

Load a VM variable and push it:

- `push(vars[arg])`

(`vars[]` base is `ctx+0x0e`.)

#### 6 — `load_arg`

Push a value from the call frame, addressed *below* `fp`:

- `push(stack[fp - (arg + 2)])`

#### 7 — `load_local`

Push a value from the call frame, addressed *at/above* `fp`:

- `push(local[arg])` where `local[i]` is stored at `ctx + 0x48 + 2*(fp+i)`.

#### 8 — `pop/ret` (sub-op by `arg`)

- `arg == 0`: pop into accumulator
  - `acc = pop()`
- `arg == 1`: return (epilogue)
  - if `sp == 0x3C`: abort execution (`ip = 0`)
  - else: restores `fp` and return PC from the stack, then jumps to that PC
- otherwise: abort execution (`ip = 0`)

#### 9 — `store_var`

Pop and store into `vars[arg]`:

- `vars[arg] = pop()`

#### 10 — `store_arg`

Pop and store into `stack[fp - (arg + 2)]`:

- `stack[fp - (arg + 2)] = pop()`

#### 11 — `store_local`

Pop and store into `local[arg]` (frame-local cell at `ctx+0x48`):

- `local[arg] = pop()`

#### 12 — `sp_add`

Adjust stack pointer upwards:

- `sp += arg`

#### 13 — `sp_sub`

Adjust stack pointer downwards:

- `sp -= arg`

#### 14 — `call` (native host call)

Call into the host (game engine) function table:

- Function id is `arg & 0xff`.
- The base table pointer is stored in the EMC resource struct at `res+0x0E`.
- Entry size is 4 bytes (far pointer).
- The handler is called as `fn(ctx)`.
- Return value (AX) is written to `acc`.

This is the “meaningful” opcode for conversations: the VM is small, and the
bulk of game-specific effects are implemented by these native call ids.

#### 15 — `jz` (pop-and-branch-if-zero)

- `v = pop()`
- if `v == 0`: `jmp(arg)`
- else: continue

Important detail for Kyra sources:

- In these scripts, opcode 15 almost always uses `flags == 1` (0x2000), meaning it consumes an extra u16 word from the stream.
- That extra word is commonly stored in the same long-jump encoding (`0x8000 | target`), so it *looks like* a `jmp(4, target)` instruction word when dumped.
- The tooling treats that operand word as non-executed (it is an immediate argument), but still uses its embedded target for control-flow labels.

#### 16 — `unary`

Unary op over the stack top:

- `x = pop()`
- compute `y = op(arg, x)`
- `push(y)`

Unary operator ids:

- `0`: logical NOT (`y = 1 if x == 0 else 0`)
- `1`: arithmetic negate (`y = -x`)
- `2`: bitwise NOT (`y = ~x`)

#### 17 — `binary`

Binary op over the two stack tops:

- `b = pop()`
- `a = pop()`
- compute `r = op(arg, a, b)`
- `push(r)`

Binary operator ids (as implemented in `sub_1F3F1`):

- `0`: boolean AND (`a!=0 && b!=0` → 1 else 0)
- `1`: boolean OR (`a!=0 || b!=0` → 1 else 0)
- `2`: `a == b`
- `3`: `a != b`
- `4`: `a <  b` (signed)
- `5`: `a <= b` (signed)
- `6`: `a >  b` (signed)
- `7`: `a >= b` (signed)
- `8`: `a + b`
- `9`: `a - b`
- `10`: `a * b`
- `11`: `a / b` (signed quotient)
- `12`: `a >> (b & 0xFF)` (arithmetic shift right)
- `13`: `a << (b & 0xFF)`
- `14`: `a & b`
- `15`: `a | b`
- `16`: `a % b` (signed remainder)
- `17`: `a ^ b`

#### 18 — `resume/jmp2` (rare)

If the stack is “full” (`sp == 0x3c`), aborts execution. Otherwise it pops two
values (`acc` then a jump target), clears an internal execution flag
(`ctx+0x0C2`), and jumps to the popped target.

### Native call table used for conversations

In this build, conversations load scripts with the native call table pointer
set to `dseg:2FE2`. This is a contiguous table of far pointers (4 bytes each),
indexed by the `call(..., id)` argument (0..255):

- `dseg:2FE2 + 4*id` → handler far pointer

Handlers are mostly in `seg004` (e.g. call id 1 → `loc_13F3E`).

Practical workflow to document a specific call id:

1. Find the table entry at `dseg:2FE2 + 4*id` in `MAIN.EXE`.
2. Jump to the target `loc_XXXX:` routine.
3. Identify its stack-argument reads (it usually pulls parameters from
   `ctx.stack[ctx.sp]` at offsets `+4A`, `+4C`, `+4E`, ...).
4. Follow calls to higher-level engine routines (often the real “effect”).

#### Documented call ids (selected)

These are higher-level notes for the most common/understood call ids. For a
complete index of all ids, see the table below.

##### `0x00` — `sub_1D9F9(0xFF, a, b)`

- Handler: `loc_13F0C` (argc=2)
- Args: `a=+4A`, `b=+4C`
- Returns: 0
- Usage: common low-level helper; typically appears as `call_0x00(a, b)`.
- Usage: common low-level helper; typically appears as `call(func_0, a, b)`.

##### `0x01` (aka `speak`, legacy `call_1`) — show `TEXT[text_id]`

- Handler: `loc_13F3E` (argc=3)
- Args: `text_id=+4A` (this is the *last* argument in decompiled `speak(a,b,text_id)`), plus `+4C/+4E` forwarded into `sub_1D216(text_ptr, speaker, param)`
- Returns: 0
- Usage: the primary “say line” / speech renderer in conversations; appears constantly as `call(func_1, ...)`.

Contrast with `0x34` (`tell`, legacy `call_52`): that call also resolves a `TEXT[text_id]`, but renders it via the UI caption/textbox renderer (`sub_20C1D`), not the speech renderer (`sub_1D216`).

##### `0x02` — `sub_213FC/sub_21451(x, y)`

- Handler: `loc_13FC5` (argc=2)
- Args: `x=+4A`, `y=+4C`
- Returns: 0
- Usage: coordinate-based engine operation; scripts call it repeatedly with computed `x/y`.

##### `0x03` — wrapper around `sub_1CA4C(n)`

- Handler: `loc_1401E` (argc=5)
- Args: `n` (byte) from `+4A`
- Returns: 0
- Usage: used around UI/scene/dialogue mode switches; appears as `call(func_3, ...)`.

##### `0x04` — test flag bit

- Handler: `loc_140B5` (argc=1)
- Args: `flag=+4A`
- Returns: 1 if set, else 0
- Usage: used to gate branches (scripts typically call `call(func_4, flag)` and branch based on the resulting `acc`).

##### `0x05` — set flag bit

- Handler: `loc_140DA` (argc=1)
- Args: `flag=+4A`
- Returns: 1
- Usage: script-side state progression.

##### `0x06` — clear flag bit

- Handler: `loc_140F6` (argc=1)
- Args: `flag=+4A`
- Returns: 1
- Usage: script-side state progression / undo.

##### `0x09` — clear `0x80` over rectangle

- Handler: `loc_141A1` (argc=4)
- Args: `(x1,y1,x2,y2)` from `+4A/+4C/+4E/+50`
- Effect: calls `sub_31039(x1, y1, w=x2-x1+1, h=y2-y1+1)` which ANDs `0x7F` into a `0x140`-stride buffer
- Usage: typically paired with `0x0a` to maintain a mask/coverage map over screen-space rectangles.
- Used in scripts (examples):
  - `conversations/CGATE.EMC.kyra:109`: `call(func_9, 0x77, u16(0x00ce), 0x6c, u16(0x00aa))`
  - `conversations/GATECV.EMC.kyra:351`: `call(func_9, sub(add(0x4b, 0x09), 0x01), sub(add(0x1d, 0x14), 0x01), 0x4b, 0x1d)`
  - `conversations/EXTHEAL.EMC.kyra:91`: `call(func_9, add(0x52, 0x0f), add(u16(0x0099), 0x21), 0x52, u16(0x0099))`

##### `0x0a` — set `0x80` over rectangle

- Handler: `loc_14208` (argc=4)
- Args: `(x1,y1,x2,y2)` from `+4A/+4C/+4E/+50`
- Effect: calls `sub_30FFC(x1, y1, w=x2-x1+1, h=y2-y1+1)` which ORs `0x80` into the same buffer as `0x09`
- Usage: typically paired with `0x09`.
- Used in scripts (examples):
  - `conversations/GENCAVB.EMC.kyra:86`: `call(func_10, sub(add(0x08, 0x30), 0x01), sub(add(u16(0x0091), 0x38), 0x01), 0x08, u16(0x0091))`
  - `conversations/GENCAVB.EMC.kyra:93`: `call(func_10, sub(add(0x58, 0x08), 0x01), sub(add(u16(0x010a), 0x36), 0x01), 0x58, u16(0x010a))`
  - `conversations/GENCAVB.EMC.kyra:172`: `call(func_10, 0x63, u16(0x00bf), sub(0x5a, 0x04), u16(0x008b))`

##### `0x0b` — `sub_3AB32(a,b,c,d)` with optional UI toggle

- Handler: `loc_1426F` (argc=4)
- Returns: 1 once when `word_3E967` was set (and clears it), else 0
- Usage: shows up in scripts as a conditional wait/loop primitive around UI state.

##### `0x0d` — draw/blit resource at `(x,y)`

- Handler: `loc_1444B` (argc=4)
- Args: `idx=+4A`, `x=+4C`, `y=+4E`, `flag=(+50 != 0)`
- Effect: selects a far pointer from `dseg:[0x751E + 4*idx]`; runs two passes via `sub_137C0(..., mode=2)` then `sub_137C0(..., mode=0)`, then engine updates
- Usage: appears around UI/scene rendering sequences.
- Used in scripts (examples):
  - `conversations/BELROOM.EMC.kyra:460`: `call(func_13, 0x00, 0x19, u16(0x0095), 0x00)`
  - `conversations/LIBRARY.EMC.kyra:214`: `call(func_13, 0x00, add(0x32, 0x08), add(u16(0x0090), 0x08), 0x09)`
  - `conversations/GEMCUT.EMC.kyra:889`: `call(func_13, 0x00, 0x5d, u16(0x00f8), 0x06)`

##### `0x0e` — wrapper around `sub_1CA4C(n)`

- Handler: `loc_14515` (argc=1)
- Args: `n` (byte) from `+4A`
- Returns: 0
- Usage: small mode-setting helper.

##### `0x10` — set `word[0x7442 + 2*idx] = 1`

- Handler: `loc_14610` (argc=1)
- Returns: 0
- Usage: toggles an indexed engine flag.

##### `0x11` — set `word[0x7442 + 2*idx] = 0`

- Handler: `loc_1462E` (argc=1)
- Returns: 0
- Usage: toggles an indexed engine flag.

##### `0x17` — `sub_20272(x)`

- Handler: `loc_14763` (argc=1)
- Returns: 0
- Usage: commonly used as an action/trigger with a small numeric id.

##### `0x18` — `sub_2015D(a, b)`

- Handler: `loc_14781` (argc=2)
- Returns: 0
- Usage: frequently paired with other stateful calls during scripted sequences.

##### `0x21` — numeric helper + `sub_213FC(dx, ax)`

- Handler: `sub_14934` (argc=1)
- Returns: 0
- Usage: called repeatedly (often with `1`) as part of loops; likely a timing/position helper.

##### `0x25` — set `word[0x76CC + 0x2D*idx] = 1`

- Handler: `loc_14A34` (argc=1)
- Returns: 0
- Usage: per-index state flag (set).

##### `0x26` — `sub_3A9EF()`

- Handler: `loc_14A57` (argc=0)
- Returns: 0
- Usage: used as a step in many scripted UI/scene sequences.

##### `0x28` — build interpolation table

- Handler: `loc_14A76` (argc=4)
- Returns: 1
- Usage: used before time-based transitions/animations; scripts typically call it once then enter loops.

##### `0x29` — allocate handle in per-slot table

- Handler: `loc_14B32` (argc=4)
- Args: `idx=+4A`, `slot=+4C` (plus more)
- Returns: 0
- Usage: resource allocation / setup for later repeated calls (see `0x2c/0x3f/0x63`).

##### `0x2a` — free handle from per-slot table

- Handler: `loc_14C18` (argc=1)
- Returns: 0
- Usage: teardown/cleanup counterpart of `0x29`.

##### `0x2c` — time-based loop; `sub_2B9EC` + `sub_30C95`

- Handler: `loc_14D49` (argc=5)
- Returns: 0
- Usage: frequently used for scripted timed effects; look for repeated `call_0x2c(...)` calls.
- Usage: frequently used for scripted timed effects; look for repeated `call(func_44, ...)` calls.

##### `0x2d` — room/scene transition wrapper

- Handler: `loc_14E12` (argc=5)
- Returns: 0
- Usage: changes room/scene; these calls tend to be “big steps” in scripts.

##### `0x34` (aka `tell`, legacy `call_52`) — show `TEXT[text_id]` via `sub_20C1D` (UI caption/textbox)

- Handler: `loc_1527E` (argc=4)
- Returns: 0
- Args (VM stack reads; matches decompiled `tell(style, x_center, y_base, text_id)`):
  - `text_id=+4A` (resolved via `sub_310AD(text_id)`)
  - `x_center=+4C`
  - `y_base=+4E`
  - `style=+50` (byte)
- Effect: `sub_20C1D(text_ptr, x_center, y_base, style, 0, 2)`.
- Notes:
  - `sub_20C1D` splits the resolved string into lines on `0x0D` (CR) and draws each line with a 10-pixel vertical step.
  - The horizontal placement is centered around `x_center`; `sub_1C8FE` clamps the computed left/right bounds into a safe on-screen range.
  - `style` is forwarded into the text drawing routine (`sub_19629`) and also stored into `byte_3E913`.
  - On exit, `sub_20C1D` sets `word_40998=1`; `0x35` (`sub_20D4D(2,0)`) checks this flag and re-invokes the same frame routine (`es:off_40BB2`) with the last caption geometry.
- Usage: used for narration/caption-like text (often “replica-like” strings that are not shown via `0x01`).

##### `0x35` — `sub_20D4D(2, 0)`

- Handler: `loc_15326` (argc=0)
- Returns: 0
- Usage: often used as a “barrier” step in scripts.

##### `0x36` — `sub_30C63()`

- Handler: `loc_1533C` (argc=0)
- Returns: 0
- Usage: engine pre-update wrapper; many render/transfer calls are bracketed by `0x36` / `0x37`.

##### `0x37` — `sub_30C95()`

- Handler: `loc_15348` (argc=0)
- Returns: 0
- Usage: engine post-update wrapper.

##### `0x38` — get `seg099[idx].word_5D2`

- Handler: `loc_15354` (argc=1)
- Returns: `seg099[idx].word_5D2`
- Usage: often used as an input into conditionals or arithmetic.

##### `0x3a` — mutate `seg099[idx]` + engine updates

- Handler: `loc_1539E` (argc=3)
- Returns: 0
- Usage: configuration of a per-`idx` record (often part of animation/script state machines).

##### `0x3b` — opaque rectangle blit/copy

- Handler: `loc_15435` (argc=6)
- Args: `(x,y,w,h,src_page,dst_page)` from `+4A..+54`
- Effect: calls `sub_23121` / `sub_2312F` (clipped rectangle copy; mode selects opaque vs skip-zero)
- Usage: used to copy blocks between pages/buffers (UI composition, transitions).
- Used in scripts (examples):
  - `conversations/GEMCUT.EMC.kyra:294`: `call(func_59, 0x02, 0x00, 0x48, 0x58, 0x20, 0x68)`
  - `conversations/LIBRARY.EMC.kyra:143`: `call(func_59, 0x00, 0x02, u16(0x0080), u16(0x00b0), 0x08, 0x48)`

##### `0x3d` — random in inclusive range

- Handler: `loc_15582` (argc=2)
- Returns: `rand(min(a,b)..max(a,b))`
- Usage: used for random branching/variation.
- Used in scripts (examples):
  - `conversations/_STARTUP.EMC.kyra:84`: `call(func_61, 0x03, 0x00)`
  - `conversations/GEMCUT.EMC.kyra:216`: `call(func_61, 0x0c, 0x08)`

##### `0x3f` — time-based loop; `sub_2B9EC` + `sub_30C95`

- Handler: `loc_155E6` (argc=5)
- Returns: 0
- Usage: similar to `0x2c` (timed effects).

##### `0x40` — range-stepped timed effect (cycle primitive)

- Handler: `loc_156B2` (argc=7)
- Returns: 0
- Stack args (from `ctx.stack[sp]`):
  - `+4A` (`arg0`): start index (word)
  - `+4C` (`arg1`): end index (word)
  - `+4E` (`arg2`): parameter forwarded into `sub_2B9EC` (word)
  - `+50` (`arg3`): parameter forwarded into `sub_2B9EC` (word)
  - `+52` (`arg4`): per-step delay/ticks (word)
  - `+54` (`arg5`): selector/index into a pointer table at `seg004:3929/392B` (word)
  - `+56` (`arg6`): repeat count (word; clamped to at least 1)
- Behavior (from `MAIN.EXE` runtime analysis):
  - Repeats `arg6` times.
  - Walks `si` from `arg0` to `arg1` inclusive (ascending if `arg0<=arg1`, else descending).
  - For each `si`, calls `sub_2B9EC(...)` using pointers selected by `arg5`, then busy-waits until `arg4` time passes (calls `sub_1FA74`/`sub_1769F`), then calls `sub_30C95`.
- Notes:
  - This handler does **not** read the EMC `TEXT` table or call `sub_1D216`; it looks like an animation/effect stepping primitive.

##### `0x41` — mutate `seg099[idx]` (with optional `sub_1769F`)

- Handler: `loc_15832` (argc=4)
- Returns: 0
- Usage: updates per-`idx` fields; often follows `0x3a` or precedes timed loops.

##### `0x42` — set `word[0x76CC + 0x2D*idx] = 0`

- Handler: `loc_158D1` (argc=1)
- Returns: 0
- Usage: per-index state flag (clear), counterpart to `0x25`.

##### `0x8b` — draw title/caption from `TEXT`; `sub_1D6BA(ptr, mode)`

- Handler: `loc_17364` (argc=2)
- Returns: 0
- Usage: resource-indexed engine operation; appears as `call(2, 0x8b)` / `call(2, func_139)` in scripts.
- Behavior (from `MAIN.EXE` runtime analysis):
  - Signature in `.kyra` sources: `title(mode, text_index)`.
    - `text_index` is used as an index into the current EMC `TEXT` offset table.
    - `mode` is read as a byte (`mode = arg0 & 0xff`) and forwarded to the UI routine.
  - Resolves `ptr` from `TEXT[text_index]` by reading a big-endian u16 offset word, then byte-swapping via `sub_310AD` (which is just `xchg ah,al`).
  - Computes `ptr = text_base + swapped_offset` and calls `sub_1D6BA(ptr, mode)`.
  - `mode` selects a 3-byte record from `dword_4439C` (used as UI color/style).
- Notes:
  - In the shipped scripts in this repo, `mode` is most often passed as `u16(0x00b3)` (so `mode == 0xB3`), but a few scripts use other values (e.g. `0x0e`, `0x0c`).

##### `0x8f` — queue dirty rectangle `(x,y,w,h)`

- Handler: `loc_17479` (argc=4)
- Effect: pushes an inclusive rectangle `(x0,y0,x1=x+w-1,y1=y+h-1)` into the first free slot of the 11-slot list at `dseg:826E`
- Usage: typically appears around drawing/blitting sequences to mark regions for refresh.
- Used in scripts (examples):
  - `conversations/BELROOM.EMC.kyra:288`: `call(func_143, 0x28, 0x40, 0x08, u16(0x0080))`
  - `conversations/GEMCUT.EMC.kyra:545`: `call(func_143, 0x30, 0x20, 0x44, 0x50)`
  - `conversations/GRAVE.EMC.kyra:220`: `call(func_143, 0x58, u16(0x0088), 0x08, 0x78)`

#### Call id index (complete)

Derived from `MAIN.EXE` runtime analysis (`dseg:2FE2` table) + current `conversations/*.kyra`.

- Native call table size: 155 entries (`0x00..0x9a`).
- Script usage (`call(2, id)` / `call(func_NNN, ...)`): 140 unique ids (`0x00..0x99`), invalid: none.
- Script usage (`call(4, id)`): none observed in the current `conversations/*.kyra` outputs.
- Unused `call(2)` ids (present in table but not referenced by scripts): 15: `0x07`, `0x23`, `0x3e`, `0x4b`, `0x4d`, `0x4e`, `0x4f`, `0x65`, `0x69`, `0x6a`, `0x75`, `0x84`, `0x87`, `0x88`, `0x9a`.

No-op check (strict): no handler in the `dseg:2FE2` table is a pure “do nothing and return 0” no-op; even the smallest ones are simple state getters/setters.

Current documentation coverage (table below, 155 ids total): 39 `documented`, 33 `trivial*` (simple getters/setters), 83 `unknown` (non-trivial handlers not yet reverse-engineered).

Classification notes:

- `documented`: described above in this file.
- `trivial-*`: small handler with no internal `call`; typically a simple getter/setter over one global or resource field.
- `unknown`: non-trivial handler not yet reverse-engineered.

| id | handler | used | argc | kind | notes |
|---:|---------|:----:|-----:|------|-------|
| `0x00` | `loc_13F0C` | yes | 2 | documented | sub_1D9F9(0xFF,a,b) |
| `0x01` | `loc_13F3E` | yes | 3 | documented | show TEXT[text_id] via sub_1D216 |
| `0x02` | `loc_13FC5` | yes | 2 | documented | sub_213FC/sub_21451(x,y) |
| `0x03` | `loc_1401E` | yes | 5 | documented | sub_1CA4C(n) |
| `0x04` | `loc_140B5` | yes | 1 | documented | test flag bit (sub_196A5) |
| `0x05` | `loc_140DA` | yes | 1 | documented | set flag bit (sub_196E0) |
| `0x06` | `loc_140F6` | yes | 1 | documented | clear flag bit (sub_1970E) |
| `0x07` | `loc_14112` | no | 1 | unknown |  |
| `0x08` | `loc_1416D` | yes | 0 | trivial |  |
| `0x09` | `loc_141A1` | yes | 4 | documented | clear `0x80` bit over rect via `sub_31039(x1,y1,w,h)` |
| `0x0a` | `loc_14208` | yes | 4 | documented | set `0x80` bit over rect via `sub_30FFC(x1,y1,w,h)` |
| `0x0b` | `loc_1426F` | yes | 4 | documented | sub_3AB32(a,b,c,d) UI toggle if c==0 |
| `0x0c` | `loc_14353` | yes | 3 | unknown |  |
| `0x0d` | `loc_1444B` | yes | 4 | documented | draw resource `dseg:[0x751E+4*idx]` at `(x,y)`; `flag!=0` affects `sub_137C0` |
| `0x0e` | `loc_14515` | yes | 1 | documented | sub_1CA4C(n) |
| `0x0f` | `loc_14534` | yes | 2 | unknown |  |
| `0x10` | `loc_14610` | yes | 1 | documented | set word[0x7442+2*idx]=1 |
| `0x11` | `loc_1462E` | yes | 1 | documented | set word[0x7442+2*idx]=0 |
| `0x12` | `loc_1464C` | yes | 0 | unknown |  |
| `0x13` | `loc_14665` | yes | 0 | trivial | returns `1` iff `byte_3EB0E == 0xFF` |
| `0x14` | `loc_14678` | yes | 0 | documented | reset mouse cursor: `sub_30928(hotspot=(1,1), shape=seg100:word_33C5D/word_33C5F)`; sets `byte_3EB0E=0xFF` |
| `0x15` | `loc_14684` | yes | 1 | unknown |  |
| `0x16` | `loc_146FD` | yes | 4 | unknown |  |
| `0x17` | `loc_14763` | yes | 1 | documented | sub_20272(x) |
| `0x18` | `loc_14781` | yes | 2 | documented | sub_2015D(a,b) |
| `0x19` | `loc_147AE` | yes | 2 | unknown |  |
| `0x1a` | `loc_147DC` | yes | 0 | trivial-set | seg004:08DF                 mov     word_3E23E, 1 |
| `0x1b` | `loc_147EA` | yes | 0 | trivial-set | seg004:08ED                 mov     word_3E23E, 0 |
| `0x1c` | `loc_147F7` | yes | 0 | trivial-get | seg004:08FA                 mov     ax, word_3E23E |
| `0x1d` | `loc_147FF` | yes | 2 | unknown |  |
| `0x1e` | `loc_14892` | yes | 2 | unknown |  |
| `0x1f` | `loc_148D6` | yes | 2 | unknown |  |
| `0x20` | `loc_14912` | yes | 1 | trivial-set |  |
| `0x21` | `sub_14934` | yes | 1 | documented | sub_1038F(...); sub_213FC(dx,ax) |
| `0x22` | `loc_14971` | yes | 1 | unknown |  |
| `0x23` | `loc_1499E` | no | 1 | unknown |  |
| `0x24` | `loc_14A12` | yes | 1 | unknown |  |
| `0x25` | `loc_14A34` | yes | 1 | documented | set word[0x76CC+0x2D*idx]=1 |
| `0x26` | `loc_14A57` | yes | 0 | documented | sub_3A9EF() |
| `0x27` | `loc_14A63` | yes | 0 | unknown |  |
| `0x28` | `loc_14A76` | yes | 4 | documented | build interpolation table; word_3EA04=1; returns 1 |
| `0x29` | `loc_14B32` | yes | 4 | documented | alloc handle in per-slot table (sub_2B59B) |
| `0x2a` | `loc_14C18` | yes | 1 | documented | free per-slot handle (sub_2B9B7) |
| `0x2b` | `loc_14C68` | yes | 5 | unknown |  |
| `0x2c` | `loc_14D49` | yes | 5 | documented | time-based loop; sub_2B9EC; sub_30C95 |
| `0x2d` | `loc_14E12` | yes | 5 | documented | room/scene transition wrapper (sub_19ADA) |
| `0x2e` | `loc_14E8E` | yes | 2 | trivial-set | set `word_3EBB1=a`, `word_3EBB3=b`; if both `0xFFFF` then `es:[dword_42294+4]=0x58` |
| `0x2f` | `loc_14ED1` | yes | 6 | unknown |  |
| `0x30` | `loc_14FB4` | yes | 4 | unknown |  |
| `0x31` | `loc_151C1` | yes | 1 | trivial-set | seg004:12DF                 mov     word_425DB, 0 |
| `0x32` | `loc_151F8` | yes | 3 | unknown |  |
| `0x33` | `loc_145A2` | yes | 2 | unknown |  |
| `0x34` | `loc_1527E` | yes | 4 | documented | UI caption/textbox: resolve ptr; sub_20C1D(ptr,x_center,y_base,style,0,2) |
| `0x35` | `loc_15326` | yes | 0 | documented | sub_20D4D(2,0) |
| `0x36` | `loc_1533C` | yes | 0 | documented | sub_30C63() |
| `0x37` | `loc_15348` | yes | 0 | documented | sub_30C95() |
| `0x38` | `loc_15354` | yes | 1 | documented | returns seg099[idx].word_5D2 |
| `0x39` | `loc_15379` | yes | 1 | unknown |  |
| `0x3a` | `loc_1539E` | yes | 3 | documented | mutate seg099[idx] fields; engine updates |
| `0x3b` | `loc_15435` | yes | 6 | documented | opaque blit rect `(x,y,w,h)` from `src_page` to `dst_page` (sub_23121) |
| `0x3c` | `loc_154D2` | yes | 5 | unknown |  |
| `0x3d` | `loc_15582` | yes | 2 | documented | random in inclusive range via `sub_279A6(a,b)` |
| `0x3e` | `loc_155BF` | no | 1 | unknown |  |
| `0x3f` | `loc_155E6` | yes | 5 | documented | time-based loop; sub_2B9EC; sub_30C95 |
| `0x40` | `loc_156B2` | yes | 7 | documented | range-stepped timed effect (cycle primitive) |
| `0x41` | `loc_15832` | yes | 4 | documented | mutate seg099[idx] field; optional sub_1769F |
| `0x42` | `loc_158D1` | yes | 1 | documented | set word[0x76CC+0x2D*idx]=0 |
| `0x43` | `loc_158FC` | yes | 3 | unknown |  |
| `0x44` | `loc_15980` | yes | 0 | trivial-set | seg004:1A83                 mov     word_3EB00, 0 |
| `0x45` | `loc_1598D` | yes | 0 | trivial-get | seg004:1A90                 mov     ax, word_3EB00 |
| `0x46` | `loc_15995` | yes | 0 | unknown |  |
| `0x47` | `loc_159A6` | yes | 4 | unknown |  |
| `0x48` | `loc_15A66` | yes | 3 | unknown |  |
| `0x49` | `loc_15AC5` | yes | 6 | unknown |  |
| `0x4a` | `loc_15B7D` | yes | 0 | unknown |  |
| `0x4b` | `loc_15B95` | no | 2 | unknown |  |
| `0x4c` | `loc_15C03` | yes | 6 | unknown |  |
| `0x4d` | `loc_15CCE` | no | 2 | unknown |  |
| `0x4e` | `loc_15D1E` | no | 1 | unknown |  |
| `0x4f` | `loc_15D8A` | no | 5 | unknown |  |
| `0x50` | `loc_15EC6` | yes | 2 | unknown |  |
| `0x51` | `loc_15F71` | yes | 3 | unknown |  |
| `0x52` | `loc_1613C` | yes | 0 | unknown |  |
| `0x53` | `loc_161F1` | yes | 0 | unknown |  |
| `0x54` | `loc_16230` | yes | 1 | trivial-set | seg004:2342                 mov     word_40DBF, dx |
| `0x55` | `loc_1624A` | yes | 6 | unknown |  |
| `0x56` | `loc_16313` | yes | 0 | unknown | calls `sub_1911C` (walks list at `word_425D6/word_425D8`, invokes `sub_1801D` for eligible nodes, clears `[node+5]`) |
| `0x57` | `loc_1631F` | yes | 1 | trivial |  |
| `0x58` | `loc_16348` | yes | 1 | trivial |  |
| `0x59` | `loc_16362` | yes | 2 | unknown |  |
| `0x5a` | `loc_163A8` | yes | 1 | trivial-get | returns signed byte `seg099[idx*0x26 + 0x5C3]` |
| `0x5b` | `loc_163D6` | yes | 1 | unknown |  |
| `0x5c` | `loc_1746D` | yes | 0 | unknown |  |
| `0x5d` | `loc_16413` | yes | 0 | unknown |  |
| `0x5e` | `loc_164F2` | yes | 1 | unknown |  |
| `0x5f` | `loc_165B4` | yes | 1 | trivial-set | seg004:26C6                 mov     byte_3E96C, al |
| `0x60` | `loc_165CD` | yes | 3 | unknown |  |
| `0x61` | `loc_1661E` | yes | 0 | unknown |  |
| `0x62` | `loc_1662A` | yes | 5 | unknown |  |
| `0x63` | `loc_16754` | yes | 2 | unknown |  |
| `0x64` | `loc_1679B` | yes | 0 | trivial |  |
| `0x65` | `loc_16834` | no | 1 | unknown |  |
| `0x66` | `loc_1685A` | yes | 2 | unknown |  |
| `0x67` | `loc_16896` | yes | 0 | trivial-get | seg004:2999                 mov     ax, word_3EAAC |
| `0x68` | `loc_1689E` | yes | 1 | trivial-set | seg004:29B0                 mov     word_3EAAC, ax |
| `0x69` | `loc_168B5` | no | 3 | unknown |  |
| `0x6a` | `loc_1694E` | no | 5 | unknown |  |
| `0x6b` | `loc_169C9` | yes | 2 | unknown |  |
| `0x6c` | `loc_16A1F` | yes | 1 | unknown |  |
| `0x6d` | `loc_16A4D` | yes | 3 | unknown |  |
| `0x6e` | `loc_16AA2` | yes | 0 | unknown |  |
| `0x6f` | `loc_16AB5` | yes | 2 | unknown |  |
| `0x70` | `loc_16AF5` | yes | 1 | unknown |  |
| `0x71` | `loc_16B22` | yes | 1 | trivial |  |
| `0x72` | `loc_16B47` | yes | 1 | trivial | returns `1` iff `(word_3EA18 & mask) != 0` |
| `0x73` | `loc_16B72` | yes | 0 | unknown |  |
| `0x74` | `loc_16CFD` | yes | 1 | unknown |  |
| `0x75` | `loc_16D2D` | no | 1 | trivial |  |
| `0x76` | `loc_16D49` | yes | 2 | unknown |  |
| `0x77` | `loc_16D83` | yes | 1 | unknown |  |
| `0x78` | `loc_16DA9` | yes | 0 | unknown |  |
| `0x79` | `loc_16DCB` | yes | 2 | unknown |  |
| `0x7a` | `loc_16E07` | yes | 0 | trivial-get | seg004:2F0A                 mov     ax, word_3EB0F |
| `0x7b` | `loc_16E0F` | yes | 5 | unknown |  |
| `0x7c` | `loc_16E90` | yes | 3 | unknown |  |
| `0x7d` | `loc_16EF2` | yes | 1 | trivial-set | seg004:3004                 mov     word_3E96A, ax |
| `0x7e` | `loc_16F0B` | yes | 2 | unknown |  |
| `0x7f` | `loc_1701D` | yes | 2 | unknown |  |
| `0x80` | `loc_17064` | yes | 0 | trivial |  |
| `0x81` | `loc_1706E` | yes | 1 | trivial-set | seg004:3180                 mov     byte_3EA1C, dl |
| `0x82` | `loc_17088` | yes | 1 | trivial-get | seg004:319E                 mov     ax, word_3EA1D |
| `0x83` | `loc_170B2` | yes | 2 | unknown |  |
| `0x84` | `loc_170EA` | no | 3 | unknown |  |
| `0x85` | `loc_1718D` | yes | 1 | unknown |  |
| `0x86` | `loc_1723D` | yes | 0 | unknown |  |
| `0x87` | `loc_172BF` | no | 2 | unknown |  |
| `0x88` | `loc_172FB` | no | 0 | trivial | seg004:33FE                 mov     word_3EA04, 0 |
| `0x89` | `loc_17309` | yes | 1 | unknown |  |
| `0x8a` | `loc_1732E` | yes | 2 | unknown |  |
| `0x8b` | `loc_17364` | yes | 2 | documented | `title(mode:u8, text_index:u16)`: resolve `ptr = TEXT[text_index]` via big-endian offset table (`sub_310AD` byte-swap) and draw as a UI title/label via `sub_1D6BA(ptr,mode)`; `mode` selects a 3-byte entry from `dword_4439C`. Shipped scripts usually pass `u16(0x00b3)` (so `mode==0xB3`). |
| `0x8c` | `loc_173D7` | yes | 2 | unknown |  |
| `0x8d` | `loc_17412` | yes | 1 | unknown |  |
| `0x8e` | `loc_17437` | yes | 2 | unknown |  |
| `0x8f` | `loc_17479` | yes | 4 | documented | queue dirty rect `(x,y,w,h)` into 11-slot list at `dseg:826E` |
| `0x90` | `loc_174DF` | yes | 0 | trivial-set | seg004:35E9                 mov     word_3E947, 1 |
| `0x91` | `loc_174F3` | yes | 0 | unknown |  |
| `0x92` | `loc_17505` | yes | 1 | trivial-set | seg004:3617                 mov     word_3EB0A, ax |
| `0x93` | `loc_1751C` | yes | 0 | unknown |  |
| `0x94` | `loc_17528` | yes | 0 | unknown |  |
| `0x95` | `loc_17534` | yes | 1 | trivial-set | seg004:3646                 mov     word_3E941, ax |
| `0x96` | `loc_1754B` | yes | 0 | trivial |  |
| `0x97` | `loc_17574` | yes | 5 | unknown |  |
| `0x98` | `loc_175CE` | yes | 1 | trivial-set | seg004:36E0                 mov     word_3EB04, ax |
| `0x99` | `loc_175E5` | yes | 6 | unknown |  |
| `0x9a` | `loc_17698` | no | 0 | trivial |  |

