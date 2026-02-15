'''Kyra: a tiny Python-like source format for Kyrandia EMC2 scripts.

This module provides two functions:

- decompile(emc_bytes: bytes, name: str | None = None) -> str
    Converts an EMC2 (FORM/EMC2) binary into Kyra source.

- compile(source) -> bytes
    Parses Kyra source and emits an EMC2 binary.

Design goal: round-trip byte-for-byte:
    compile(decompile(original_bytes)) == original_bytes

Kyra is intentionally "python-like" but is not Python. Parsing is implemented
here without using python's ast module.

Source format (v2):

- `strings = { ... }` is required.
    Keys are bare identifiers (e.g. s000) and values are single-quoted ASCII.
    Key names are cosmetic; order of entries defines TEXT indices.

- `globals = [global_0, global_1, ...]` encodes the ORDR table.
    Each global label resolves to a u16 word index into DATA.

- Everything else is "code":
    - `label_name:` defines a label at the current instruction index.
    - Instruction statements encode the DATA stream, one instruction per line:
            jmp(flags, arg)        # arg may be a number (0..255) or a label
            ifnot(flags, target)   # conditional jump (used as an "if")
                        push16(value)          # 2-word immediate push (instr 3, flags 1)
            push(flags, arg)
            call(flags, arg)
            unary(flags, arg)
            binary(flags, arg)
            instr_N(flags, arg)  for unknown instruction ids.

- Call sugar:
    - `call_NNN(...)` is sugar for pushing N argument values, calling native
        function NNN (0..255), then doing the canonical stack fixup
        `instr_12(2, N)`.
        - Arguments may be simple byte immediates (`0x00`..`0xff`) or VM value
            expressions reconstructed from the stack machine:
                        u16(0x1234)            # push16
                        var(0xNN) / arg(0xNN) / local(0xNN) / acc()
                        not(x) / neg(x) / bnot(x)
                        add(a,b) / sub(a,b) / ... (binary ops)

Legacy support:
- For older sources, `entries = [...]` is still accepted as an alias for
    `globals = [...]`.
- For older sources, `entry(i, offset)` statements are still accepted if
    `globals = [...]` is not present.

TEXT chunk presence:
- In the original Kyrandia EMC files in this repo, 0 strings is represented as
    a missing TEXT chunk (not a present-but-empty TEXT chunk).
- If you ever need to force an empty-but-present TEXT chunk, add:
            # text: present
    If you need to force TEXT to be omitted, add:
            # text: absent
'''

from dataclasses import dataclass
from typing import Iterator

import re
import unicodedata


# Preferred human-readable aliases for select native calls.
# These are purely syntactic sugar: the underlying EMC2 binary encoding is
# unchanged.
_NATIVE_CALL_ID_TO_ALIAS: dict[int, str] = {
    1: 'speak',
    52: 'tell',
    139: 'title',
}
_NATIVE_CALL_ALIAS_TO_ID: dict[str, int] = {v: k for k, v in _NATIVE_CALL_ID_TO_ALIAS.items()}


def _native_call_symbol(call_id: int) -> str:
    alias = _NATIVE_CALL_ID_TO_ALIAS.get(call_id)
    return alias if alias is not None else f'call_{call_id}'


