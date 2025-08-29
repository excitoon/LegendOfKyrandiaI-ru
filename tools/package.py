def decode(data: bytes) -> dict[str, bytes]:
    offsets = []
    chunks = []
    names = []
    i = 0
    while i < len(data):
        assert i + 4 <= len(data)
        offset = int.from_bytes(data[i:i+4], 'little')
        i += 4
        if offset == 0:
            break
        offsets.append(offset)
        name_end = data.find(b'\x00', i)
        assert name_end != -1
        name = data[i:name_end].decode('ascii')
        i = name_end + 1
        names.append(name)
    offsets.append(len(data))
    assert i == offsets[0], f'Header size mismatch: {i} != {offsets[0]}.'
    assert offsets == sorted(offsets)
    sizes = [offsets[j+1] - offsets[j] for j in range(len(offsets)-1)]
    for i in range(len(sizes)):
        chunk = data[offsets[i]:offsets[i]+sizes[i]]
        chunks.append(chunk)
    return dict(zip(names, chunks))
