"""
Lecture et écriture exactes du NBT et des fichiers de région de Minecraft (Java), sans dépendance.

Le NBT est relu tel quel puis réécrit octet pour octet : les noms et les chaînes restent des octets
bruts (UTF-8 modifié de Java, jamais décodé), les tableaux d'octets, d'entiers et de longs restent des
octets bruts. Une valeur est un couple (type, contenu) :

    1 octet, 2 short, 3 int, 4 long : int        5 float, 6 double : float
    7, 11, 12 tableaux : (longueur, octets bruts)  8 chaîne : octets bruts
    9 liste : (type des éléments, [contenus])      10 compound : dict {nom (octets) : (type, contenu)}

Python 3.11 suffit, sans dépendance externe.
"""

import gzip
import os
import struct
import zlib

END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = range(13)

_B = struct.Struct(">b")
_H = struct.Struct(">h")
_I = struct.Struct(">i")
_Q = struct.Struct(">q")
_F = struct.Struct(">f")
_D = struct.Struct(">d")
_US = struct.Struct(">H")


class NbtError(Exception):
    pass


# ==================== LECTURE ====================

def _read_payload(t, buf, pos):
    if t == COMPOUND:
        out = {}
        while True:
            ct = buf[pos]
            pos += 1
            if ct == END:
                return out, pos
            n = _US.unpack_from(buf, pos)[0]
            pos += 2
            name = bytes(buf[pos:pos + n])
            pos += n
            val, pos = _read_payload(ct, buf, pos)
            out[name] = (ct, val)
    if t == LIST:
        et = buf[pos]
        n = _I.unpack_from(buf, pos + 1)[0]
        pos += 5
        items = []
        for _ in range(max(0, n)):
            v, pos = _read_payload(et, buf, pos)
            items.append(v)
        return (et, items), pos
    if t == BYTE:
        return _B.unpack_from(buf, pos)[0], pos + 1
    if t == SHORT:
        return _H.unpack_from(buf, pos)[0], pos + 2
    if t == INT:
        return _I.unpack_from(buf, pos)[0], pos + 4
    if t == LONG:
        return _Q.unpack_from(buf, pos)[0], pos + 8
    if t == FLOAT:
        return _F.unpack_from(buf, pos)[0], pos + 4
    if t == DOUBLE:
        return _D.unpack_from(buf, pos)[0], pos + 8
    if t == STRING:
        n = _US.unpack_from(buf, pos)[0]
        pos += 2
        return bytes(buf[pos:pos + n]), pos + n
    if t in (BYTE_ARRAY, INT_ARRAY, LONG_ARRAY):
        n = _I.unpack_from(buf, pos)[0]
        size = n * (1 if t == BYTE_ARRAY else 4 if t == INT_ARRAY else 8)
        pos += 4
        return (n, bytes(buf[pos:pos + size])), pos + size
    raise NbtError("type NBT inconnu : %d" % t)


def loads(data):
    """Le compound racine d'un NBT non compressé : (nom, contenu)."""
    buf = memoryview(data)
    if buf[0] != COMPOUND:
        raise NbtError("la racine n'est pas un compound")
    n = _US.unpack_from(buf, 1)[0]
    name = bytes(buf[3:3 + n])
    val, pos = _read_payload(COMPOUND, buf, 3 + n)
    if pos != len(buf):
        raise NbtError("%d octets en trop après la racine" % (len(buf) - pos))
    return name, val


# ==================== ÉCRITURE ====================

def _write_payload(t, v, out):
    if t == COMPOUND:
        for name, (ct, cv) in v.items():
            out.append(bytes((ct,)))
            out.append(_US.pack(len(name)))
            out.append(name)
            _write_payload(ct, cv, out)
        out.append(b"\x00")
    elif t == LIST:
        et, items = v
        out.append(bytes((et,)))
        out.append(_I.pack(len(items)))
        for item in items:
            _write_payload(et, item, out)
    elif t == BYTE:
        out.append(_B.pack(v))
    elif t == SHORT:
        out.append(_H.pack(v))
    elif t == INT:
        out.append(_I.pack(v))
    elif t == LONG:
        out.append(_Q.pack(v))
    elif t == FLOAT:
        out.append(_F.pack(v))
    elif t == DOUBLE:
        out.append(_D.pack(v))
    elif t == STRING:
        out.append(_US.pack(len(v)))
        out.append(v)
    elif t in (BYTE_ARRAY, INT_ARRAY, LONG_ARRAY):
        n, raw = v
        out.append(_I.pack(n))
        out.append(raw)
    else:
        raise NbtError("type NBT inconnu : %d" % t)