def decompile(emc_bytes: bytes, name: str | None = None, *, call_sugar: bool = True) -> str:
    form = _parse_emc2(emc_bytes)

    strings = form.strings

    def _decode_word(word: int) -> tuple[int, int, int, bool]:
        """Decode one DATA word.

        The runtime has a special long-jump encoding: if bit 15 is set, the
        interpreter forces `instr=0` and uses the lower 15 bits as an absolute
        jump target (word index).

        Returns (flags, instr, arg, is_long_jmp).
        - For long-jumps, we normalize to (4, 0, target15, True).
        - For normal words, arg is the low 8-bit immediate.
        """
        if word & 0x8000:
            return 4, 0, word & 0x7FFF, True
        flags = (word >> 13) & 0x7
        instr = (word >> 8) & 0x1F
        arg = word & 0xFF
        return flags, instr, arg, False

    def _is_push16_at(pc: int) -> bool:
        # In these scripts, instr 3 with flags==1 is a 2-word push immediate.
        if pc + 1 >= len(form.data):
            return False
        flags, instr, _, is_long = _decode_word(form.data[pc])
        return (not is_long) and instr == 3 and flags == 1

    def _push16_imm_at(pc: int) -> int:
        return form.data[pc + 1]

    # Some instructions are multi-word. In the shipped scripts, opcode 15
    # (flags=1) consumes an extra u16 word as an immediate argument. That operand
    # is typically encoded as a long-jump word (bit 15 set) and therefore *looks
    # like* `jmp(4, target)`.
    #
    # The operand word is not an addressable instruction, so we treat its PC as
    # non-executed for label placement/decoding. We still treat its embedded jump
    # target as a real control-flow edge.
    ifnot_operand_pcs: set[int] = set()
    executed_pcs: set[int] = set()
    embedded_jmp_targets: set[int] = set()
    pc = 0
    while pc < len(form.data):
        executed_pcs.add(pc)
        word = form.data[pc]
        if _is_push16_at(pc):
            pc += 2
            continue
        flags, instr, _, _ = _decode_word(word)
        if instr == 15 and flags == 1 and pc + 1 < len(form.data):
            op_word = form.data[pc + 1]
            ifnot_operand_pcs.add(pc + 1)

            _, op_instr, op_arg, op_is_long = _decode_word(op_word)
            # Canonical encoding is a long-jump word; if not, only treat it as a
            # jump when it still decodes like a short `jmp`.
            if op_is_long or op_instr == 0:
                embedded_jmp_targets.add(op_arg)

            pc += 2
        else:
            pc += 1

    valid_jump_targets = executed_pcs | {len(form.data)}

    def prev_executed(before_pc: int, lower_bound: int) -> int | None:
        for p in range(before_pc - 1, lower_bound - 1, -1):
            if p in executed_pcs:
                return p
        return None

    # When we emit structured `if/else:` blocks we intentionally hide some
    # instruction words (notably: the join-jump at the end of a then-branch).
    # If other jumps target those hidden PCs we must avoid rendering them as
    # labels, since the label definition would also be hidden and the source
    # would no longer compile.
    hidden_pcs: set[int] = set()
    structured_conditional_starts: set[int] = set()

    def compute_hidden_pcs(start_pc: int, end_pc: int) -> None:
        pc = start_pc
        while pc < end_pc:
            if _is_push16_at(pc):
                pc += 2
                continue
            word = form.data[pc]
            flags, instr, _, _ = _decode_word(word)

            # Mirror the structured conditional recognition in emit_range.
            if instr == 15 and flags == 1 and pc + 1 < len(form.data) and (pc + 1) in ifnot_operand_pcs:
                op_word = form.data[pc + 1]
                _, op_instr, op_arg, op_is_long = _decode_word(op_word)

                if op_is_long and op_instr == 0 and op_arg in valid_jump_targets and op_arg > pc and op_arg < end_pc:
                    structured_conditional_starts.add(pc)
                    else_start = op_arg
                    then_start = pc + 2

                    last_then_pc = prev_executed(else_start, then_start)
                    join_target: int | None = None
                    if last_then_pc is not None:
                        last_word = form.data[last_then_pc]
                        last_flags, last_instr, last_arg, last_is_long = _decode_word(last_word)
                        if last_is_long and last_instr == 0 and last_arg in valid_jump_targets and last_arg > else_start and last_arg <= end_pc:
                            join_target = last_arg

                    if join_target is not None and last_then_pc is not None:
                        # Hide the join-jump instruction itself.
                        hidden_pcs.add(last_then_pc)
                        compute_hidden_pcs(then_start, last_then_pc)
                        compute_hidden_pcs(else_start, join_target)
                        pc = join_target
                        continue

                    # `if` without `else`.
                    compute_hidden_pcs(then_start, else_start)
                    pc = else_start
                    continue

                pc += 2
                continue

            pc += 1

    def _collect_speech_text_indices() -> set[int]:
        """Collect TEXT indices used by speech-related native calls.

        Heuristics (matches shipped scripts):
            - speak: call_1(a, b, text_id) => call(2,1)  instr_12(2,3) with last push as i8.
            - tell:  call_52(a, b, c, text_id) => call(2,52) instr_12(2,4) with last push as i8.

                Note:
                - Native call 0x40 (decimal 64) is widely used near dialogue logic, but runtime
                    inspection shows it is not a text output primitive (does not read TEXT / call
                    sub_1D216). Therefore we do not treat its arguments as speech text indices for
                    slugging.
        """

        if not strings:
            return set()

        call_id = _NAME_TO_INSTR_ID.get('call')
        if call_id is None:
            return set()

        used: set[int] = set()
        pc = 0
        while pc < len(form.data) - 1:
            if pc in ifnot_operand_pcs:
                pc += 1
                continue

            f0, i0, a0, l0 = _decode_word(form.data[pc])
            if l0 or i0 != call_id or f0 != 2:
                pc += 1
                continue

            f1, i1, nargs, l1 = _decode_word(form.data[pc + 1])
            if l1 or i1 != 12 or f1 != 2:
                pc += 1
                continue

            call_target = a0

            def is_i8_push_at(p: int) -> bool:
                if p < 0 or p >= len(form.data):
                    return False
                if _is_push16_at(p):
                    return False
                ff, ii, _, ll = _decode_word(form.data[p])
                return (not ll) and ff == 2 and ii in {3, 4}

            def push_arg_at(p: int) -> int | None:
                if not is_i8_push_at(p):
                    return None
                _, _, arg, _ = _decode_word(form.data[p])
                return int(arg)

            if call_target == 1 and nargs == 3:
                idx = push_arg_at(pc - 1)
                if idx is not None and 0 <= idx < len(strings):
                    used.add(idx)

            if call_target == 52 and nargs == 4:
                idx = push_arg_at(pc - 1)
                if idx is not None and 0 <= idx < len(strings):
                    used.add(idx)

            pc += 1

        return used

    def _collect_title_text_indices() -> set[int]:
        """Collect TEXT indices used as location/title captions.

        Heuristics (matches shipped scripts in this repo):
        - title: call_139(u16(0x00b3), text_index)
            Encoded as push16(0x00b3), push(text_index), call(2,139), instr_12(2,2).

        We only treat the pattern with the first argument equal to u16(0x00b3)
        as a "caption from this script's TEXT". Other uses of call 139 exist.
        """

        if not strings:
            return set()

        call_id = _NAME_TO_INSTR_ID.get('call')
        if call_id is None:
            return set()

        used: set[int] = set()
        pc = 0
        while pc < len(form.data) - 1:
            if pc in ifnot_operand_pcs:
                pc += 1
                continue

            f0, i0, a0, l0 = _decode_word(form.data[pc])
            if l0 or i0 != call_id or f0 != 2:
                pc += 1
                continue

            f1, i1, nargs, l1 = _decode_word(form.data[pc + 1])
            if l1 or i1 != 12 or f1 != 2:
                pc += 1
                continue

            call_target = a0

            if call_target == 139 and nargs == 2:
                # Expect: push16(0x00b3), push(i8), call, instr_12
                # Note: push16 is 2 words, so it typically starts at pc-3.
                base: int | None = None
                if (pc - 3) >= 0 and _is_push16_at(pc - 3):
                    base = _push16_imm_at(pc - 3)
                elif (pc - 2) >= 0 and _is_push16_at(pc - 2):
                    # Fallback for odd encodings.
                    base = _push16_imm_at(pc - 2)

                if base == 0x00B3 and (pc - 1) >= 0:
                    if not _is_push16_at(pc - 1):
                        ff, ii, arg, ll = _decode_word(form.data[pc - 1])
                        if (not ll) and ff == 2 and ii in {3, 4}:
                            idx = int(arg)
                            if 0 <= idx < len(strings):
                                used.add(idx)

            pc += 1

        return used

    def _compute_text_keys() -> list[str]:
        used_speech = _collect_speech_text_indices()
        used_title = _collect_title_text_indices()
        keys: list[str] = [f's{i:03d}' for i in range(len(strings))]
        if not used_speech and not used_title:
            return keys

        stop = {
            'a', 'an', 'and', 'are', 'as', 'at', 'be', 'but', 'by', 'for', 'from', 'has', 'have', 'he', 'her',
            'his', 'i', 'in', 'is', 'it', 'its', 'me', 'my', 'no', 'not', 'of', 'on', 'or', 'our', 'she', 'so',
            'that', 'the', 'their', 'then', 'there', 'they', 'this', 'to', 'up', 'was', 'we', 'were', 'what',
            'when', 'where', 'who', 'why', 'will', 'with', 'you', 'your',

            # Common contraction spellings after apostrophe removal.
            'theres', 'thats', 'whats', 'heres', 'wheres', 'whos', 'hows', 'lets',
        }

        def words_for_slug(s: str) -> list[str]:
            s = s.strip()
            # Merge common apostrophe contractions/possessives into one token.
            # Examples: "don't" -> "dont", "can't" -> "cant", "Grandfather's" -> "grandfathers".
            s = re.sub(r"(?<=\w)[\u2019'](?=\w)", "", s)
            s_norm = unicodedata.normalize('NFKD', s)
            s_ascii = s_norm.encode('ascii', 'ignore').decode('ascii')
            parts = re.findall(r'[A-Za-z0-9]+', s_ascii.lower())
            if not parts:
                return []
            good = [p for p in parts if p not in stop and len(p) > 1]
            return good if len(good) >= 3 else parts

        used_names: set[str] = set(keys)

        # Prefer a stable semantic name for the common "location caption" entry.
        # Only override default numeric keys (sNNN), to avoid clobbering more
        # descriptive speech-based slugs.
        for i in sorted(used_title):
            if re.fullmatch(r's\d{3}', keys[i]):
                base = 's_title'
                cand = base
                k = 2
                while cand in used_names:
                    cand = f'{base}_{k}'
                    k += 1
                keys[i] = cand
                used_names.add(cand)

        for i in sorted(used_speech):
            w = words_for_slug(strings[i])
            if not w:
                continue
            base = 's_' + '_'.join(w[:4])
            base = re.sub(r'[^A-Za-z0-9_]', '_', base)
            if not (base[0].isalpha() or base[0] == '_'):
                base = 's_' + base
            cand = base
            k = 2
            while cand in used_names:
                cand = f'{base}_{k}'
                k += 1
            keys[i] = cand
            used_names.add(cand)

        return keys

    text_keys = _compute_text_keys()
    text_key_by_index: dict[int, str] = {i: text_keys[i] for i in range(len(text_keys))}

    lines: list[str] = []
    if name:
        lines.append(f'# `{name}`.')
        lines.append('')

    if not strings:
        lines.append('strings = {}')
    else:
        lines.append('strings = {')
        for i, s in enumerate(strings):
            lines.append(f'\t{text_keys[i]}: {_quote_string(s)},')
        lines.append('}')

    lines.append('')

    # ORDR as globals.
    global_labels = [f'global_{i}' for i in range(len(form.order))]
    lines.append(f'globals = [{", ".join(global_labels)}]')
    lines.append('')

    labels_at: dict[int, list[str]] = {}
    preferred_label_at: dict[int, str] = {}
    for i, off in enumerate(form.order):
        name = global_labels[i]
        labels_at.setdefault(off, []).append(name)
        preferred_label_at.setdefault(off, name)

    # Compute hidden PCs after globals are known (in case we ever need to avoid
    # hiding a global entry point in the future).
    compute_hidden_pcs(0, len(form.data))

    # Add labels for jmp targets where the argument looks like a valid executed
    # instruction index (or end-of-data).
    jump_labels: dict[int, str] = {}
    for pc in sorted(executed_pcs):
        word = form.data[pc]
        _, instr, arg, _ = _decode_word(word)
        if instr != 0:
            continue
        if arg not in valid_jump_targets:
            continue
        if arg in preferred_label_at:
            continue
        jump_labels.setdefault(arg, f'label_{arg}')

    # Add labels referenced by embedded conditional-jump targets.
    for arg in sorted(embedded_jmp_targets):
        if arg not in valid_jump_targets:
            continue
        if arg in preferred_label_at:
            continue
        jump_labels.setdefault(arg, f'label_{arg}')

    for pc, label in sorted(jump_labels.items()):
        labels_at.setdefault(pc, []).append(label)
        preferred_label_at.setdefault(pc, label)

    # Scripted function entry points: these are reached via the VM's
    # stackctl-prologue + long-jmp convention. If we print `func_NNN()` call
    # sugar, also emit an explicit entry label `func_NNN` at that PC.
    func_entry_pcs: set[int] = set()
    for pc in range(0, len(form.data) - 1):
        f0, i0, a0, l0 = _decode_word(form.data[pc])
        if l0:
            continue
        if not (f0 == 2 and i0 == 2 and a0 == 1):
            continue
        f1, i1, a1, l1 = _decode_word(form.data[pc + 1])
        if not (i1 == 0 and f1 == 4):
            continue
        if a1 not in valid_jump_targets:
            continue
        func_entry_pcs.add(a1)

    for entry in sorted(func_entry_pcs):
        func_label = f'func_{entry}'
        lst = labels_at.setdefault(entry, [])
        if func_label not in lst:
            lst.append(func_label)

        # If this entry PC only got a synthetic `label_NNN` because it was a
        # jump target, prefer the explicit `func_NNN` label and drop the
        # redundant `label_NNN`. Once call sugar folds the jump, `label_NNN`
        # would otherwise become unreferenced noise.
        pref = preferred_label_at.get(entry)
        if pref is None or pref.startswith('label_'):
            preferred_label_at[entry] = func_label

        auto = f'label_{entry}'
        if auto in lst:
            lst.remove(auto)

    # Some labels exist only because structured `if/else:` hides a conditional
    # operand word (an embedded `jmp(4, target)`). Those targets are real
    # control-flow edges but they are not referenced by any *printed* `jmp(...)`.
    # Emitting a `label label_N` line at the branch entry point makes the label
    # look unused in the source, so we suppress such labels when safe.
    rendered_label_jump_targets: set[int] = set()

    def would_render_jump_target_as_label(arg: int) -> bool:
        return (
            arg in preferred_label_at
            and arg in valid_jump_targets
            and not (arg in hidden_pcs and preferred_label_at[arg].startswith('label_'))
        )

    # Targets referenced by regular `jmp(...)` instructions.
    for pc in sorted(executed_pcs):
        if pc in ifnot_operand_pcs:
            continue
        word = form.data[pc]
        _, instr, arg, _ = _decode_word(word)
        if instr != 0:
            continue
        if would_render_jump_target_as_label(arg):
            rendered_label_jump_targets.add(arg)

    # Targets referenced by operand-jmps in conditionals that fall back to the
    # raw two-word form (i.e. not structured as `if/else:`).
    for pc in sorted(executed_pcs):
        word = form.data[pc]
        flags, instr, _, _ = _decode_word(word)
        if not (instr == 15 and flags == 1 and pc + 1 < len(form.data) and (pc + 1) in ifnot_operand_pcs):
            continue
        if pc in structured_conditional_starts:
            continue
        op_word = form.data[pc + 1]
        _, op_instr, op_arg, op_is_long = _decode_word(op_word)
        if (op_is_long or op_instr == 0) and would_render_jump_target_as_label(op_arg):
            rendered_label_jump_targets.add(op_arg)

    suppressed_label_pcs: set[int] = set()

    def emit_blank_line() -> None:
        # Keep output tidy: avoid consecutive empty lines.
        if lines and lines[-1] != '':
            lines.append('')

    def emit(indent: int, s: str) -> None:
        # Readability: visually separate `else:` / `elif ...:` blocks.
        # (Avoid consecutive blanks via emit_blank_line().)
        if s == 'else:' or (s.startswith('elif ') and s.endswith(':')):
            emit_blank_line()
        lines.append('\t' * indent + s)

    def emit_labels(indent: int, at_pc: int) -> None:
        pc_labels = labels_at.get(at_pc, [])
        if pc_labels:
            pc_labels = sorted(pc_labels, key=lambda s: (0 if s.startswith('global_') else 1, s))

        # Visual separation: blank line before top-level label blocks.
        # (Avoid doing this for nested labels so we don't add a blank line
        # immediately after an `if cond:` header.)
        if indent == 0 and pc_labels:
            emit_blank_line()

        for label in pc_labels:
            if at_pc in suppressed_label_pcs and label.startswith('label_'):
                continue
            emit(indent, f'label {label}')

    def emit_instr(indent: int, at_pc: int) -> None:
        word = form.data[at_pc]
        flags, instr, arg, is_long = _decode_word(word)
        name = _INSTR_ID_TO_NAME.get(instr, f'instr_{instr}')

        call_id = _NAME_TO_INSTR_ID.get('call')
        if call_id is not None and instr == call_id and not is_long:
            emit(indent, f'{name}({flags}, {_native_call_symbol(arg)})')
            return

        if (
            instr == 0
            and arg in preferred_label_at
            and arg in valid_jump_targets
            and not (arg in hidden_pcs and preferred_label_at[arg].startswith('label_'))
        ):
            emit(indent, f'{name}({flags}, {preferred_label_at[arg]})')
        else:
            if instr == 0 and is_long and arg > 0xFF:
                emit(indent, f'{name}({flags}, 0x{arg:04x})')
            else:
                emit(indent, f'{name}({flags}, 0x{arg:02x})')

    # --- Stack-expression decoding for call/function sugar ---
    @dataclass(frozen=True)
    class _E:
        kind: str
        a: object | None = None
        b: object | None = None

    _BIN_ID_TO_NAME = {
        0: 'and',
        1: 'or',
        2: 'eq',
        3: 'ne',
        4: 'lt',
        5: 'le',
        6: 'gt',
        7: 'ge',
        8: 'add',
        9: 'sub',
        10: 'mul',
        11: 'div',
        12: 'shr',
        13: 'shl',
        14: 'band',
        15: 'bor',
        16: 'mod',
        17: 'xor',
    }
    _UN_ID_TO_NAME = {
        0: 'not',
        1: 'neg',
        2: 'bnot',
    }

    def _fmt_i8(x: int) -> str:
        return f'0x{x:02x}'

    def _fmt_u16(x: int) -> str:
        return f'0x{x:04x}'

    def render_expr(e: _E) -> str:
        if e.kind == 'i8':
            return _fmt_i8(int(e.a))
        if e.kind == 'u16':
            return f'u16({_fmt_u16(int(e.a))})'
        if e.kind == 'var':
            return f'var({_fmt_i8(int(e.a))})'
        if e.kind == 'arg':
            return f'arg({_fmt_i8(int(e.a))})'
        if e.kind == 'local':
            return f'local({_fmt_i8(int(e.a))})'
        if e.kind == 'acc':
            return 'acc'
        if e.kind.startswith('un:'):
            nm = e.kind.split(':', 1)[1]
            return f'{nm}({render_expr(e.a)})'
        if e.kind.startswith('bin:'):
            nm = e.kind.split(':', 1)[1]
            return f'{nm}({render_expr(e.a)}, {render_expr(e.b)})'
        return e.kind

    def render_call_args(call_target: int, exprs: list[_E]) -> str:
        rendered = [render_expr(e) for e in exprs]

        # speak: speak(a, b, text_id)
        if call_target == 1 and exprs:
            e = exprs[-1]
            if e.kind == 'i8':
                idx = int(e.a)
                if idx in text_key_by_index:
                    rendered[-1] = text_key_by_index[idx]

        # tell: tell(a, b, c, text_id)
        if call_target == 52 and exprs:
            e = exprs[-1]
            if e.kind == 'i8':
                idx = int(e.a)
                if idx in text_key_by_index:
                    rendered[-1] = text_key_by_index[idx]

        return ", ".join(rendered)

    def parse_stack_exprs(seq_start: int, seq_end: int) -> list[_E] | None:
        """Parse a contiguous stack-building sequence into value expressions.

        Only accepts the safe subset used by the VM expression stack (pushes,
        loads, acc, unary, binary, push16). Returns the final stack.
        """

        stack: list[_E] = []
        pc = seq_start
        while pc < seq_end:
            if pc in ifnot_operand_pcs:
                return None
            if pc != seq_start and labels_at.get(pc):
                return None

            if _is_push16_at(pc):
                stack.append(_E('u16', _push16_imm_at(pc)))
                pc += 2
                continue

            flags, instr, arg, is_long = _decode_word(form.data[pc])
            if is_long:
                return None

            if instr in {3, 4} and flags == 2:
                stack.append(_E('i8', arg))
                pc += 1
                continue
            if instr == 5 and flags == 2:
                stack.append(_E('var', arg))
                pc += 1
                continue
            if instr == 6 and flags == 2:
                stack.append(_E('arg', arg))
                pc += 1
                continue
            if instr == 7 and flags == 2:
                stack.append(_E('local', arg))
                pc += 1
                continue
            if instr == 2 and flags == 2 and arg == 0:
                stack.append(_E('acc'))
                pc += 1
                continue
            if instr == 16 and flags == 2:
                if not stack:
                    return None
                x = stack.pop()
                nm = _UN_ID_TO_NAME.get(arg)
                if nm is None:
                    return None
                stack.append(_E(f'un:{nm}', x))
                pc += 1
                continue
            if instr == 17 and flags == 2:
                if len(stack) < 2:
                    return None
                b = stack.pop()
                a = stack.pop()
                nm = _BIN_ID_TO_NAME.get(arg)
                if nm is None:
                    return None
                stack.append(_E(f'bin:{nm}', a, b))
                pc += 1
                continue

            return None

        return stack

    # Note: for scripted calls (stackctl+long-jmp), the most reliable place to
    # infer argument count is the canonical caller-side cleanup `sp_add(N)` at
    # the return address (the word immediately following the jmp).

    def try_emit_call_sugar(indent: int, start_pc: int, end_pc: int) -> int | None:
        """Fold stack-building expressions + `call(2, id) instr_12(2, N)` into `call_ID(expr, ...)`.

        Uses VM stack semantics (KYRA.md) so only real value expressions become
        call arguments.
        """

        call_id = _NAME_TO_INSTR_ID.get('call')
        if call_id is None:
            return None

        for call_pc in range(start_pc, end_pc - 1):
            if call_pc in ifnot_operand_pcs:
                return None
            if call_pc != start_pc and labels_at.get(call_pc):
                return None

            flags, instr, call_target, is_long = _decode_word(form.data[call_pc])
            if is_long:
                continue
            if not (instr == call_id and flags == 2):
                continue

            cleanup_pc = call_pc + 1
            if cleanup_pc >= end_pc:
                continue
            if cleanup_pc in ifnot_operand_pcs:
                continue
            if labels_at.get(cleanup_pc):
                continue

            c_flags, c_instr, nargs, c_is_long = _decode_word(form.data[cleanup_pc])
            if c_is_long:
                continue
            if not (c_instr == 12 and c_flags == 2):
                continue

            exprs = parse_stack_exprs(start_pc, call_pc)
            if exprs is None or len(exprs) != nargs:
                continue

            rendered_args = render_call_args(call_target, exprs)
            call_name = _native_call_symbol(call_target)
            if rendered_args:
                emit(indent, f'{call_name}({rendered_args})')
            else:
                emit(indent, f'{call_name}()')
            return cleanup_pc + 1

        return None

    def try_emit_return_call_sugar(indent: int, start_pc: int, end_pc: int) -> int | None:
        """Fold `call_NNN(...)` + bare return into `return call_NNN(...)`.

        Pattern:
            <stack exprs...>
            call(2, id)
            instr_12(2, nargs)
            instr_8(2, 1)

        This is the common idiom where a native call produces the return value
        in the accumulator and the function immediately returns it.
        """

        call_id = _NAME_TO_INSTR_ID.get('call')
        if call_id is None:
            return None

        # Scan within the provided slice. Some callers (structured `if` emit)
        # pass `end_pc` that ends exactly at the return, so we must allow
        # matching 2-word epilogues (`call ; ret`) as well.
        for call_pc in range(start_pc, end_pc - 1):
            if call_pc in ifnot_operand_pcs:
                return None
            if call_pc != start_pc and labels_at.get(call_pc):
                return None

            flags, instr, call_target, is_long = _decode_word(form.data[call_pc])
            if is_long:
                continue
            if not (instr == call_id and flags == 2):
                continue

            # Two encodings exist in the shipped scripts:
            #   A) call ; sp_add(nargs) ; ret
            #   B) call ; ret                (nargs == 0, no cleanup word)
            next_pc = call_pc + 1
            if next_pc >= end_pc:
                continue
            if next_pc in ifnot_operand_pcs:
                continue
            if labels_at.get(next_pc):
                continue

            n_flags, n_instr, n_arg, n_is_long = _decode_word(form.data[next_pc])
            if n_is_long:
                continue

            nargs = None
            ret_pc = None

            # Pattern B: call ; ret
            if n_instr == 8 and n_flags == 2 and n_arg == 1:
                nargs = 0
                ret_pc = next_pc

            # Pattern A: call ; sp_add(nargs) ; ret
            if nargs is None and n_instr == 12 and n_flags == 2:
                cleanup_pc = next_pc
                ret_pc2 = cleanup_pc + 1
                if ret_pc2 >= end_pc:
                    continue
                if ret_pc2 in ifnot_operand_pcs:
                    continue
                if labels_at.get(ret_pc2):
                    continue
                r_flags, r_instr, r_arg, r_is_long = _decode_word(form.data[ret_pc2])
                if r_is_long:
                    continue
                if not (r_instr == 8 and r_flags == 2 and r_arg == 1):
                    continue
                nargs = n_arg
                ret_pc = ret_pc2

            if nargs is None or ret_pc is None:
                continue

            exprs = parse_stack_exprs(start_pc, call_pc)
            if exprs is None or len(exprs) != nargs:
                continue

            rendered_args = render_call_args(call_target, exprs)
            call_name = _native_call_symbol(call_target)
            if rendered_args:
                emit(indent, f'return {call_name}({rendered_args})')
            else:
                emit(indent, f'return {call_name}()')
            return ret_pc + 1

        return None

    def try_emit_return_func_sugar(indent: int, start_pc: int, end_pc: int) -> int | None:
        """Fold `func_ENTRY(...)` + bare return into `return func_ENTRY(...)`.

        Pattern:
            <stack exprs...>
            instr_2(2, 1)
            jmp(4, entry)
            [optional instr_12(2, nargs)]
            instr_8(2, 1)

        This represents returning the accumulator value produced by a scripted
        function call.
        """

        for pro_pc in range(start_pc, end_pc - 2):
            if pro_pc in ifnot_operand_pcs:
                return None
            if pro_pc != start_pc and labels_at.get(pro_pc):
                return None

            if _is_push16_at(pro_pc):
                continue

            f0, i0, a0, l0 = _decode_word(form.data[pro_pc])
            if l0:
                continue
            if not (f0 == 2 and i0 == 2 and a0 == 1):
                continue

            jpc = pro_pc + 1
            if jpc >= end_pc:
                continue
            if jpc in ifnot_operand_pcs:
                continue
            if labels_at.get(jpc):
                continue

            f1, i1, entry, l1 = _decode_word(form.data[jpc])
            if not (l1 and i1 == 0 and entry in valid_jump_targets):
                continue

            # Optional canonical cleanup at the return address.
            nargs = 0
            next_pc = jpc + 1
            cleanup_pc = jpc + 1
            if cleanup_pc < end_pc and cleanup_pc not in ifnot_operand_pcs and not labels_at.get(cleanup_pc):
                c_flags, c_instr, c_arg, c_is_long = _decode_word(form.data[cleanup_pc])
                if (not c_is_long) and c_flags == 2 and c_instr == 12:
                    nargs = c_arg
                    next_pc = cleanup_pc + 1

            if next_pc >= end_pc:
                continue
            if next_pc in ifnot_operand_pcs:
                continue
            if labels_at.get(next_pc):
                continue

            r_flags, r_instr, r_arg, r_is_long = _decode_word(form.data[next_pc])
            if r_is_long:
                continue
            if not (r_instr == 8 and r_flags == 2 and r_arg == 1):
                continue

            exprs = parse_stack_exprs(start_pc, pro_pc)
            if exprs is None or len(exprs) != nargs:
                continue

            rendered_args = ", ".join(render_expr(e) for e in exprs)
            if rendered_args:
                emit(indent, f'return func_{entry}({rendered_args})')
            else:
                emit(indent, f'return func_{entry}()')
            return next_pc + 1

        return None

    def try_emit_func_sugar(indent: int, start_pc: int, end_pc: int) -> int | None:
        """Fold stack-building expressions + `instr_2(2,1) jmp(4, entry)` into `func_ENTRY(expr, ...)`.

        This is the VM's scripted-call convention (stackctl prologue + long-jump).
        Arg count is inferred from callee `load_arg`/`store_arg` usage.
        """

        for pro_pc in range(start_pc, end_pc - 1):
            if pro_pc in ifnot_operand_pcs:
                return None
            if pro_pc != start_pc and labels_at.get(pro_pc):
                return None

            if _is_push16_at(pro_pc):
                continue

            f0, i0, a0, l0 = _decode_word(form.data[pro_pc])
            if l0:
                continue
            if not (f0 == 2 and i0 == 2 and a0 == 1):
                continue

            jpc = pro_pc + 1
            if jpc >= end_pc:
                continue
            if jpc in ifnot_operand_pcs:
                continue
            if labels_at.get(jpc):
                continue

            f1, i1, entry, l1 = _decode_word(form.data[jpc])
            if not (l1 and i1 == 0 and entry in valid_jump_targets):
                continue

            # Optional canonical cleanup at the return address.
            nargs = 0
            next_pc = jpc + 1
            cleanup_pc = jpc + 1
            if cleanup_pc < end_pc and cleanup_pc not in ifnot_operand_pcs and not labels_at.get(cleanup_pc):
                c_flags, c_instr, c_arg, c_is_long = _decode_word(form.data[cleanup_pc])
                if (not c_is_long) and c_flags == 2 and c_instr == 12:
                    nargs = c_arg
                    next_pc = cleanup_pc + 1

            exprs = parse_stack_exprs(start_pc, pro_pc)
            if exprs is None or len(exprs) != nargs:
                continue

            emit(indent, f'func_{entry}({", ".join(render_expr(e) for e in exprs)})')
            return next_pc

        return None

    def try_emit_return_sugar(indent: int, start_pc: int, end_pc: int) -> int | None:
        """Fold a pure stack-expression followed by `instr_8(2,0) instr_8(2,1)` into `return <expr>`.

        In this VM, `instr_8(2,0)` pops into the accumulator and `instr_8(2,1)` returns.
        This sugar is only applied when the entire prefix is a safe, side-effect-free
        stack-expression sequence (push/load/acc/unary/binary/push16) that leaves exactly
        one value on the expression stack.
        """

        pc = start_pc
        while pc + 1 < end_pc:
            if pc in ifnot_operand_pcs:
                return None
            if pc != start_pc and labels_at.get(pc):
                return None

            if _is_push16_at(pc):
                pc += 2
                continue

            flags0, instr0, arg0, is_long0 = _decode_word(form.data[pc])
            if is_long0:
                return None

            if instr0 == 8 and flags0 == 2 and arg0 == 0:
                next_pc = pc + 1
                if next_pc in ifnot_operand_pcs:
                    return None
                if labels_at.get(next_pc):
                    return None
                flags1, instr1, arg1, is_long1 = _decode_word(form.data[next_pc])

                # Pattern A: pop->acc ; ret
                if (not is_long1) and instr1 == 8 and flags1 == 2 and arg1 == 1:
                    exprs = parse_stack_exprs(start_pc, pc)
                    if exprs is None or len(exprs) != 1:
                        return None
                    if exprs[0].kind == 'acc':
                        return None
                    emit(indent, f'return {render_expr(exprs[0])}')
                    return next_pc + 1

                # Pattern B: pop->acc ; sp_add(N) ; ret
                if (not is_long1) and instr1 == 12 and flags1 == 2:
                    ret_pc = next_pc + 1
                    if ret_pc >= end_pc:
                        return None
                    if ret_pc in ifnot_operand_pcs:
                        return None
                    if labels_at.get(ret_pc):
                        return None
                    flags2, instr2, arg2, is_long2 = _decode_word(form.data[ret_pc])
                    if (not is_long2) and instr2 == 8 and flags2 == 2 and arg2 == 1:
                        exprs = parse_stack_exprs(start_pc, pc)
                        if exprs is None or len(exprs) != 1:
                            return None
                        if exprs[0].kind == 'acc':
                            return None
                        emit(indent, f'leave 0x{arg1:02x}')
                        emit(indent, f'return {render_expr(exprs[0])}')
                        return ret_pc + 1

            pc += 1

        return None

    def try_emit_return_acc_sugar(indent: int, start_pc: int, end_pc: int) -> int | None:
        """Fold a bare `instr_8(2,1)` into `return acc()`.

        This represents the common epilogue case where the function returns the
        current accumulator without an explicit pop-to-acc right before it.
        """

        if start_pc >= end_pc:
            return None
        if start_pc in ifnot_operand_pcs:
            return None

        flags, instr, arg, is_long = _decode_word(form.data[start_pc])
        if is_long:
            return None

        # Pattern: sp_add(N) ; ret  => leave N ; return acc
        if instr == 12 and flags == 2:
            ret_pc = start_pc + 1
            if ret_pc >= end_pc:
                return None
            if ret_pc in ifnot_operand_pcs:
                return None
            if labels_at.get(ret_pc):
                return None
            f2, i2, a2, l2 = _decode_word(form.data[ret_pc])
            if (not l2) and i2 == 8 and f2 == 2 and a2 == 1:
                emit(indent, f'leave 0x{arg:02x}')
                emit(indent, 'return acc')
                return ret_pc + 1

        if instr == 8 and flags == 2 and arg == 1:
            # Try to recover a concrete expression for the accumulator.
            # This is conservative and only looks within the current straight-line
            # block (no labels / ifnot operands in between).
            def infer_acc_expr() -> str | None:
                call_id = _NAME_TO_INSTR_ID.get('call')
                if call_id is None:
                    return None

                MAX_BACK_SCAN = 96

                # Find a conservative basic-block start.
                block_start = 0
                for p in range(start_pc - 1, -1, -1):
                    if p in ifnot_operand_pcs:
                        # Non-executed immediate word for an `ifnot`/`jz`; do not
                        # treat as a control-flow boundary for accumulator tracking.
                        continue
                    if labels_at.get(p):
                        block_start = p + 1
                        break

                def acc_overwritten_between(write_pc: int) -> bool:
                    """Return True if `acc` might be overwritten between write_pc and start_pc."""
                    ok = True
                    for p2 in range(write_pc + 1, start_pc):
                        if p2 in ifnot_operand_pcs:
                            continue
                        if labels_at.get(p2):
                            ok = False
                            break
                        ff, ii, aa, ll = _decode_word(form.data[p2])
                        if ll:
                            continue
                        # acc writers: set_acc (1), native call, pop->acc (instr_8(2,0))
                        if ii == 1:
                            ok = False
                            break
                        if ii == call_id and ff == 2 and p2 != write_pc:
                            ok = False
                            break
                        if ii == 8 and ff == 2 and aa == 0:
                            ok = False
                            break
                    return not ok

                def try_parse_single_stack_expr(end_at_pc: int) -> _Expr | None:
                    search_start = max(block_start, end_at_pc - MAX_BACK_SCAN)
                    for s in range(end_at_pc - 1, search_start - 1, -1):
                        if s in ifnot_operand_pcs:
                            continue
                        if labels_at.get(s):
                            break
                        if s > 0 and _is_push16_at(s - 1):
                            continue
                        exprs = parse_stack_exprs(s, end_at_pc)
                        if exprs is not None and len(exprs) == 1:
                            return exprs[0]
                    return None

                # Prefer the most recent explicit pop-to-acc expression.
                for pc0 in range(start_pc - 1, block_start - 1, -1):
                    if pc0 in ifnot_operand_pcs:
                        continue
                    if labels_at.get(pc0):
                        break
                    f0, i0, a0, l0 = _decode_word(form.data[pc0])
                    if l0:
                        continue
                    if i0 == 8 and f0 == 2 and a0 == 0:
                        if acc_overwritten_between(pc0):
                            continue
                        expr = try_parse_single_stack_expr(pc0)
                        if expr is None:
                            continue
                        return render_expr(expr)

                # Next prefer a direct `set_acc` immediate.
                for pc1 in range(start_pc - 1, block_start - 1, -1):
                    if pc1 in ifnot_operand_pcs:
                        continue
                    if labels_at.get(pc1):
                        break
                    f1, i1, a1, l1 = _decode_word(form.data[pc1])
                    if l1:
                        continue
                    if i1 == 1 and isinstance(a1, int):
                        if acc_overwritten_between(pc1):
                            continue
                        return f'0x{a1:02x}'

                # Walk backwards to find the most recent native call that sets `acc`.
                for call_pc in range(start_pc - 1, block_start - 1, -1):
                    if call_pc in ifnot_operand_pcs:
                        continue
                    if labels_at.get(call_pc):
                        break

                    f, i, call_target, is_long_call = _decode_word(form.data[call_pc])
                    if is_long_call:
                        continue
                    if not (i == call_id and f == 2):
                        continue

                    # Optional canonical stack fix after a call: instr_12(2, nargs)
                    nargs: int | None = None
                    cleanup_pc = call_pc + 1
                    if cleanup_pc < start_pc and cleanup_pc not in ifnot_operand_pcs and not labels_at.get(cleanup_pc):
                        cf, ci, n, is_long_cleanup = _decode_word(form.data[cleanup_pc])
                        if (not is_long_cleanup) and ci == 12 and cf == 2:
                            nargs = n

                    if acc_overwritten_between(call_pc):
                        continue

                    # If we know nargs, try to reconstruct args from a *local* pure
                    # expression suffix ending at the call, without requiring the
                    # whole block prefix to be a pure expression stack sequence.
                    if nargs is not None:
                        search_start = max(block_start, call_pc - MAX_BACK_SCAN)
                        best_exprs: list[_Expr] | None = None
                        for s in range(call_pc - 1, search_start - 1, -1):
                            if s in ifnot_operand_pcs:
                                break
                            if labels_at.get(s):
                                break
                            # Don't start in the middle of a push16 immediate word.
                            if s > 0 and _is_push16_at(s - 1):
                                continue
                            exprs = parse_stack_exprs(s, call_pc)
                            if exprs is not None and len(exprs) == nargs:
                                best_exprs = exprs
                                break

                        if best_exprs is not None:
                            rendered_args = ", ".join(render_expr(e) for e in best_exprs)
                            call_name = _native_call_symbol(call_target)
                            return f'{call_name}({rendered_args})' if rendered_args else f'{call_name}()'

                        # Can't reconstruct args safely; still provide a useful hint.
                        return f'{_native_call_symbol(call_target)}(...)'

                    # Unknown arg count; provide a generic hint.
                    return f'{_native_call_symbol(call_target)}(...)'

                return None

            acc_expr = infer_acc_expr()
            if acc_expr is not None:
                emit(indent, f'return acc  # acc = {acc_expr}')
            else:
                emit(indent, 'return acc')
            return start_pc + 1

        return None

    def emit_range(start_pc: int, end_pc: int, indent: int) -> None:
        pc = start_pc
        while pc < end_pc:
            emit_labels(indent, pc)

            if call_sugar:
                # Return sugar: fold expression + pop/ret into `return <expr>`.
                # Must run before the push16 fast-path so sequences that start
                # with push16 can still fold.
                next_pc = try_emit_return_sugar(indent, pc, end_pc)
                if next_pc is not None:
                    pc = next_pc
                    continue

                # Return sugar: return value is the result of a call/func.
                next_pc = try_emit_return_func_sugar(indent, pc, end_pc)
                if next_pc is not None:
                    pc = next_pc
                    continue

                next_pc = try_emit_return_call_sugar(indent, pc, end_pc)
                if next_pc is not None:
                    pc = next_pc
                    continue

                # Return sugar: bare epilogue `instr_8(2,1)` => `return acc()`.
                next_pc = try_emit_return_acc_sugar(indent, pc, end_pc)
                if next_pc is not None:
                    pc = next_pc
                    continue

                # Scripted function call sugar: stackctl-prologue + long-jump.
                # Must run before the push16 fast-path so sequences that start
                # with push16 can still fold.
                next_pc = try_emit_func_sugar(indent, pc, end_pc)
                if next_pc is not None:
                    pc = next_pc
                    continue

                # Call sugar: fold `push... call(2, id) instr_12(2, N)` into `call_NNN(...)`.
                # Must run before the push16 fast-path so sequences that start
                # with push16 can still fold.
                next_pc = try_emit_call_sugar(indent, pc, end_pc)
                if next_pc is not None:
                    pc = next_pc
                    continue

            if _is_push16_at(pc):
                imm = _push16_imm_at(pc)
                emit(indent, f'push16(0x{imm:04x})')
                pc += 2
                continue

            word = form.data[pc]
            flags, instr, _, _ = _decode_word(word)

            # Structured conditional: instr_15(1, 0x00) + embedded `jmp(4, else)`.
            if instr == 15 and flags == 1 and pc + 1 < len(form.data) and (pc + 1) in ifnot_operand_pcs:
                op_word = form.data[pc + 1]
                _, op_instr, op_arg, op_is_long = _decode_word(op_word)

                # Recognize only the canonical pattern used in this repo.
                if op_is_long and op_instr == 0 and op_arg in valid_jump_targets and op_arg > pc and op_arg < end_pc:
                    else_start = op_arg
                    then_start = pc + 2

                    # If the else/join entry point is only targeted by the
                    # embedded conditional operand jump, omit the synthetic
                    # `label label_N` line there.
                    pref = preferred_label_at.get(else_start)
                    if (
                        pref
                        and pref.startswith('label_')
                        and else_start in embedded_jmp_targets
                        and else_start not in rendered_label_jump_targets
                    ):
                        suppressed_label_pcs.add(else_start)

                    # Try to recognize `if/else` by finding a `jmp(4, end)` immediately before `else_start`.
                    last_then_pc = prev_executed(else_start, then_start)
                    join_target: int | None = None
                    if last_then_pc is not None:
                        last_word = form.data[last_then_pc]
                        last_flags, last_instr, last_arg, last_is_long = _decode_word(last_word)
                        if last_is_long and last_instr == 0 and last_arg in valid_jump_targets and last_arg > else_start and last_arg <= end_pc:
                            join_target = last_arg

                    emit(indent, 'if cond:')
                    if join_target is not None and last_then_pc is not None:
                        emit_range(then_start, last_then_pc, indent + 1)
                        emit(indent, 'else:')
                        emit_range(else_start, join_target, indent + 1)
                        pc = join_target
                        emit_blank_line()
                        continue

                    # `if` without `else`.
                    emit_range(then_start, else_start, indent + 1)
                    pc = else_start
                    emit_blank_line()
                    continue

                # Fallback: prefer explicit `ifnot(1, target)` when the operand
                # is a canonical long-jump word.
                if op_is_long and op_instr == 0:
                    if (
                        op_arg in preferred_label_at
                        and op_arg in valid_jump_targets
                        and not (op_arg in hidden_pcs and preferred_label_at[op_arg].startswith('label_'))
                    ):
                        emit(indent, f'ifnot(1, {preferred_label_at[op_arg]})')
                    else:
                        emit(indent, f'ifnot(1, 0x{op_arg:04x})')
                    pc += 2
                    continue

                # Final fallback: emit the raw two-word form.
                emit(indent, 'instr_15(1, 0x00)')
                op_name = _INSTR_ID_TO_NAME.get(op_instr, f'instr_{op_instr}')
                if (
                    op_instr == 0
                    and op_arg in preferred_label_at
                    and op_arg in valid_jump_targets
                    and not (op_arg in hidden_pcs and preferred_label_at[op_arg].startswith('label_'))
                ):
                    emit(indent, f'{op_name}(4, {preferred_label_at[op_arg]})')
                else:
                    emit(indent, f'{op_name}(4, 0x{op_arg:04x})')
                pc += 2
                continue

            emit_instr(indent, pc)
            pc += 1

    emit_range(0, len(form.data), indent=0)

    # Labels that point to the end of DATA (rare, but representable).
    end_labels = labels_at.get(len(form.data), [])
    if end_labels:
        end_labels = sorted(end_labels, key=lambda s: (0 if s.startswith('global_') else 1, s))
    for label in end_labels:
        emit_blank_line()
        lines.append(f'label {label}')

    # Drop unreferenced synthetic jump labels (`label_NNN`). These can remain
    # after we fold structured control flow (e.g. `if/else`) that hides the
    # original jump instruction that referenced the label.
    synthetic_label_def_re = re.compile(r'^(\t*)label\s+(label_\d+)\s*$')
    synthetic_label_ref_re = re.compile(r'\blabel_\d+\b')

    referenced_synthetic_labels: set[str] = set()
    for line in lines:
        if synthetic_label_def_re.match(line):
            continue
        for lbl in synthetic_label_ref_re.findall(line):
            referenced_synthetic_labels.add(lbl)

    if referenced_synthetic_labels is not None:
        filtered: list[str] = []
        for line in lines:
            m = synthetic_label_def_re.match(line)
            if m and m.group(2) not in referenced_synthetic_labels:
                continue
            filtered.append(line)
        lines = filtered

    lines.append('')
    return '\n'.join(lines)


