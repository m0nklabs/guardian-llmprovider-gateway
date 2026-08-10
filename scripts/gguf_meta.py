#!/usr/bin/env python3
"""Print GGUF metadata keys related to context/rope for a model file."""
import struct
import sys


def read_str(f):
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def main(path: str) -> None:
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            raise SystemExit("not a GGUF file")
        (_version,) = struct.unpack("<I", f.read(4))
        _n_tensors, n_kv = struct.unpack("<QQ", f.read(16))
        scalars = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
                   4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<B", 1),
                   10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
        for _ in range(n_kv):
            key = read_str(f)
            vtype = struct.unpack("<I", f.read(4))[0]
            if vtype == 8:
                val = read_str(f)
            elif vtype == 9:
                etype = struct.unpack("<I", f.read(4))[0]
                n = struct.unpack("<Q", f.read(8))[0]
                if etype == 8:
                    val = [read_str(f) for _ in range(min(n, 3))]
                    for _ in range(n - len(val)):
                        read_str(f)
                    val = f"{val} ... ({n} items)"
                else:
                    f.seek(scalars[etype][1] * n, 1)
                    val = f"<array[{n}] type={etype}>"
            elif vtype in scalars:
                fmt, size = scalars[vtype]
                val = struct.unpack(fmt, f.read(size))[0]
            else:
                raise SystemExit(f"unknown vtype {vtype}")
            kl = key.lower()
            if "--all" in sys.argv or any(
                s in kl for s in ("context_length", "rope", "yarn", "max_position")
            ):
                print(f"{key} = {val}")


if __name__ == "__main__":
    main(sys.argv[1])
