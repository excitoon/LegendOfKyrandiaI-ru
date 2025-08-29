_instructions = [
]


def _read_block(data: bytes, inclusive_size: bool = False) -> tuple[int, bytes]:
    assert len(data) >= 4
    size = int.from_bytes(data[0:4], 'big')
    if inclusive_size:
        size -= 8
        assert size >= 0, f'Negative block size {size}.'
    assert len(data) >= 4 + size, f'Block size {size} exceeds available data {len(data)-4}.'
    block = data[4:4+size]
    return size, block


def _read_text(data: bytes) -> list[str]:
    i = 0
    offsets = []
    strings = []
    while i < len(data):
        assert i + 2 <= len(data)
        offsets.append(int.from_bytes(data[i:i+2], 'big'))
        i += 2
        if offsets[0] <= i:
            assert offsets[0] == i
            break
    offsets.append(len(data))
    assert offsets == sorted(offsets)
    for j in range(len(offsets)-1):
        s = data[offsets[j]:offsets[j+1]].decode('ascii')
        assert s.endswith('\x00')
        s = s[:-1]
        strings.append(s)
    return strings


def _read_data(data: list[int]) -> list[str]:
    result = []
    for x in data:
        flags, instruction, arguments = (x & 0xe000) >> 13, (x & 0x1f00) >> 8, x & 0x00ff
        assert 0 <= instruction < 18
        result.append(f'{str(instruction).rjust(2, "0")} {flags} {str(arguments).rjust(3, "0")}')
    return result


def decode(data: bytes) -> list[str]:
    assert data.startswith(b'FORM')
    payload_size, payload = _read_block(data[4:], inclusive_size=True)
    data = data[8+payload_size:]
    assert data == b''
    assert payload.startswith(b'EMC2')
    payload = payload[4:]
    assert payload.startswith(b'ORDR')
    order_size, order_block = _read_block(payload[4:])
    payload = payload[8+order_size:]
    if order_size & 1:
        assert payload.startswith(b'\x00')
        payload = payload[1:]
    if payload.startswith(b'TEXT'):
        text_size, text_block = _read_block(payload[4:])
        payload = payload[8+text_size:]
        if text_size & 1:
            assert payload.startswith(b'\x00')
            payload = payload[1:]
    else:
        text_size = None
        text_block = None
    assert payload.startswith(b'DATA')
    data_size, data_block = _read_block(payload[4:])
    payload = payload[8+data_size:]
    if data_size & 1:
        assert payload.startswith(b'\x00')
        payload = payload[:-1]
    assert payload == b''

    if text_block is not None:
        strings = _read_text(text_block)
    else:
        strings = None

    assert len(order_block) % 2 == 0
    order_list = [int.from_bytes(order_block[i:i+2], 'big') for i in range(0, len(order_block), 2)]

    assert len(data_block) % 2 == 0
    data_list = _read_data([int.from_bytes(data_block[i:i+2], 'big') for i in range(0, len(data_block), 2)])

    for offset in order_list:
        assert offset in range(len(data_list)), f'Order offset {offset} out of range 0..{len(data_list)-1}.'

    return order_list, strings, data_list