def compile(source: str) -> bytes:
    source = _desugar_structured_control_flow(source)
    parsed = _parse_kyra_source(source)

    strings_dict = parsed.strings
    strings = list(strings_dict.values())
    for s in strings:
        try:
            s.encode('ascii')
        except UnicodeEncodeError as e:
            raise ValueError('`strings` values must be ASCII.') from e

    if parsed.text_present is None:
        # In this repo, "0 strings" is represented as missing TEXT.
        text_present = bool(strings)
    else:
        text_present = parsed.text_present

    form = _Emc2Form(
        order=[_u16(x) for x in parsed.order],
        strings=strings,
        data=[_u16(x) for x in parsed.data],
        text_present=text_present,
    )
    return _emit_emc2(form)


@dataclass(frozen=True)
class _Emc2Form:
    order: list[int]
    strings: list[str]
    data: list[int]
    text_present: bool


def _u16(x: int) -> int:
    if not (0 <= x <= 0xffff):
        raise ValueError(f'Value out of u16 range: {x}.')
    return x


def _quote_string(s: str) -> str:
    # Kyra strings are single-quoted, with minimal escaping.
    out: list[str] = ["'"]
    for ch in s:
        code = ord(ch)
        if ch == "\\":
            out.append('\\\\')
        elif ch == "'":
            out.append("\\'")
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif 32 <= code <= 126:
            out.append(ch)
        else:
            out.append(f'\\x{code:02x}')
    out.append("'")
    return ''.join(out)