def dumps(name, compound):
    out = [bytes((COMPOUND,)), _US.pack(len(name)), name]
    _write_payload(COMPOUND, compound, out)
    return b"".join(out)


def payload_bytes(t, v):
    """Le contenu sérialisé d'une valeur, pour comparer deux valeurs octet pour octet."""
    out = []
    _write_payload(t, v, out)
    return b"".join(out)


# ==================== FICHIERS NBT COMPRESSÉS (level.dat, joueurs) ====================

def read_gzip_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return loads(gzip.decompress(raw))
    except OSError:
        return loads(raw)


def write_gzip_file(path, name, compound):
    with open(path, "wb") as f:
        f.write(gzip.compress(dumps(name, compound), compresslevel=6, mtime=0))


# ==================== AIDES ====================

def ints_of(arr):
    """Le contenu d'un tableau d'entiers (type 11) en liste Python."""
    n, raw = arr
    return list(struct.unpack(">%di" % n, raw))


def int_array(values):
    return (len(values), struct.pack(">%di" % len(values), *values))


# ==================== FICHIERS DE RÉGION ====================

SECTOR = 4096


class RegionError(Exception):
    pass


def region_coords(filename):
    """r.<x>.<z>.mca : (x, z), ou None."""
    parts = filename.split(".")
    if len(parts) != 4 or parts[0] != "r" or parts[3] != "mca":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def read_region(path):
    """
    Les chunks d'un fichier de région : {index (x + z*32) : (horodatage, type de compression, NBT brut)}.
    Une compression inconnue (LZ4...) lève une RegionError : rien n'est jamais deviné.
    """
    with open(path, "rb") as f:
        data = f.read()
    chunks = {}
    if len(data) < 2 * SECTOR:
        return chunks
    folder = os.path.dirname(path)
    rc = region_coords(os.path.basename(path))
    for i in range(1024):
        loc = int.from_bytes(data[i * 4:i * 4 + 3], "big")
        count = data[i * 4 + 3]
        if loc == 0 or count == 0:
            continue
        ts = int.from_bytes(data[SECTOR + i * 4:SECTOR + i * 4 + 4], "big")
        start = loc * SECTOR
        if start + 5 > len(data):
            raise RegionError("%s : chunk %d hors du fichier" % (path, i))
        length = int.from_bytes(data[start:start + 4], "big")
        ctype = data[start + 4]
        if ctype & 128:
            if rc is None:
                raise RegionError("%s : chunk externe sans coordonnées de région" % path)
            cx, cz = rc[0] * 32 + (i % 32), rc[1] * 32 + (i // 32)
            with open(os.path.join(folder, "c.%d.%d.mcc" % (cx, cz)), "rb") as f:
                payload = f.read()
            ctype &= 127
        else:
            payload = data[start + 5:start + 4 + length]
        if ctype == 1:
            raw = gzip.decompress(payload)
        elif ctype == 2:
            raw = zlib.decompress(payload)
        elif ctype == 3:
            raw = bytes(payload)
        else:
            raise RegionError("%s : compression de type %d non gérée (LZ4 ?)" % (path, ctype))
        chunks[i] = (ts, ctype, raw)
    return chunks


def write_region(path, chunks):
    """
    Écrit un fichier de région neuf (compression zlib, comme le jeu par défaut). {index : (horodatage, NBT brut)}.
    Un chunk trop gros pour 255 secteurs part dans un fichier c.<x>.<z>.mcc à côté, comme le fait le jeu.
    """
    folder = os.path.dirname(path)
    rc = region_coords(os.path.basename(path))
    header = bytearray(2 * SECTOR)
    body = bytearray()
    sector = 2
    for i in sorted(chunks):
        ts, raw = chunks[i]
        comp = zlib.compress(raw, 6)
        record = struct.pack(">IB", len(comp) + 1, 2) + comp
        if (len(record) + SECTOR - 1) // SECTOR > 255:
            cx, cz = rc[0] * 32 + (i % 32), rc[1] * 32 + (i // 32)
            with open(os.path.join(folder, "c.%d.%d.mcc" % (cx, cz)), "wb") as f:
                f.write(comp)
            record = struct.pack(">IB", 1, 2 | 128)
        count = (len(record) + SECTOR - 1) // SECTOR
        header[i * 4:i * 4 + 4] = ((sector << 8) | count).to_bytes(4, "big")
        header[SECTOR + i * 4:SECTOR + i * 4 + 4] = ts.to_bytes(4, "big")
        body += record
        body += b"\x00" * (count * SECTOR - len(record))
        sector += count
    with open(path, "wb") as f:
        f.write(header)
        f.write(body)