def _read_u32be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], 'big')


def _write_u32be(x: int) -> bytes:
    return int(x).to_bytes(4, 'big')


def _write_u16be(x: int) -> bytes:
    return int(x).to_bytes(2, 'big')


def _parse_emc2(data: bytes) -> _Emc2Form:
    if not data.startswith(b'FORM'):
        raise ValueError('Not an EMC2 FORM file (missing FORM header).')

    file_len = _read_u32be(data, 4)
    if file_len != len(data):
        raise ValueError(f'FORM length mismatch: header={file_len}, actual={len(data)}.')

    payload = data[8:]
    if not payload.startswith(b'EMC2'):
        raise ValueError('Not an EMC2 FORM file (missing EMC2 tag).')
    payload = payload[4:]

    order_block: bytes | None = None
    text_block: bytes | None = None
    data_block: bytes | None = None

    i = 0
    while i < len(payload):
        if i + 8 > len(payload):
            raise ValueError('Truncated chunk header.')
        name = payload[i:i + 4]
        size = _read_u32be(payload, i + 4)
        i += 8
        if i + size > len(payload):
            raise ValueError(f'Truncated chunk {name!r}: size={size}.')
        chunk = payload[i:i + size]
        i += size
        if size & 1:
            if i >= len(payload) or payload[i:i + 1] != b'\x00':
                raise ValueError(f'Missing pad byte after odd-sized chunk {name!r}.')
            i += 1

        if name == b'ORDR':
            order_block = chunk
        elif name == b'TEXT':
            text_block = chunk
        elif name == b'DATA':
            data_block = chunk
        else:
            # Unknown chunks are ignored for now. If you hit a file that needs
            # them preserved byte-for-byte, extend Kyra format to include them.
            pass

    if order_block is None or data_block is None:
        raise ValueError('Missing required ORDR or DATA chunk.')

    if len(order_block) % 2 != 0:
        raise ValueError('ORDR size is not a multiple of 2.')
    order = [int.from_bytes(order_block[j:j + 2], 'big') for j in range(0, len(order_block), 2)]

    text_present = text_block is not None
    strings: list[str]
    strings = [] if text_block is None else _decode_text_block(text_block)

    if len(data_block) % 2 != 0:
        raise ValueError('DATA size is not a multiple of 2.')
    words = [int.from_bytes(data_block[j:j + 2], 'big') for j in range(0, len(data_block), 2)]

    return _Emc2Form(order=order, strings=strings, data=words, text_present=text_present)


def _emit_emc2(form: _Emc2Form) -> bytes:
    order_bytes = b''.join(_write_u16be(_u16(x)) for x in form.order)
    chunks = [
        _emit_chunk(b'ORDR', order_bytes),
    ]

    if form.text_present:
        text_bytes = _encode_text_block(form.strings)
        chunks.append(_emit_chunk(b'TEXT', text_bytes))

    data_bytes = b''.join(_write_u16be(_u16(w)) for w in form.data)
    chunks.append(_emit_chunk(b'DATA', data_bytes))

    payload = b'EMC2' + b''.join(chunks)
    total_len = 8 + len(payload)
    return b'FORM' + _write_u32be(total_len) + payload


def _emit_chunk(name: bytes, data: bytes) -> bytes:
    if len(name) != 4:
        raise ValueError('Chunk name must be 4 bytes.')
    out = bytearray()
    out += name
    out += _write_u32be(len(data))
    out += data
    if len(data) & 1:
        out += b'\x00'
    return bytes(out)


def _decode_text_block(data: bytes) -> list[str]:
    # Follows the logic in tools/conversation.py, but returns a list.
    i = 0
    offsets: list[int] = []
    while i < len(data):
        if i + 2 > len(data):
            raise ValueError('TEXT offset table truncated.')
        offsets.append(int.from_bytes(data[i:i + 2], 'big'))
        i += 2
        if offsets[0] <= i:
            if offsets[0] != i:
                raise ValueError('TEXT offset table malformed (first offset mismatch).')
            break

    if not offsets and len(data) != 0:
        raise ValueError('TEXT offset table missing.')

    offsets.append(len(data))
    if offsets != sorted(offsets):
        raise ValueError('TEXT offsets not sorted.')

    strings: list[str] = []
    for j in range(len(offsets) - 1):
        s_bytes = data[offsets[j]:offsets[j + 1]]
        try:
            s = s_bytes.decode('ascii')
        except UnicodeDecodeError as e:
            raise ValueError('TEXT contains non-ascii bytes.') from e
        if not s.endswith('\x00'):
            raise ValueError('TEXT string is not NUL-terminated.')
        strings.append(s[:-1])
    return strings


def _encode_text_block(strings: list[str]) -> bytes:
    for s in strings:
        if '\x00' in s:
            raise ValueError('TEXT strings must not contain NUL.')
        try:
            s.encode('ascii')
        except UnicodeEncodeError as e:
            raise ValueError('TEXT strings must be ASCII.') from e

    n = len(strings)
    offsets: list[int] = []
    cursor = 2 * n
    for s in strings:
        offsets.append(cursor)
        cursor += len(s.encode('ascii')) + 1

    out = bytearray()
    for off in offsets:
        out += _write_u16be(_u16(off))
    for s in strings:
        out += s.encode('ascii') + b'\x00'
    return bytes(out)


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


class _Lexer:
    def __init__(self, text: str):
        self._text = text
        self._i = 0

    def tokens(self) -> Iterator[_Token]:
        while True:
            self._skip_ws_and_comments()
            if self._i >= len(self._text):
                yield _Token('EOF', '')
                return

            ch = self._text[self._i]
            if ch.isalpha() or ch == '_':
                start = self._i
                self._i += 1
                while self._i < len(self._text) and (self._text[self._i].isalnum() or self._text[self._i] == '_'):
                    self._i += 1
                yield _Token('IDENT', self._text[start:self._i])
                continue

            if ch.isdigit() or (ch == '-' and self._i + 1 < len(self._text) and self._text[self._i + 1].isdigit()):
                yield _Token('NUMBER', self._read_number())
                continue

            if ch == "'":
                yield _Token('STRING', self._read_single_quoted_string())
                continue

            if ch in '[](),={}':
                self._i += 1
                yield _Token(ch, ch)
                continue

            if ch == ':':
                self._i += 1
                yield _Token(':', ':')
                continue

            raise ValueError(f'Unexpected character: {ch!r}.')

    def _skip_ws_and_comments(self) -> None:
        while self._i < len(self._text):
            ch = self._text[self._i]
            if ch in ' \t\r\n':
                self._i += 1
                continue
            if ch == '#':
                nl = self._text.find('\n', self._i)
                self._i = len(self._text) if nl == -1 else nl + 1
                continue
            break

    def _read_number(self) -> str:
        start = self._i
        if self._text[self._i] == '-':
            self._i += 1
        if self._i + 1 < len(self._text) and self._text[self._i] == '0' and self._text[self._i + 1] in {'x', 'X'}:
            self._i += 2
            while self._i < len(self._text) and self._text[self._i] in '0123456789abcdefABCDEF':
                self._i += 1
        else:
            while self._i < len(self._text) and self._text[self._i].isdigit():
                self._i += 1
        return self._text[start:self._i]

    def _read_single_quoted_string(self) -> str:
        assert self._text[self._i] == "'"
        self._i += 1
        out: list[str] = []
        while True:
            if self._i >= len(self._text):
                raise ValueError('Unterminated string literal.')
            ch = self._text[self._i]
            self._i += 1
            if ch == "'":
                return ''.join(out)
            if ch != '\\':
                out.append(ch)
                continue
            if self._i >= len(self._text):
                raise ValueError('Unterminated escape sequence.')
            esc = self._text[self._i]
            self._i += 1
            if esc == 'n':
                out.append('\n')
            elif esc == 'r':
                out.append('\r')
            elif esc == 't':
                out.append('\t')
            elif esc == "'":
                out.append("'")
            elif esc == '\\':
                out.append('\\')
            elif esc == 'x':
                if self._i + 2 > len(self._text):
                    raise ValueError('Invalid \\x escape.')
                hx = self._text[self._i:self._i + 2]
                self._i += 2
                try:
                    out.append(chr(int(hx, 16)))
                except ValueError as e:
                    raise ValueError('Invalid \\x escape.') from e
            else:
                raise ValueError(f'Unknown escape sequence: \\{esc}.')


@dataclass(frozen=True)
class _KyraProgram:
    strings: dict[str, str]
    text_present: bool | None
    order: list[int]
    data: list[int]


@dataclass(frozen=True)
class _InstrExpr:
    name: str
    args: list[object]


class _Parser:
    def __init__(self, tokens: Iterator[_Token]):
        self._tokens = iter(tokens)
        self._lookahead = next(self._tokens)

    def parse_program(self, text_present: bool | None) -> _KyraProgram:
        strings = self._parse_strings_assignment()
        string_key_to_index: dict[str, int] = {k: i for i, k in enumerate(strings.keys())}

        # Optional: `globals = [global_0, global_1, ...]`.
        # Legacy alias: `entries = [...]`.
        pending_globals: list[str | int] | None = None
        if self._lookahead.kind == 'IDENT' and self._lookahead.value in {'globals', 'entries'}:
            pending_globals = self._parse_globals_assignment()

        legacy_order: list[int] = []
        pending_instrs: list[tuple[int, int, int | str]] = []
        pending_ifnots: list[tuple[int, int | str]] = []
        labels: dict[str, int] = {}
        pc = 0
        pending_leave_n: int | None = None

        while self._lookahead.kind != 'EOF':
            name = self._consume('IDENT').value

            if pending_leave_n is not None and name != 'return':
                raise ValueError('`leave` must be immediately followed by `return`.')

            # `leave N` (stack cleanup) must be placed immediately before a `return`.
            # It is encoded as instr_12(2,N) inserted right before the `ret`.
            if name == 'leave':
                if self._lookahead.kind != 'NUMBER':
                    raise ValueError(f'`leave` expects a numeric arg, got {self._lookahead.kind}.')
                pending_leave_n = int(self._consume('NUMBER').value, 0)
                if not (0 <= pending_leave_n <= 255):
                    raise ValueError(f'leave arg out of range (0..255): {pending_leave_n}.')
                continue

            # High-level return syntax: `return <value-expr>`.
            # Lowers to: <emit value expr> ; instr_8(2,0) ; instr_8(2,1)
            # Optional cleanup can be expressed as `leave N` immediately before `return`,
            # or via the backward-compatible suffix: `return <value-expr>, drop(N)`.
            if name == 'return':
                push_instr_id = _NAME_TO_INSTR_ID.get('push')
                if push_instr_id is None:
                    raise ValueError('Internal error: missing `push` instruction id.')

                if self._lookahead.kind == 'NUMBER':
                    value: object = int(self._consume('NUMBER').value, 0)
                elif self._lookahead.kind == 'IDENT':
                    ident = self._consume('IDENT').value
                    if self._lookahead.kind == '(':
                        expr_args = self._parse_call_args_any()
                        value = _InstrExpr(name=ident, args=expr_args)
                    else:
                        # Treat `acc` as a value expression even without parentheses.
                        # (Decompiler prints `acc`, not `acc()`).
                        if ident == 'acc':
                            value = _InstrExpr(name='acc', args=[])
                        else:
                            value = ident
                else:
                    raise ValueError(f'`return` expects a value expression, got {self._lookahead.kind}.')

                drop_n = None
                if self._lookahead.kind == ',':
                    self._consume(',')
                    drop_kw = self._consume('IDENT').value
                    if drop_kw != 'drop':
                        raise ValueError(
                            f'Expected `drop(...)` after comma in return, got {drop_kw}.'
                        )
                    self._consume('(')
                    if self._lookahead.kind != 'NUMBER':
                        raise ValueError('drop(...) expects a numeric arg.')
                    drop_n = int(self._consume('NUMBER').value, 0)
                    self._consume(')')
                    if not (0 <= drop_n <= 255):
                        raise ValueError(f'drop(...) arg out of range (0..255): {drop_n}.')

                if pending_leave_n is not None and drop_n is not None:
                    raise ValueError('Use either `leave N` or `, drop(N)` on a return, not both.')
                leave_n = pending_leave_n if pending_leave_n is not None else drop_n
                pending_leave_n = None

                # Special-case: `return acc()` is the compact epilogue form and
                # must compile to a *bare* `instr_8(2,1)` to preserve
                # byte-for-byte round-tripping.
                if (
                    (isinstance(value, _InstrExpr) and value.name == 'acc' and not value.args)
                    or value == 'acc'
                ):
                    if leave_n is not None:
                        pending_instrs.append((12, 2, leave_n))
                        pc += 1
                    pending_instrs.append((8, 2, 1))
                    pc += 1
                    continue

                _bin_name_to_id = {
                    'and': 0,
                    'or': 1,
                    'eq': 2,
                    'ne': 3,
                    'lt': 4,
                    'le': 5,
                    'gt': 6,
                    'ge': 7,
                    'add': 8,
                    'sub': 9,
                    'mul': 10,
                    'div': 11,
                    'shr': 12,
                    'shl': 13,
                    'band': 14,
                    'bor': 15,
                    'mod': 16,
                    'xor': 17,
                }
                _un_name_to_id = {
                    'not': 0,
                    'neg': 1,
                    'bnot': 2,
                }

                def emit_value_expr(x: object) -> None:
                    nonlocal pc

                    if isinstance(x, int):
                        if not (0 <= x <= 255):
                            raise ValueError(
                                f'Immediate value out of range for i8 literal (0..255): {x}. Use u16(0x....) for 16-bit pushes.'
                            )
                        pending_instrs.append((push_instr_id, 2, x))
                        pc += 1
                        return

                    if isinstance(x, str):
                        idx = string_key_to_index.get(x)
                        if idx is None:
                            raise ValueError(f'Unknown string key: {x}.')
                        if not (0 <= idx <= 255):
                            raise ValueError(
                                f'TEXT index out of range for i8 literal (0..255): {idx}. (Key: {x})'
                            )
                        pending_instrs.append((push_instr_id, 2, idx))
                        pc += 1
                        return

                    if not isinstance(x, _InstrExpr):
                        raise ValueError('`return` value must be a number or a value expression.')

                    n = x.name
                    a = x.args

                    if n == 'u16':
                        if len(a) != 1:
                            raise ValueError('u16 expects 1 arg: u16(0x1234) or u16(s_key).')
                        v_src = a[0]
                        if isinstance(v_src, str):
                            # Convenience: allow `u16(s_key)` where `s_key` is a key in
                            # `strings = {...}` whose *string value* is a numeric literal.
                            # This is useful for naming pointer-table selectors and other
                            # constants while keeping source syntax simple.
                            raw = strings.get(v_src)
                            if raw is None:
                                raise ValueError(f'Unknown string key: {v_src}.')
                            try:
                                v = int(raw.strip(), 0)
                            except ValueError as e:
                                raise ValueError(
                                    f'u16({v_src}) expects strings[{v_src!r}] to be a numeric literal, got: {raw!r}.'
                                ) from e
                        elif isinstance(v_src, int):
                            v = v_src
                        else:
                            raise ValueError('u16 expects a numeric arg or a string key: u16(0x1234) or u16(s_key).')
                        if not (0 <= v <= 0xFFFF):
                            raise ValueError(f'u16 value out of range (0..0xffff): {v}.')
                        pending_instrs.append((3, 1, 0))
                        pending_instrs.append((-2, 0, v))
                        pc += 2
                        return

                    if n == 'var':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('var expects 1 numeric arg: var(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'var index out of range (0..255): {idx}.')
                        pending_instrs.append((5, 2, idx))
                        pc += 1
                        return

                    if n == 'arg':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('arg expects 1 numeric arg: arg(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'arg index out of range (0..255): {idx}.')
                        pending_instrs.append((6, 2, idx))
                        pc += 1
                        return

                    if n == 'local':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('local expects 1 numeric arg: local(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'local index out of range (0..255): {idx}.')
                        pending_instrs.append((7, 2, idx))
                        pc += 1
                        return

                    if n == 'acc':
                        if len(a) != 0:
                            raise ValueError('acc expects no args: acc().')
                        pending_instrs.append((2, 2, 0))
                        pc += 1
                        return

                    if n in _un_name_to_id:
                        if len(a) != 1:
                            raise ValueError(f'{n} expects 1 arg.')
                        emit_value_expr(a[0])
                        pending_instrs.append((16, 2, _un_name_to_id[n]))
                        pc += 1
                        return

                    if n in _bin_name_to_id:
                        if len(a) != 2:
                            raise ValueError(f'{n} expects 2 args.')
                        emit_value_expr(a[0])
                        emit_value_expr(a[1])
                        pending_instrs.append((17, 2, _bin_name_to_id[n]))
                        pc += 1
                        return

                    # Back-compat: raw single-word instruction expression: foo(flags, arg)
                    expr_id = _NAME_TO_INSTR_ID.get(n)
                    if expr_id is None:
                        if n.startswith('instr_'):
                            try:
                                expr_id = int(n[len('instr_'):])
                            except ValueError as e:
                                raise ValueError(f'Invalid instruction name: {n}.') from e
                            if not (0 <= expr_id <= 31):
                                raise ValueError(f'Instruction id out of range: {expr_id}.')
                        else:
                            raise ValueError(f'Unknown value expression: {n}(...)')

                    if len(a) != 2:
                        raise ValueError(f'Instruction expression expects 2 args: {n}(flags, arg).')
                    expr_flags, expr_arg = a
                    if not isinstance(expr_flags, int):
                        raise ValueError('Instruction expression flags must be a number.')
                    if not (0 <= expr_flags <= 7):
                        raise ValueError(f'Flags out of range (0..7): {expr_flags}.')
                    if not isinstance(expr_arg, int):
                        raise ValueError('Instruction expression arg must be a number.')
                    if not (0 <= expr_arg <= 255):
                        raise ValueError(f'Arg out of range (0..255): {expr_arg}.')

                    pending_instrs.append((expr_id, expr_flags, expr_arg))
                    pc += 1

                # `return call_NNN(...)` / `return speak(...)` / `return tell(...)` / `return func_ENTRY(...)`:
                # These forms return the current accumulator value produced by
                # a call-like statement sequence (native call or scripted call)
                # without forcing an extra stack-pop into acc.
                if isinstance(value, _InstrExpr):
                    call_target: int | None = None
                    if value.name.startswith('call_'):
                        suffix = value.name[len('call_'):]
                        if suffix.startswith('0x'):
                            try:
                                call_target = int(suffix[2:], 16)
                            except ValueError as e:
                                raise ValueError(f'Invalid call target: {value.name}.') from e
                        else:
                            try:
                                call_target = int(suffix, 10)
                            except ValueError:
                                # Back-compat: allow hex without 0x prefix.
                                try:
                                    call_target = int(suffix, 16)
                                except ValueError as e:
                                    raise ValueError(f'Invalid call target: {value.name}.') from e
                    else:
                        call_target = _NATIVE_CALL_ALIAS_TO_ID.get(value.name)

                    if call_target is not None:
                        call_instr_id = _NAME_TO_INSTR_ID.get('call')
                        if call_instr_id is None:
                            raise ValueError('Internal error: missing `call` instruction id.')

                        if not (0 <= call_target <= 255):
                            raise ValueError(f'Call target out of range (0..255): {call_target}.')

                        for a in value.args:
                            emit_value_expr(a)

                        pending_instrs.append((call_instr_id, 2, call_target))
                        pc += 1

                        nargs = len(value.args)
                        if not (0 <= nargs <= 255):
                            raise ValueError(f'Native call argument count out of range (0..255): {nargs}.')
                        pending_instrs.append((12, 2, nargs))
                        pc += 1

                        if leave_n is not None:
                            pending_instrs.append((12, 2, leave_n))
                            pc += 1

                        pending_instrs.append((8, 2, 1))
                        pc += 1
                        continue

                if isinstance(value, _InstrExpr) and value.name.startswith('func_'):
                    suffix = value.name[len('func_'):]
                    if suffix.startswith('0x'):
                        try:
                            entry_pc = int(suffix[2:], 16)
                        except ValueError as e:
                            raise ValueError(f'Invalid function target: {value.name}.') from e
                    else:
                        try:
                            entry_pc = int(suffix, 10)
                        except ValueError as e:
                            raise ValueError(f'Invalid function target: {value.name}.') from e

                    if not (0 <= entry_pc <= 0x7FFF):
                        raise ValueError(f'Function target out of range (0..0x7fff): {entry_pc}.')

                    for a in value.args:
                        emit_value_expr(a)

                    pending_instrs.append((2, 2, 1))
                    pending_instrs.append((0, 4, entry_pc))
                    pc += 2

                    nargs = len(value.args)
                    if nargs:
                        pending_instrs.append((12, 2, nargs))
                        pc += 1

                    if leave_n is not None:
                        pending_instrs.append((12, 2, leave_n))
                        pc += 1

                    pending_instrs.append((8, 2, 1))
                    pc += 1
                    continue

                emit_value_expr(value)

                # pop into acc + return
                pending_instrs.append((8, 2, 0))
                pc += 1
                if leave_n is not None:
                    pending_instrs.append((12, 2, leave_n))
                    pc += 1
                pending_instrs.append((8, 2, 1))
                pc += 1
                continue

            # New label syntax: `label name`
            if name == 'label':
                label_name = self._consume('IDENT').value
                if label_name in labels:
                    raise ValueError(f'Duplicate label: {label_name}.')
                labels[label_name] = pc
                continue

            # label_name:
            if self._lookahead.kind == ':':
                self._consume(':')
                if name in labels:
                    raise ValueError(f'Duplicate label: {name}.')
                labels[name] = pc
                continue

            # Disallow assignments other than the initial strings/globals.
            if self._lookahead.kind == '=':
                raise ValueError('Only `strings = {...}` and optional `globals = [...]` are allowed.')

            args = self._parse_call_args_any()

            def _parse_native_func_symbol(sym: str) -> int | None:
                # Preferred aliases (e.g. speak/tell) for selected native calls.
                alias = _NATIVE_CALL_ALIAS_TO_ID.get(sym)
                if alias is not None:
                    return alias

                # Canonical native call target symbol: call_NNN (decimal).
                # Legacy accepted: func_NNN and hex suffixes.
                if sym.startswith('call_'):
                    suffix = sym[len('call_'):]
                elif sym.startswith('func_'):
                    suffix = sym[len('func_'):]
                else:
                    return None

                if suffix.startswith('0x'):
                    try:
                        v = int(suffix[2:], 16)
                    except ValueError:
                        return None
                else:
                    try:
                        v = int(suffix, 10)
                    except ValueError:
                        # Back-compat: allow hex without 0x prefix (e.g. call_3a).
                        try:
                            v = int(suffix, 16)
                        except ValueError:
                            return None
                if 0 <= v <= 255:
                    return v
                return None

            if name == 'push16':
                if len(args) != 1:
                    raise ValueError('`push16` expects 1 arg: push16(value).')
                value = args[0]
                if not isinstance(value, int):
                    raise ValueError('`push16` value must be a number.')
                if not (0 <= value <= 0xFFFF):
                    raise ValueError(f'`push16` value out of range (0..0xffff): {value}.')
                pending_instrs.append((3, 1, 0))
                pending_instrs.append((-2, 0, value))
                pc += 2
                continue

            # Legacy call sugar: call(call_NNN, a, b, c) (or call(func_NNN, ...))
            # => push(a); push(b); push(c); call(2, NNN); instr_12(2, nargs)
            if name == 'call' and args and isinstance(args[0], str):
                call_target = _parse_native_func_symbol(args[0])
                if call_target is not None:
                    call_instr_id = _NAME_TO_INSTR_ID.get('call')
                    push_instr_id = _NAME_TO_INSTR_ID.get('push')
                    if call_instr_id is None or push_instr_id is None:
                        raise ValueError('Internal error: missing `call`/`push` instruction ids.')

                    call_args = args[1:]

                    _bin_name_to_id = {
                        'and': 0,
                        'or': 1,
                        'eq': 2,
                        'ne': 3,
                        'lt': 4,
                        'le': 5,
                        'gt': 6,
                        'ge': 7,
                        'add': 8,
                        'sub': 9,
                        'mul': 10,
                        'div': 11,
                        'shr': 12,
                        'shl': 13,
                        'band': 14,
                        'bor': 15,
                        'mod': 16,
                        'xor': 17,
                    }
                    _un_name_to_id = {
                        'not': 0,
                        'neg': 1,
                        'bnot': 2,
                    }

                    def emit_value_expr(x: object) -> None:
                        nonlocal pc

                        if isinstance(x, int):
                            if not (0 <= x <= 255):
                                raise ValueError(
                                    f'Immediate value out of range for i8 literal (0..255): {x}. Use u16(0x....) for 16-bit pushes.'
                                )
                            pending_instrs.append((push_instr_id, 2, x))
                            pc += 1
                            return

                        if isinstance(x, str):
                            idx = string_key_to_index.get(x)
                            if idx is None:
                                raise ValueError(f'Unknown string key: {x}.')
                            if not (0 <= idx <= 255):
                                raise ValueError(
                                    f'TEXT index out of range for i8 literal (0..255): {idx}. (Key: {x})'
                                )
                            pending_instrs.append((push_instr_id, 2, idx))
                            pc += 1
                            return

                        if not isinstance(x, _InstrExpr):
                            raise ValueError('Native call arguments must be numbers or value expressions.')

                        n = x.name
                        a = x.args

                        if n == 'u16':
                            if len(a) != 1:
                                raise ValueError('u16 expects 1 arg: u16(0x1234) or u16(s_key).')
                            v_src = a[0]
                            if isinstance(v_src, str):
                                raw = strings.get(v_src)
                                if raw is None:
                                    raise ValueError(f'Unknown string key: {v_src}.')
                                try:
                                    v = int(raw.strip(), 0)
                                except ValueError as e:
                                    raise ValueError(
                                        f'u16({v_src}) expects strings[{v_src!r}] to be a numeric literal, got: {raw!r}.'
                                    ) from e
                            elif isinstance(v_src, int):
                                v = v_src
                            else:
                                raise ValueError('u16 expects a numeric arg or a string key: u16(0x1234) or u16(s_key).')
                            if not (0 <= v <= 0xFFFF):
                                raise ValueError(f'u16 value out of range (0..0xffff): {v}.')
                            pending_instrs.append((3, 1, 0))
                            pending_instrs.append((-2, 0, v))
                            pc += 2
                            return

                        if n == 'var':
                            if len(a) != 1 or not isinstance(a[0], int):
                                raise ValueError('var expects 1 numeric arg: var(0xNN).')
                            idx = a[0]
                            if not (0 <= idx <= 255):
                                raise ValueError(f'var index out of range (0..255): {idx}.')
                            pending_instrs.append((5, 2, idx))
                            pc += 1
                            return

                        if n == 'arg':
                            if len(a) != 1 or not isinstance(a[0], int):
                                raise ValueError('arg expects 1 numeric arg: arg(0xNN).')
                            idx = a[0]
                            if not (0 <= idx <= 255):
                                raise ValueError(f'arg index out of range (0..255): {idx}.')
                            pending_instrs.append((6, 2, idx))
                            pc += 1
                            return

                        if n == 'local':
                            if len(a) != 1 or not isinstance(a[0], int):
                                raise ValueError('local expects 1 numeric arg: local(0xNN).')
                            idx = a[0]
                            if not (0 <= idx <= 255):
                                raise ValueError(f'local index out of range (0..255): {idx}.')
                            pending_instrs.append((7, 2, idx))
                            pc += 1
                            return

                        if n == 'acc':
                            if len(a) != 0:
                                raise ValueError('acc expects no args: acc().')
                            pending_instrs.append((2, 2, 0))
                            pc += 1
                            return

                        if n in _un_name_to_id:
                            if len(a) != 1:
                                raise ValueError(f'{n} expects 1 arg.')
                            emit_value_expr(a[0])
                            pending_instrs.append((16, 2, _un_name_to_id[n]))
                            pc += 1
                            return

                        if n in _bin_name_to_id:
                            if len(a) != 2:
                                raise ValueError(f'{n} expects 2 args.')
                            emit_value_expr(a[0])
                            emit_value_expr(a[1])
                            pending_instrs.append((17, 2, _bin_name_to_id[n]))
                            pc += 1
                            return

                        # Back-compat: raw single-word instruction expression: foo(flags, arg)
                        expr_id = _NAME_TO_INSTR_ID.get(n)
                        if expr_id is None:
                            if n.startswith('instr_'):
                                try:
                                    expr_id = int(n[len('instr_'):])
                                except ValueError as e:
                                    raise ValueError(f'Invalid instruction name: {n}.') from e
                                if not (0 <= expr_id <= 31):
                                    raise ValueError(f'Instruction id out of range: {expr_id}.')
                            else:
                                raise ValueError(f'Unknown value expression: {n}(...)')

                        if len(a) != 2:
                            raise ValueError(f'Instruction expression expects 2 args: {n}(flags, arg).')
                        expr_flags, expr_arg = a
                        if not isinstance(expr_flags, int):
                            raise ValueError('Instruction expression flags must be a number.')
                        if not (0 <= expr_flags <= 7):
                            raise ValueError(f'Flags out of range (0..7): {expr_flags}.')
                        if not isinstance(expr_arg, int):
                            raise ValueError('Instruction expression arg must be a number.')
                        if not (0 <= expr_arg <= 255):
                            raise ValueError(f'Arg out of range (0..255): {expr_arg}.')

                        pending_instrs.append((expr_id, expr_flags, expr_arg))
                        pc += 1
                        return

                    for a in call_args:
                        emit_value_expr(a)

                    pending_instrs.append((call_instr_id, 2, call_target))
                    pc += 1

                    nargs = len(call_args)
                    if not (0 <= nargs <= 255):
                        raise ValueError(f'Native call argument count out of range (0..255): {nargs}.')
                    pending_instrs.append((12, 2, nargs))
                    pc += 1
                    continue

            # Call sugar: call_NNN(a, b, c) / speak(a, b, c) / tell(a, b, c)
            # => push(a); push(b); push(c); call(2, NNN); instr_12(2, nargs)
            if name.startswith('call_') or name in _NATIVE_CALL_ALIAS_TO_ID:
                call_instr_id = _NAME_TO_INSTR_ID.get('call')
                push_instr_id = _NAME_TO_INSTR_ID.get('push')
                if call_instr_id is None or push_instr_id is None:
                    raise ValueError('Internal error: missing `call`/`push` instruction ids.')

                call_target: int
                if name.startswith('call_'):
                    suffix = name[len('call_'):]
                    if suffix.startswith('0x'):
                        try:
                            call_target = int(suffix[2:], 16)
                        except ValueError as e:
                            raise ValueError(f'Invalid call target: {name}.') from e
                    else:
                        try:
                            call_target = int(suffix, 10)
                        except ValueError:
                            # Back-compat: allow hex without 0x prefix (e.g. call_3a).
                            try:
                                call_target = int(suffix, 16)
                            except ValueError as e:
                                raise ValueError(f'Invalid call target: {name}.') from e
                else:
                    call_target = _NATIVE_CALL_ALIAS_TO_ID[name]

                if not (0 <= call_target <= 255):
                    raise ValueError(f'Call target out of range (0..255): {call_target}.')

                _bin_name_to_id = {
                    'and': 0,
                    'or': 1,
                    'eq': 2,
                    'ne': 3,
                    'lt': 4,
                    'le': 5,
                    'gt': 6,
                    'ge': 7,
                    'add': 8,
                    'sub': 9,
                    'mul': 10,
                    'div': 11,
                    'shr': 12,
                    'shl': 13,
                    'band': 14,
                    'bor': 15,
                    'mod': 16,
                    'xor': 17,
                }
                _un_name_to_id = {
                    'not': 0,
                    'neg': 1,
                    'bnot': 2,
                }

                def emit_value_expr(x: object) -> None:
                    nonlocal pc

                    if isinstance(x, int):
                        if not (0 <= x <= 255):
                            raise ValueError(f'Immediate value out of range for i8 literal (0..255): {x}. Use u16(0x....) for 16-bit pushes.')
                        pending_instrs.append((push_instr_id, 2, x))
                        pc += 1
                        return

                    if isinstance(x, str):
                        idx = string_key_to_index.get(x)
                        if idx is None:
                            raise ValueError(f'Unknown string key: {x}.')
                        if not (0 <= idx <= 255):
                            raise ValueError(
                                f'TEXT index out of range for i8 literal (0..255): {idx}. (Key: {x})'
                            )
                        pending_instrs.append((push_instr_id, 2, idx))
                        pc += 1
                        return

                    if not isinstance(x, _InstrExpr):
                        raise ValueError('Native call arguments must be numbers or value expressions.')

                    n = x.name
                    a = x.args

                    if n == 'u16':
                        if len(a) != 1:
                            raise ValueError('u16 expects 1 arg: u16(0x1234) or u16(s_key).')
                        v_src = a[0]
                        if isinstance(v_src, str):
                            raw = strings.get(v_src)
                            if raw is None:
                                raise ValueError(f'Unknown string key: {v_src}.')
                            try:
                                v = int(raw.strip(), 0)
                            except ValueError as e:
                                raise ValueError(
                                    f'u16({v_src}) expects strings[{v_src!r}] to be a numeric literal, got: {raw!r}.'
                                ) from e
                        elif isinstance(v_src, int):
                            v = v_src
                        else:
                            raise ValueError('u16 expects a numeric arg or a string key: u16(0x1234) or u16(s_key).')
                        if not (0 <= v <= 0xFFFF):
                            raise ValueError(f'u16 value out of range (0..0xffff): {v}.')
                        pending_instrs.append((3, 1, 0))
                        pending_instrs.append((-2, 0, v))
                        pc += 2
                        return

                    if n == 'var':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('var expects 1 numeric arg: var(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'var index out of range (0..255): {idx}.')
                        pending_instrs.append((5, 2, idx))
                        pc += 1
                        return

                    if n == 'arg':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('arg expects 1 numeric arg: arg(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'arg index out of range (0..255): {idx}.')
                        pending_instrs.append((6, 2, idx))
                        pc += 1
                        return

                    if n == 'local':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('local expects 1 numeric arg: local(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'local index out of range (0..255): {idx}.')
                        pending_instrs.append((7, 2, idx))
                        pc += 1
                        return

                    if n == 'acc':
                        if len(a) != 0:
                            raise ValueError('acc expects no args: acc().')
                        pending_instrs.append((2, 2, 0))
                        pc += 1
                        return

                    if n in _un_name_to_id:
                        if len(a) != 1:
                            raise ValueError(f'{n} expects 1 arg.')
                        emit_value_expr(a[0])
                        pending_instrs.append((16, 2, _un_name_to_id[n]))
                        pc += 1
                        return

                    if n in _bin_name_to_id:
                        if len(a) != 2:
                            raise ValueError(f'{n} expects 2 args.')
                        emit_value_expr(a[0])
                        emit_value_expr(a[1])
                        pending_instrs.append((17, 2, _bin_name_to_id[n]))
                        pc += 1
                        return

                    # Back-compat: raw single-word instruction expression: foo(flags, arg)
                    expr_id = _NAME_TO_INSTR_ID.get(n)
                    if expr_id is None:
                        if n.startswith('instr_'):
                            try:
                                expr_id = int(n[len('instr_'):])
                            except ValueError as e:
                                raise ValueError(f'Invalid instruction name: {n}.') from e
                            if not (0 <= expr_id <= 31):
                                raise ValueError(f'Instruction id out of range: {expr_id}.')
                        else:
                            raise ValueError(f'Unknown value expression: {n}(...)')

                    if len(a) != 2:
                        raise ValueError(f'Instruction expression expects 2 args: {n}(flags, arg).')
                    expr_flags, expr_arg = a
                    if not isinstance(expr_flags, int):
                        raise ValueError('Instruction expression flags must be a number.')
                    if not (0 <= expr_flags <= 7):
                        raise ValueError(f'Flags out of range (0..7): {expr_flags}.')

                    if expr_id == 0:
                        if isinstance(expr_arg, int):
                            if expr_flags == 4:
                                if not (0 <= expr_arg <= 0x7FFF):
                                    raise ValueError(f'Long-jmp target out of range (0..0x7fff): {expr_arg}.')
                            else:
                                if not (0 <= expr_arg <= 255):
                                    raise ValueError(f'Arg out of range (0..255): {expr_arg}.')
                        elif isinstance(expr_arg, str):
                            pass
                        else:
                            raise ValueError('Instruction expression `jmp` arg must be a number or a label.')
                    else:
                        if not isinstance(expr_arg, int):
                            raise ValueError('Instruction expression arg must be a number.')
                        if not (0 <= expr_arg <= 255):
                            raise ValueError(f'Arg out of range (0..255): {expr_arg}.')

                    pending_instrs.append((expr_id, expr_flags, expr_arg))
                    pc += 1

                for a in args:
                    emit_value_expr(a)

                pending_instrs.append((call_instr_id, 2, call_target))
                pc += 1

                # Canonical stack fix after a call: instr_12(2, nargs)
                nargs = len(args)
                if not (0 <= nargs <= 255):
                    raise ValueError(f'`call_...` argument count out of range (0..255): {nargs}.')
                pending_instrs.append((12, 2, nargs))
                pc += 1
                continue

            # Scripted function call sugar: func_229(a, b) => push(a); push(b); instr_2(2,1); jmp(4, 229)
            if name.startswith('func_'):
                push_instr_id = _NAME_TO_INSTR_ID.get('push')
                if push_instr_id is None:
                    raise ValueError('Internal error: missing `push` instruction id.')

                suffix = name[len('func_'):]
                if suffix.startswith('0x'):
                    try:
                        entry_pc = int(suffix[2:], 16)
                    except ValueError as e:
                        raise ValueError(f'Invalid function target: {name}.') from e
                else:
                    try:
                        entry_pc = int(suffix, 10)
                    except ValueError as e:
                        raise ValueError(f'Invalid function target: {name}.') from e

                if not (0 <= entry_pc <= 0x7FFF):
                    raise ValueError(f'Function target out of range (0..0x7fff): {entry_pc}.')

                _bin_name_to_id = {
                    'and': 0,
                    'or': 1,
                    'eq': 2,
                    'ne': 3,
                    'lt': 4,
                    'le': 5,
                    'gt': 6,
                    'ge': 7,
                    'add': 8,
                    'sub': 9,
                    'mul': 10,
                    'div': 11,
                    'shr': 12,
                    'shl': 13,
                    'band': 14,
                    'bor': 15,
                    'mod': 16,
                    'xor': 17,
                }
                _un_name_to_id = {
                    'not': 0,
                    'neg': 1,
                    'bnot': 2,
                }

                def emit_value_expr(x: object) -> None:
                    nonlocal pc

                    if isinstance(x, int):
                        if not (0 <= x <= 255):
                            raise ValueError(
                                f'Immediate value out of range for i8 literal (0..255): {x}. Use u16(0x....) for 16-bit pushes.'
                            )
                        pending_instrs.append((push_instr_id, 2, x))
                        pc += 1
                        return

                    if isinstance(x, str):
                        idx = string_key_to_index.get(x)
                        if idx is None:
                            raise ValueError(f'Unknown string key: {x}.')
                        if not (0 <= idx <= 255):
                            raise ValueError(
                                f'TEXT index out of range for i8 literal (0..255): {idx}. (Key: {x})'
                            )
                        pending_instrs.append((push_instr_id, 2, idx))
                        pc += 1
                        return

                    if not isinstance(x, _InstrExpr):
                        raise ValueError('`func_...` arguments must be numbers or value expressions.')

                    n = x.name
                    a = x.args

                    if n == 'u16':
                        if len(a) != 1:
                            raise ValueError('u16 expects 1 arg: u16(0x1234) or u16(s_key).')
                        v_src = a[0]
                        if isinstance(v_src, str):
                            raw = strings.get(v_src)
                            if raw is None:
                                raise ValueError(f'Unknown string key: {v_src}.')
                            try:
                                v = int(raw.strip(), 0)
                            except ValueError as e:
                                raise ValueError(
                                    f'u16({v_src}) expects strings[{v_src!r}] to be a numeric literal, got: {raw!r}.'
                                ) from e
                        elif isinstance(v_src, int):
                            v = v_src
                        else:
                            raise ValueError('u16 expects a numeric arg or a string key: u16(0x1234) or u16(s_key).')
                        if not (0 <= v <= 0xFFFF):
                            raise ValueError(f'u16 value out of range (0..0xffff): {v}.')
                        pending_instrs.append((3, 1, 0))
                        pending_instrs.append((-2, 0, v))
                        pc += 2
                        return

                    if n == 'var':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('var expects 1 numeric arg: var(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'var index out of range (0..255): {idx}.')
                        pending_instrs.append((5, 2, idx))
                        pc += 1
                        return

                    if n == 'arg':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('arg expects 1 numeric arg: arg(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'arg index out of range (0..255): {idx}.')
                        pending_instrs.append((6, 2, idx))
                        pc += 1
                        return

                    if n == 'local':
                        if len(a) != 1 or not isinstance(a[0], int):
                            raise ValueError('local expects 1 numeric arg: local(0xNN).')
                        idx = a[0]
                        if not (0 <= idx <= 255):
                            raise ValueError(f'local index out of range (0..255): {idx}.')
                        pending_instrs.append((7, 2, idx))
                        pc += 1
                        return

                    if n == 'acc':
                        if len(a) != 0:
                            raise ValueError('acc expects no args: acc().')
                        pending_instrs.append((2, 2, 0))
                        pc += 1
                        return

                    if n in _un_name_to_id:
                        if len(a) != 1:
                            raise ValueError(f'{n} expects 1 arg.')
                        emit_value_expr(a[0])
                        pending_instrs.append((16, 2, _un_name_to_id[n]))
                        pc += 1
                        return

                    if n in _bin_name_to_id:
                        if len(a) != 2:
                            raise ValueError(f'{n} expects 2 args.')
                        emit_value_expr(a[0])
                        emit_value_expr(a[1])
                        pending_instrs.append((17, 2, _bin_name_to_id[n]))
                        pc += 1
                        return

                    # Back-compat: raw single-word instruction expression: foo(flags, arg)
                    expr_id = _NAME_TO_INSTR_ID.get(n)
                    if expr_id is None:
                        if n.startswith('instr_'):
                            try:
                                expr_id = int(n[len('instr_'):])
                            except ValueError as e:
                                raise ValueError(f'Invalid instruction name: {n}.') from e
                            if not (0 <= expr_id <= 31):
                                raise ValueError(f'Instruction id out of range: {expr_id}.')
                        else:
                            raise ValueError(f'Unknown value expression: {n}(...)')

                    if len(a) != 2:
                        raise ValueError(f'Instruction expression expects 2 args: {n}(flags, arg).')
                    expr_flags, expr_arg = a
                    if not isinstance(expr_flags, int):
                        raise ValueError('Instruction expression flags must be a number.')
                    if not (0 <= expr_flags <= 7):
                        raise ValueError(f'Flags out of range (0..7): {expr_flags}.')

                    if expr_id == 0:
                        if isinstance(expr_arg, int):
                            if expr_flags == 4:
                                if not (0 <= expr_arg <= 0x7FFF):
                                    raise ValueError(f'Long-jmp target out of range (0..0x7fff): {expr_arg}.')
                            else:
                                if not (0 <= expr_arg <= 255):
                                    raise ValueError(f'Arg out of range (0..255): {expr_arg}.')
                        elif isinstance(expr_arg, str):
                            pass
                        else:
                            raise ValueError('Instruction expression `jmp` arg must be a number or a label.')
                    else:
                        if not isinstance(expr_arg, int):
                            raise ValueError('Instruction expression arg must be a number.')
                        if not (0 <= expr_arg <= 255):
                            raise ValueError(f'Arg out of range (0..255): {expr_arg}.')

                    pending_instrs.append((expr_id, expr_flags, expr_arg))
                    pc += 1

                for a in args:
                    emit_value_expr(a)

                # stackctl prologue (arg=1) + long-jmp to entry
                pending_instrs.append((2, 2, 1))
                pending_instrs.append((0, 4, entry_pc))
                pc += 2

                # Canonical caller-side arg cleanup after return.
                nargs = len(args)
                if nargs:
                    pending_instrs.append((12, 2, nargs))
                    pc += 1
                continue
            if name == 'entry':
                if pending_globals is not None:
                    raise ValueError('Cannot mix `globals = [...]` with legacy `entry(i, offset)` statements.')
                if len(args) != 2:
                    raise ValueError('`entry` expects 2 args: entry(i, offset).')
                idx, off = args
                if not isinstance(idx, int) or not isinstance(off, int):
                    raise ValueError('`entry` expects numeric args: entry(i, offset).')
                if idx != len(legacy_order):
                    raise ValueError(f'`entry` index mismatch: got {idx}, expected {len(legacy_order)}.')
                legacy_order.append(_u16(off))
                continue

            if name == 'ifnot':
                if len(args) != 2:
                    raise ValueError('`ifnot` expects 2 args: ifnot(flags, target).')
                flags, target = args
                if not isinstance(flags, int):
                    raise ValueError(f'Flags must be a number, got {type(flags).__name__}.')
                if flags != 1:
                    raise ValueError('Only `ifnot(1, ...)` is supported.')
                if not isinstance(target, (int, str)):
                    raise ValueError('`ifnot` target must be a number or a label.')
                pending_ifnots.append((flags, target))
                pending_instrs.append((15, 1, 0))
                pending_instrs.append((-1, 0, 0))
                pc += 2
                continue

            instr_id = _NAME_TO_INSTR_ID.get(name)
            if instr_id is None:
                if name.startswith('instr_'):
                    try:
                        instr_id = int(name[len('instr_'):])
                    except ValueError as e:
                        raise ValueError(f'Invalid instruction name: {name}.') from e
                    if not (0 <= instr_id <= 31):
                        raise ValueError(f'Instruction id out of range: {instr_id}.')
                else:
                    raise ValueError(f'Unknown statement: {name}(...)')

            if len(args) != 2:
                raise ValueError(f'`{name}` expects 2 args: {name}(flags, arg).')
            flags, arg = args
            if not isinstance(flags, int):
                raise ValueError(f'Flags must be a number, got {type(flags).__name__}.')
            if not (0 <= flags <= 7):
                raise ValueError(f'Flags out of range (0..7): {flags}.')

            call_instr_id = _NAME_TO_INSTR_ID.get('call')
            if call_instr_id is not None and instr_id == call_instr_id and isinstance(arg, str):
                sym_target = _parse_native_func_symbol(arg)
                if sym_target is None:
                    raise ValueError(f'Invalid native call target symbol: {arg}. Expected call_NNN.')
                arg = sym_target

            if instr_id == 0:
                if isinstance(arg, int):
                    if flags == 4:
                        if not (0 <= arg <= 0x7FFF):
                            raise ValueError(f'Long-jmp target out of range (0..0x7fff): {arg}.')
                    else:
                        if not (0 <= arg <= 255):
                            raise ValueError(f'Arg out of range (0..255): {arg}.')
                elif isinstance(arg, str):
                    # Resolved after all labels are known.
                    pass
                else:
                    raise ValueError('`jmp` arg must be a number or a label.')
            else:
                if not isinstance(arg, int):
                    raise ValueError(f'`{name}` arg must be a number.')
                if not (0 <= arg <= 255):
                    raise ValueError(f'Arg out of range (0..255): {arg}.')

            pending_instrs.append((instr_id, flags, arg))
            pc += 1

        if pending_leave_n is not None:
            raise ValueError('`leave` must be immediately followed by `return`.')

        order: list[int]
        if pending_globals is None:
            order = legacy_order
        else:
            order = []
            for ref in pending_globals:
                if isinstance(ref, int):
                    off = ref
                else:
                    if ref not in labels:
                        raise ValueError(f'Unknown label in `globals`: {ref}.')
                    off = labels[ref]
                order.append(_u16(off))

        data: list[int] = []
        ifnot_i = 0
        for instr_id, flags, arg in pending_instrs:
            if instr_id == -1:
                # Operand word for the most recent `ifnot`.
                if ifnot_i >= len(pending_ifnots):
                    raise ValueError('Internal error: ifnot operand mismatch.')
                _, target = pending_ifnots[ifnot_i]
                ifnot_i += 1
                if isinstance(target, str):
                    if target not in labels:
                        raise ValueError(f'Unknown label in `ifnot`: {target}.')
                    target = labels[target]
                assert isinstance(target, int)
                if not (0 <= target <= 0x7FFF):
                    raise ValueError(f'`ifnot` target out of range (0..0x7fff): {target}.')
                data.append(_u16(0x8000 | target))
                continue

            if instr_id == -2:
                if not isinstance(arg, int):
                    raise ValueError('Internal error: raw word must be numeric.')
                data.append(_u16(arg))
                continue

            if instr_id == 0 and isinstance(arg, str):
                if arg not in labels:
                    raise ValueError(f'Unknown label in `jmp`: {arg}.')
                target = labels[arg]
                if flags == 4:
                    if not (0 <= target <= 0x7FFF):
                        raise ValueError(f'`jmp` label target out of 15-bit range: {arg} -> {target}.')
                    arg = target
                else:
                    if not (0 <= target <= 255):
                        raise ValueError(f'`jmp` label target out of u8 range: {arg} -> {target}.')
                    arg = target

            if not isinstance(arg, int):
                raise ValueError('Internal error: unresolved non-jmp label argument.')
            if instr_id == 0 and flags == 4:
                if not (0 <= arg <= 0x7FFF):
                    raise ValueError(f'Long-jmp target out of range (0..0x7fff): {arg}.')
                word = 0x8000 | (arg & 0x7FFF)
            else:
                word = ((flags & 0x7) << 13) | ((instr_id & 0x1f) << 8) | (arg & 0xff)
            data.append(word)

        if ifnot_i != len(pending_ifnots):
            raise ValueError('Internal error: not all `ifnot` statements were emitted.')

        return _KyraProgram(strings=strings, text_present=text_present, order=order, data=data)

    def _parse_strings_assignment(self) -> dict[str, str]:
        name = self._consume('IDENT').value
        if name != 'strings':
            raise ValueError('First statement must be `strings = {...}`.')
        self._consume('=')
        return self._parse_strings_dict()

    def _parse_globals_assignment(self) -> list[str | int]:
        name = self._consume('IDENT').value
        assert name in {'globals', 'entries'}
        self._consume('=')
        return self._parse_entries_list()

    def _parse_strings_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        self._consume('{')
        if self._lookahead.kind == '}':
            self._consume('}')
            return out
        while True:
            key_tok = self._consume('IDENT')
            self._consume(':')
            val_tok = self._consume('STRING')
            out[key_tok.value] = val_tok.value
            if self._lookahead.kind == ',':
                self._consume(',')
                if self._lookahead.kind == '}':
                    self._consume('}')
                    return out
                continue
            self._consume('}')
            return out

    def _parse_entries_list(self) -> list[str | int]:
        out: list[str | int] = []
        self._consume('[')
        if self._lookahead.kind == ']':
            self._consume(']')
            return out
        while True:
            if self._lookahead.kind == 'IDENT':
                out.append(self._consume('IDENT').value)
            elif self._lookahead.kind == 'NUMBER':
                out.append(int(self._consume('NUMBER').value, 0))
            else:
                raise ValueError(f'Expected label or number in `globals`, got {self._lookahead.kind}.')
            if self._lookahead.kind == ',':
                self._consume(',')
                if self._lookahead.kind == ']':
                    self._consume(']')
                    return out
                continue
            self._consume(']')
            return out

    def _parse_call_args_any(self) -> list[object]:
        args: list[object] = []
        self._consume('(')
        if self._lookahead.kind == ')':
            self._consume(')')
            return args
        while True:
            if self._lookahead.kind == 'NUMBER':
                n = self._consume('NUMBER')
                args.append(int(n.value, 0))
            elif self._lookahead.kind == 'IDENT':
                ident = self._consume('IDENT').value
                if self._lookahead.kind == '(':
                    expr_args = self._parse_call_args_any()
                    args.append(_InstrExpr(name=ident, args=expr_args))
                else:
                    # Allow `acc` as a bare value expression (no parentheses).
                    if ident == 'acc':
                        args.append(_InstrExpr(name='acc', args=[]))
                    else:
                        args.append(ident)
            else:
                raise ValueError(f'Expected NUMBER or IDENT in args, got {self._lookahead.kind}.')
            if self._lookahead.kind == ',':
                self._consume(',')
                if self._lookahead.kind == ')':
                    self._consume(')')
                    return args
                continue
            self._consume(')')
            return args

    def _consume(self, kind: str) -> _Token:
        if self._lookahead.kind != kind:
            raise ValueError(f'Expected {kind}, got {self._lookahead.kind}.')
        tok = self._lookahead
        self._lookahead = next(self._tokens)
        return tok


def _parse_kyra_source(source: str) -> _KyraProgram:
    text_present: bool | None = None
    for line in source.splitlines():
        s = line.strip().lower()
        if s == '# text: present':
            text_present = True
        elif s == '# text: absent':
            text_present = False

    lexer = _Lexer(source)
    parser = _Parser(lexer.tokens())
    return parser.parse_program(text_present=text_present)


def _desugar_structured_control_flow(source: str) -> str:
    """Expand Python-like if/elif/else blocks into low-level Kyra statements.

    The decompiler can present a structured view (`if/else:`) while the compiler
    stays lossless by lowering structured blocks into the canonical instruction
    pattern used by the original scripts.

    NOTE: the condition expression is currently treated as a placeholder and is
    ignored by the compiler.
    """

    lines = source.splitlines()
    ends_with_nl = source.endswith('\n')
    label_counter = 0

    def indent_of(line: str) -> int:
        n_tabs = 0
        i = 0
        spaces = 0
        while i < len(line):
            ch = line[i]
            if ch == '\t':
                n_tabs += 1
                i += 1
                spaces = 0
                continue
            if ch == ' ':
                spaces += 1
                i += 1
                if spaces == 4:
                    n_tabs += 1
                    spaces = 0
                continue
            break
        if spaces != 0:
            raise ValueError('Indentation must use tabs or multiples of 4 spaces.')
        return n_tabs

    def strip_indent(line: str, n: int) -> str:
        i = 0
        tabs_left = n
        spaces = 0
        while i < len(line) and tabs_left > 0:
            ch = line[i]
            if ch == '\t':
                tabs_left -= 1
                i += 1
                continue
            if ch == ' ':
                spaces += 1
                i += 1
                if spaces == 4:
                    tabs_left -= 1
                    spaces = 0
                continue
            break
        if tabs_left != 0:
            raise ValueError('Internal error: failed to strip indentation.')
        return line[i:]

    def is_if_header(s: str) -> bool:
        return s.startswith('if ') and s.endswith(':')

    def is_elif_header(s: str) -> bool:
        return s.startswith('elif ') and s.endswith(':')

    def is_else_header(s: str) -> bool:
        return s == 'else:'

    def collect_suite(sub_lines: list[str], i: int, header_indent: int) -> tuple[list[str], int]:
        suite: list[str] = []
        while i < len(sub_lines):
            raw = sub_lines[i]
            if raw.strip() == '':
                suite.append(raw)
                i += 1
                continue
            if indent_of(raw) <= header_indent:
                break
            suite.append(raw)
            i += 1
        return suite, i

    def dedent_one(suite_lines: list[str], header_indent: int) -> list[str]:
        out_lines: list[str] = []
        for raw in suite_lines:
            if raw.strip() == '':
                out_lines.append('')
            else:
                out_lines.append(strip_indent(raw, header_indent + 1))
        return out_lines

    def reindent(lines_in: list[str], indent: int) -> list[str]:
        out_lines: list[str] = []
        for l in lines_in:
            if l.strip() == '':
                out_lines.append(l)
            else:
                out_lines.append('\t' * indent + l)
        return out_lines

    def process(sub_lines: list[str]) -> list[str]:
        nonlocal label_counter
        out: list[str] = []
        i = 0
        while i < len(sub_lines):
            raw = sub_lines[i]
            if raw.strip() == '':
                out.append(raw)
                i += 1
                continue

            ind = indent_of(raw)
            s = strip_indent(raw, ind).rstrip('\r')

            if is_if_header(s):
                clauses: list[list[str]] = []
                else_block: list[str] | None = None

                i += 1
                then_suite, i = collect_suite(sub_lines, i, ind)
                clauses.append(then_suite)

                while i < len(sub_lines):
                    raw2 = sub_lines[i]
                    if raw2.strip() == '':
                        break
                    ind2 = indent_of(raw2)
                    if ind2 != ind:
                        break
                    s2 = strip_indent(raw2, ind2).rstrip('\r')
                    if not is_elif_header(s2):
                        break
                    i += 1
                    suite2, i = collect_suite(sub_lines, i, ind)
                    clauses.append(suite2)

                if i < len(sub_lines):
                    raw3 = sub_lines[i]
                    if raw3.strip() != '':
                        ind3 = indent_of(raw3)
                        s3 = strip_indent(raw3, ind3).rstrip('\r')
                        if ind3 == ind and is_else_header(s3):
                            i += 1
                            else_block, i = collect_suite(sub_lines, i, ind)

                end_label = f'if_end_{label_counter}'
                label_counter += 1
                next_label = f'if_else_{label_counter}'
                label_counter += 1

                for idx_clause, suite in enumerate(clauses):
                    is_last_clause = idx_clause == len(clauses) - 1
                    has_next = (not is_last_clause) or (else_block is not None)
                    else_label = next_label if has_next else end_label

                    out.append('\t' * ind + 'instr_15(1, 0x00)')
                    out.append('\t' * ind + f'jmp(4, {else_label})')

                    body_lowered = process(dedent_one(suite, ind))
                    out.extend(reindent(body_lowered, ind))

                    if has_next:
                        out.append('\t' * ind + f'jmp(4, {end_label})')
                        out.append('\t' * ind + f'{else_label}:')
                        next_label = f'if_else_{label_counter}'
                        label_counter += 1

                if else_block is not None:
                    else_lowered = process(dedent_one(else_block, ind))
                    out.extend(reindent(else_lowered, ind))

                out.append('\t' * ind + f'{end_label}:')
                continue

            if is_elif_header(s) or is_else_header(s):
                raise ValueError('`elif`/`else` without matching `if`.')

            out.append(raw)
            i += 1
        return out

    expanded = process(lines)
    result = '\n'.join(expanded)
    if ends_with_nl:
        result += '\n'
    return result


_INSTR_ID_TO_NAME = {
    0: 'jmp',
    4: 'push',
    14: 'call',
    16: 'unary',
    17: 'binary',
}

_NAME_TO_INSTR_ID = {v: k for k, v in _INSTR_ID_TO_NAME.items()}
