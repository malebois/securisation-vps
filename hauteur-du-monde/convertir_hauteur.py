#!/usr/bin/env python3
"""
convertir_hauteur.py : réduire la hauteur de l'overworld de Higher Heights (-2032 à 2032) sans rien perdre.

    python3 convertir_hauteur.py mesure    <monde> [--fond=Y]               lecture seule : rapport et fond conseillé
    python3 convertir_hauteur.py convertir <monde> <sortie> --fond=Y        écrit une dimension overworld neuve, vérifiée
    python3 convertir_hauteur.py datapack  <zip> <zip neuf> --fond=Y        le datapack Higher Heights à la même hauteur

Options : --processus=N (4 par défaut), --marge=B (blocs de vide voulus entre le hors-map et la carte, 64).

<monde> est le dossier qui contient level.dat (disposition 26.2 : dimensions/minecraft/overworld/...).
Le script ne modifie jamais <monde> : il lit, et n'écrit que dans <sortie> ou dans le zip neuf.

Le nouveau monde va de --fond (multiple de 16, -320 au plus haut) à 703. Dans chaque colonne de 16 x 16 :
  - la « carte » est ce qui est à y -320 ou plus haut : elle ne bouge pas d'un bloc ;
  - le « hors-map » est ce qui est sous y -320 (le sol plat d'origine à -2032 et ce qui est bâti dessus) :
    il monte d'un seul bloc de (fond + 2032) blocs, son sol arrive pile au nouveau fond, là où le jeu pose
    aussi le sol des chunks neufs ; sa lumière est retirée pour que le jeu la recalcule ;
  - un conflit (du contenu au-dessus de 703, ou un hors-map qui, remonté, toucherait la carte de sa colonne
    ou dépasserait 703) est listé par la mesure ; la conversion refuse de tourner tant qu'il en reste.
Les cartes de hauteur sont retirées partout : le jeu les recalcule (relues telles quelles, elles seraient
fausses sans le moindre message, voir le README de la fiche).
"""

import collections
import json
import multiprocessing
import os
import re
import shutil
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nbt_region import (  # noqa: E402
    BYTE, COMPOUND, DOUBLE, END, INT, INT_ARRAY, LIST, NbtError, RegionError,
    dumps, int_array, ints_of, loads, payload_bytes, read_gzip_file, read_region, region_coords,
    write_gzip_file, write_region,
)

# ==================== LES HAUTEURS ====================

OLD_MIN_SEC = -127            # Higher Heights 4064 : y = -2032
CUT_Y = -320                  # au-dessus : la carte, qui ne bouge pas ; en dessous : le hors-map, qui monte
CUT_SEC = CUT_Y // 16         # -20
NEW_MAX_Y = 703
NEW_MAX_SEC = NEW_MAX_Y // 16  # 43

# Réglés par set_fond (dans chaque processus aussi).
NEW_MIN_Y = NEW_MIN_SEC = NEW_SECTIONS = NEW_HEIGHT = SHIFT_SEC = SHIFT = None


def set_fond(fond):
    global NEW_MIN_Y, NEW_MIN_SEC, NEW_SECTIONS, NEW_HEIGHT, SHIFT_SEC, SHIFT
    if fond % 16 != 0 or fond > CUT_Y or fond < OLD_MIN_SEC * 16:
        raise ValueError("--fond doit être un multiple de 16 entre %d et %d" % (OLD_MIN_SEC * 16, CUT_Y))
    NEW_MIN_Y = fond
    NEW_MIN_SEC = fond // 16
    NEW_SECTIONS = NEW_MAX_SEC - NEW_MIN_SEC + 1
    NEW_HEIGHT = NEW_SECTIONS * 16
    SHIFT_SEC = NEW_MIN_SEC - OLD_MIN_SEC
    SHIFT = SHIFT_SEC * 16


AIR = {b"minecraft:air", b"minecraft:cave_air", b"minecraft:void_air"}
DIM = os.path.join("dimensions", "minecraft", "overworld")
FOLDERS = ("region", "entities", "poi")
CONFLICTS = ("haut", "trop-haut", "chevauchement")
MOVED = ("hors-map", "mixte")
# Les positions en blocs que portent certaines entités (cadres, lits, ruches...) : remontées avec elles.
ENTITY_BLOCK_POS = (b"block_pos", b"sleeping_pos", b"home_pos", b"hive_pos", b"flower_pos", b"bound_pos", b"anchor_pos")


def log(msg):
    print(msg, flush=True)


def new_sec(y):
    return y + SHIFT_SEC if y < CUT_SEC else y


def new_block_y(y):
    return y + SHIFT if y < CUT_Y else y


def y_ok(y):
    return NEW_MIN_Y <= y <= NEW_MAX_Y


# ==================== LECTURE D'UN CHUNK ====================

def section_has_blocks(sec):
    bs = sec.get(b"block_states")
    if not bs or bs[0] != COMPOUND:
        return False
    pal = bs[1].get(b"palette")
    if not pal:
        return False
    for entry in pal[1][1]:
        name = entry.get(b"Name")
        if name and name[1] not in AIR:
            return True
    return False


def section_palette(sec):
    bs = sec.get(b"block_states")
    if not bs:
        return ()
    pal = bs[1].get(b"palette")
    return tuple(sorted(e[b"Name"][1].decode("latin-1") for e in pal[1][1])) if pal else ()


def extents(root):
    """(plus basse et plus haute tranche de hors-map, plus basse et plus haute tranche de carte), None si aucune."""
    secs = root.get(b"sections")
    lo, hi = [], []
    if secs:
        for s in secs[1][1]:
            if b"Y" in s and section_has_blocks(s):
                (lo if s[b"Y"][1] < CUT_SEC else hi).append(s[b"Y"][1])
    return (min(lo) if lo else None, max(lo) if lo else None, min(hi) if hi else None, max(hi) if hi else None)


def classify(ext):
    lo_min, lo_max, hi_min, hi_max = ext
    if lo_max is None and hi_max is None:
        return "vide"
    if hi_max is not None and hi_max > NEW_MAX_SEC:
        return "haut"
    if lo_max is None:
        return "carte"
    if lo_max + SHIFT_SEC > NEW_MAX_SEC:
        return "trop-haut"
    if hi_min is None:
        return "hors-map"
    return "mixte" if lo_max + SHIFT_SEC < hi_min else "chevauchement"


def chunk_xz(rx, rz, i):
    return rx * 32 + (i % 32), rz * 32 + (i // 32)


# ==================== TRANSFORMATIONS ====================

def _reindex_lists(root, key, old_min, moved):
    """PostProcessing : une liste par tranche, comptée depuis le bas du monde. Rend ce qui est perdu."""
    pp = root.get(key)
    if not pp or pp[0] != LIST:
        return 0
    lists = pp[1][1]
    empty_type = END
    for inner in lists:
        if not inner[1]:
            empty_type = inner[0]
            break
    out = [(empty_type, []) for _ in range(NEW_SECTIONS)]
    lost = 0
    for j, inner in enumerate(lists):
        if not inner[1]:
            continue
        y = old_min + j
        idx = (new_sec(y) if moved else y) - NEW_MIN_SEC
        if 0 <= idx < NEW_SECTIONS:
            out[idx] = inner
        else:
            lost += 1
    root[key] = (LIST, (LIST, out))
    return lost


def _reindex_upgrade(root, old_min, moved):
    up = root.get(b"UpgradeData")
    if not up or up[0] != COMPOUND:
        return 0
    idx = up[1].get(b"Indices")
    if not idx or idx[0] != COMPOUND or not idx[1]:
        return 0
    out = {}
    lost = 0
    for k, v in idx[1].items():
        try:
            y = old_min + int(k.decode("latin-1"))
        except ValueError:
            continue
        n = (new_sec(y) if moved else y) - NEW_MIN_SEC
        if 0 <= n < NEW_SECTIONS:
            out[str(n).encode()] = v
        else:
            lost += 1
    up[1][b"Indices"] = (COMPOUND, out)
    return lost


def _move_positions(root, key, moved):
    """Une liste de compounds avec x, y, z (blocs à entité, ticks) : le hors-map monte, ce qui sort est retiré."""
    lst = root.get(key)
    if not lst or lst[0] != LIST:
        return []
    et, items = lst[1]
    kept, dropped = [], []
    for it in items:
        y = it.get(b"y")
        if y is None or y[0] != INT:
            kept.append(it)
            continue
        ny = new_block_y(y[1]) if moved else y[1]
        if y_ok(ny):
            it[b"y"] = (INT, ny)
            kept.append(it)
        else:
            dropped.append(it)
    root[key] = (LIST, (et, kept))
    return dropped


def entity_y(e):
    pos = e.get(b"Pos")
    if pos and pos[0] == LIST and pos[1][0] == DOUBLE and len(pos[1][1]) == 3:
        return pos[1][1][1]
    return None


def shift_entity(e, shift):
    pos = e.get(b"Pos")
    if pos and pos[0] == LIST and pos[1][0] == DOUBLE and len(pos[1][1]) == 3:
        pos[1][1][1] += shift
    origin = e.get(b"Paper.Origin")
    if origin and origin[0] == LIST and origin[1][0] == DOUBLE and len(origin[1][1]) == 3:
        origin[1][1][1] += shift
    tile = e.get(b"TileY")
    if tile and tile[0] == INT:
        e[b"TileY"] = (INT, tile[1] + shift)
    for k in ENTITY_BLOCK_POS:
        v = e.get(k)
        if v and v[0] == INT_ARRAY and v[1][0] == 3:
            x, y, z = ints_of(v[1])
            e[k] = (INT_ARRAY, int_array([x, y + shift, z]))
    pas = e.get(b"Passengers")
    if pas and pas[0] == LIST and pas[1][0] == COMPOUND:
        for p in pas[1][1]:
            shift_entity(p, shift)


def _strip_light(sec):
    for k in list(sec):
        if k in (b"BlockLight", b"SkyLight") or k.startswith(b"starlight."):
            del sec[k]


def convert_chunk(root, cls, notes):
    """Transforme le compound d'un chunk sur place. cls : 'carte', 'vide', 'hors-map' ou 'mixte'."""
    old_min = root[b"yPos"][1] if b"yPos" in root and root[b"yPos"][0] == INT else OLD_MIN_SEC
    moved = cls in MOVED
    root[b"yPos"] = (INT, NEW_MIN_SEC)
    root.pop(b"Heightmaps", None)
    if root.pop(b"carving_mask", None) is not None:
        notes["carving_mask"] += 1
    secs = root.get(b"sections")
    if secs:
        if not moved:
            out = []
            for s in secs[1][1]:
                if b"Y" not in s:
                    continue
                y = s[b"Y"][1]
                if NEW_MIN_SEC <= y <= NEW_MAX_SEC:
                    out.append(s)
                elif y == NEW_MIN_SEC - 1 or y == NEW_MAX_SEC + 1:
                    # Les tranches de lumière juste sous le fond et juste sur le plafond, comme le jeu les garde.
                    light = {k: v for k, v in s.items() if k not in (b"block_states", b"biomes")}
                    if len(light) > 1:
                        out.append(light)
        else:
            chosen = {}
            for s in secs[1][1]:
                if b"Y" not in s:
                    continue
                y = s[b"Y"][1]
                ny = new_sec(y)
                _strip_light(s)
                if not (NEW_MIN_SEC <= ny <= NEW_MAX_SEC):
                    continue
                full = section_has_blocks(s)
                is_map = y >= CUT_SEC
                prev = chosen.get(ny)
                if prev is not None:
                    if prev[1] and full:
                        raise RuntimeError("deux tranches pleines au même endroit (tranche %d)" % ny)
                    # Une tranche pleine l'emporte ; entre deux vides, celle de la carte garde ses biomes.
                    take = (full and not prev[1]) or (not full and not prev[1] and is_map and not prev[2])
                    if not take:
                        continue
                s[b"Y"] = (BYTE, ny)
                chosen[ny] = (s, full, is_map)
            out = [chosen[k][0] for k in sorted(chosen)]
            root[b"isLightOn"] = (BYTE, 0)
            for k in list(root):
                if k.startswith(b"starlight."):
                    del root[k]
        root[b"sections"] = (LIST, (COMPOUND, out))
    notes["postprocessing_perdu"] += _reindex_lists(root, b"PostProcessing", old_min, moved)
    notes["upgrade_perdu"] += _reindex_upgrade(root, old_min, moved)
    notes["blocs_entites_retires"] += len(_move_positions(root, b"block_entities", moved))
    for key in (b"block_ticks", b"fluid_ticks"):
        notes["ticks_retires"] += len(_move_positions(root, key, moved))
    ents = root.get(b"entities")
    if moved and ents and ents[0] == LIST and ents[1][0] == COMPOUND:
        for e in ents[1][1]:
            y = entity_y(e)
            if y is not None and y < CUT_Y:
                shift_entity(e, SHIFT)


# ==================== UNE RÉGION (TRAVAIL D'UN PROCESSUS) ====================

def _block_entities(root):
    be = root.get(b"block_entities")
    if not be or be[0] != LIST:
        return []
    out = []
    for it in be[1][1]:
        bid = it.get(b"id")
        x, y, z = (it.get(k, (INT, 0))[1] for k in (b"x", b"y", b"z"))
        cmd = it.get(b"Command")
        out.append((bid[1].decode("latin-1") if bid else "?", x, y, z, cmd[1].decode("utf-8", "replace") if cmd else None))
    return out


def measure_region(args):
    dim, name = args
    rx, rz = region_coords(name)
    res = {"region": name, "chunks": [], "errors": [], "entities": [], "ypos": collections.Counter(),
           "status": collections.Counter(), "notes": collections.Counter()}
    try:
        chunks = read_region(os.path.join(dim, "region", name))
    except (RegionError, OSError, EOFError) as ex:
        res["errors"].append("%s : %s" % (name, ex))
        return res
    classes = {}
    for i, (_, _, raw) in chunks.items():
        cx, cz = chunk_xz(rx, rz, i)
        try:
            _, root = loads(raw)
        except (NbtError, IndexError, ValueError) as ex:
            res["errors"].append("%s chunk %d %d : %s" % (name, cx, cz, ex))
            continue
        ext = extents(root)
        cls = classify(ext)
        classes[i] = cls
        st = root.get(b"Status", (0, b"?"))[1].decode("latin-1")
        res["status"][(cls, st == "minecraft:full")] += 1
        res["ypos"][root.get(b"yPos", (0, None))[1]] += 1
        if root.get(b"blending_data"):
            res["notes"]["blending_data"] += 1
        if root.get(b"below_zero_retrogen"):
            res["notes"]["below_zero_retrogen"] += 1
        structs = root.get(b"structures")
        if structs and structs[1].get(b"starts") and structs[1][b"starts"][1]:
            res["notes"]["structures:" + cls] += 1
        info = {"x": cx, "z": cz, "cls": cls, "ext": ext}
        if ext[0] is not None:
            secs = {s[b"Y"][1]: s for s in root[b"sections"][1][1] if b"Y" in s}
            info["floor"] = (ext[0], section_palette(secs[ext[0]]))
            info["nlo"] = sum(1 for y, s in secs.items() if y < CUT_SEC and section_has_blocks(s))
        bes = _block_entities(root)
        low = [b for b in bes if b[2] < CUT_Y]
        if low:
            info["be"] = low
        high_out = [b for b in bes if b[2] >= CUT_Y and b[2] > NEW_MAX_Y]
        if high_out:
            info["be_out"] = high_out
        res["chunks"].append(info)
    # Les entités de la même région, rangées par chunk.
    epath = os.path.join(dim, "entities", name)
    if os.path.exists(epath):
        try:
            for i, (_, _, raw) in read_region(epath).items():
                cx, cz = chunk_xz(rx, rz, i)
                _, root = loads(raw)
                ents = root.get(b"Entities")
                if not ents:
                    continue
                for e in ents[1][1]:
                    eid = e.get(b"id", (0, b"?"))[1].decode("latin-1")
                    res["entities"].append((cx, cz, classes.get(i, "vide"), eid, entity_y(e)))
        except (RegionError, NbtError, OSError, EOFError, IndexError, ValueError) as ex:
            res["errors"].append("entités %s : %s" % (name, ex))
    return res


def convert_region(args):
    dim, out_dim, name = args
    rx, rz = region_coords(name)
    notes = collections.Counter()
    problems = []
    chunks = read_region(os.path.join(dim, "region", name))
    classes, new_chunks, originals, moved = {}, {}, {}, []
    for i, (ts, _, raw) in chunks.items():
        root_name, root = loads(raw)
        cls = classify(extents(root))
        if cls in CONFLICTS:
            cx, cz = chunk_xz(rx, rz, i)
            raise RuntimeError("conflit non réglé : chunk %d %d (%s) dans %s" % (cx, cz, cls, name))
        classes[i] = cls
        originals[i] = raw
        secs = root.get(b"sections")
        notes["tranches_avant"] += len(secs[1][1]) if secs else 0
        convert_chunk(root, cls, notes)
        new_raw = dumps(root_name, root)
        new_chunks[i] = (ts, new_raw)
        secs = root.get(b"sections")
        notes["tranches_apres"] += len(secs[1][1]) if secs else 0
        notes["octets_avant"] += len(raw)
        notes["octets_apres"] += len(new_raw)
        notes["chunk:" + cls] += 1
        if cls in MOVED:
            moved.append(chunk_xz(rx, rz, i))
    write_region(os.path.join(out_dim, "region", name), new_chunks)

    # Entités : celles du hors-map montent avec leur partie de colonne, les autres ne bougent pas d'un octet.
    epath = os.path.join(dim, "entities", name)
    ent_counts = {}
    if os.path.exists(epath):
        out = {}
        for i, (ts, _, raw) in read_region(epath).items():
            name_e, root = loads(raw)
            ents = root.get(b"Entities")
            ent_counts[i] = len(ents[1][1]) if ents else 0
            if classes.get(i) in MOVED and ents and ents[0] == LIST:
                for e in ents[1][1]:
                    y = entity_y(e)
                    if y is not None and y < CUT_Y:
                        shift_entity(e, SHIFT)
                        notes["entites_remontees"] += 1
                out[i] = (ts, dumps(name_e, root))
            else:
                out[i] = (ts, raw)
        write_region(os.path.join(out_dim, "entities", name), out)

    # Points d'intérêt (lits, postes de travail, ruches...) : ceux des colonnes remontées sont retirés, le jeu
    # les reconstruit depuis les blocs au chargement ; les autres ne bougent pas (le jeu ignore ce qui sort).
    ppath = os.path.join(dim, "poi", name)
    if os.path.exists(ppath):
        out = {}
        for i, (ts, _, raw) in read_region(ppath).items():
            if classes.get(i) in MOVED:
                notes["poi_reconstruits"] += 1
                continue
            out[i] = (ts, raw)
        if out:
            write_region(os.path.join(out_dim, "poi", name), out)

    # Vérification : on relit ce qui vient d'être écrit et on compare tranche par tranche.
    check = read_region(os.path.join(out_dim, "region", name))
    if set(check) != set(chunks):
        problems.append("%s : chunks manquants ou en trop après écriture" % name)
    for i, (_, _, raw) in check.items():
        _, new = loads(raw)
        _, old = loads(originals[i])
        is_moved = classes[i] in MOVED
        new_secs = {s[b"Y"][1]: s for s in new[b"sections"][1][1]} if new.get(b"sections") else {}
        for s in (old[b"sections"][1][1] if old.get(b"sections") else []):
            if b"Y" not in s:
                continue
            y = s[b"Y"][1]
            ny = new_sec(y) if is_moved else y
            full = section_has_blocks(s)
            if not (NEW_MIN_SEC <= ny <= NEW_MAX_SEC):
                if full:
                    problems.append("%s chunk %d : tranche %d avec des blocs perdue" % (name, i, y))
                continue
            if is_moved and not full:
                continue
            n = new_secs.get(ny)
            for k in (b"block_states", b"biomes"):
                a, b = s.get(k), (n.get(k) if n else None)
                if (a is None) != (b is None) or (a and payload_bytes(*a) != payload_bytes(*b)):
                    problems.append("%s chunk %d : tranche %d -> %d différente (%s)" % (name, i, y, ny, k.decode()))
        if new.get(b"Heightmaps"):
            problems.append("%s chunk %d : cartes de hauteur restées" % (name, i))
        if new[b"yPos"][1] != NEW_MIN_SEC:
            problems.append("%s chunk %d : yPos %s" % (name, i, new[b"yPos"][1]))
        be_old = old.get(b"block_entities")
        be_new = new.get(b"block_entities")
        if (len(be_old[1][1]) if be_old else 0) != (len(be_new[1][1]) if be_new else 0):
            problems.append("%s chunk %d : nombre de blocs à entité changé" % (name, i))
    echeck = os.path.join(out_dim, "entities", name)
    if os.path.exists(echeck):
        for i, (_, _, raw) in read_region(echeck).items():
            _, root = loads(raw)
            ents = root.get(b"Entities")
            if (len(ents[1][1]) if ents else 0) != ent_counts.get(i, -1):
                problems.append("%s chunk %d : nombre d'entités changé" % (name, i))
    return {"region": name, "notes": notes, "problems": problems, "moved": moved}


# ==================== RAPPORT DE MESURE ====================

def _zones(cells):
    """Regroupe des chunks voisins (8 directions) en zones : liste de listes de (x, z)."""
    left = set(cells)
    zones = []
    while left:
        start = left.pop()
        stack, zone = [start], [start]
        while stack:
            x, z = stack.pop()
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    n = (x + dx, z + dz)
                    if n in left:
                        left.remove(n)
                        stack.append(n)
                        zone.append(n)
        zones.append(zone)
    zones.sort(key=len, reverse=True)
    return zones


def _players(world, moved):
    out = []
    pd = os.path.join(world, "players", "data")
    if not os.path.isdir(pd):
        return out, "dossier players/data introuvable"
    for f in sorted(os.listdir(pd)):
        if not f.endswith(".dat"):
            continue
        try:
            _, root = read_gzip_file(os.path.join(pd, f))
        except Exception as ex:  # noqa: BLE001 - un fichier illisible est signalé, jamais ignoré
            out.append((f, None, None, "illisible : %s" % ex))
            continue
        dim = root.get(b"Dimension", (0, b"minecraft:overworld"))[1].decode("latin-1")
        pos = root.get(b"Pos")
        if not pos:
            out.append((f, dim, None, "pas de position"))
            continue
        x, y, z = pos[1][1]
        if dim != "minecraft:overworld" or (y >= CUT_Y and y_ok(int(y // 1))):
            continue
        cell = (int(x // 16), int(z // 16))
        if y < CUT_Y and cell in moved:
            what = "dans le hors-map, remonté avec lui (arrivera en y %.0f)" % (y + SHIFT)
        else:
            what = "hors de la plage, posé au spawn"
        out.append((f, dim, (round(x, 1), round(y, 1), round(z, 1)), what))
    return out, None


def _zone_lines(cells_info, limit):
    L = []
    zones = _zones(list(cells_info))
    for zone in zones[:limit]:
        xs = [p[0] for p in zone]
        zs = [p[1] for p in zone]
        yhi = max(cells_info[p] for p in zone)
        L.append("    x %d à %d, z %d à %d : %d chunk(s), jusqu'à y %d (arrivera à y %d)" % (
            min(xs) * 16, max(xs) * 16 + 15, min(zs) * 16, max(zs) * 16 + 15, len(zone), yhi * 16 + 15, yhi * 16 + 15 + SHIFT))
    if len(zones) > limit:
        L.append("    ... et %d petites zones de plus" % (len(zones) - limit))
    return L, len(zones)


def cmd_mesure(world, procs, marge):
    dim = os.path.join(world, DIM)
    regions = sorted(f for f in os.listdir(os.path.join(dim, "region")) if region_coords(f))
    log("Mesure de %d fichiers de région dans %s (%d processus, lecture seule)." % (len(regions), dim, procs))
    log("Fond étudié : y %d (le rapport conseille aussi un fond)." % NEW_MIN_Y)
    t0 = time.time()
    all_chunks, errors, entities = [], [], []
    ypos, status, notes = collections.Counter(), collections.Counter(), collections.Counter()
    with multiprocessing.Pool(procs, initializer=set_fond, initargs=(NEW_MIN_Y,)) as pool:
        for k, res in enumerate(pool.imap_unordered(measure_region, [(dim, r) for r in regions]), 1):
            all_chunks += res["chunks"]
            errors += res["errors"]
            entities += res["entities"]
            ypos.update(res["ypos"])
            status.update(res["status"])
            notes.update(res["notes"])
            if k % 50 == 0 or k == len(regions):
                log("  %d / %d régions (%.0f s)" % (k, len(regions), time.time() - t0))

    L = []
    L.append("RAPPORT DE MESURE : %s" % world)
    L.append("La carte (y %d et plus) ne bouge pas ; le hors-map (sous y %d) monte de (fond + 2032) blocs. Plafond : %d." % (CUT_Y, CUT_Y, NEW_MAX_Y))
    L.append("")

    # Le fond conseillé : le plus haut possible sans qu'aucun hors-map remonté ne touche la carte de sa colonne,
    # avec la marge demandée sous le point le plus bas de toute la carte.
    lo_all = [c["ext"][1] for c in all_chunks if c["ext"][1] is not None]
    hi_all = [c["ext"][2] for c in all_chunks if c["ext"][2] is not None]
    hi_top = max([c["ext"][3] for c in all_chunks if c["ext"][3] is not None], default=None)
    L.append("FOND")
    if lo_all and hi_all:
        lo_top, hi_bottom = max(lo_all), min(hi_all)
        tech = min([c["ext"][2] - c["ext"][1] - 1 for c in all_chunks if c["ext"][1] is not None and c["ext"][2] is not None], default=999)
        cap = NEW_MAX_SEC - lo_top
        shift_tech = min(tech, cap, CUT_SEC - OLD_MIN_SEC)
        shift_rec = min(shift_tech, hi_bottom - lo_top - 1 - (marge + 15) // 16)
        L.append("  Haut du hors-map : y %d (tranche %d). Bas de la carte : y %d (tranche %d). Haut de la carte : y %d." % (
            lo_top * 16 + 15, lo_top, hi_bottom * 16, hi_bottom, hi_top * 16 + 15))
        if shift_tech < 0:
            L.append("  ! Aucun fond ne sépare le hors-map de la carte dans certaines colonnes (voir CONFLITS).")
        else:
            for label, sh in (("Le plus haut possible, sans aucun chevauchement", shift_tech),
                              ("Conseillé, avec %d blocs de vide entre le haut du hors-map et le bas de la carte" % marge, shift_rec)):
                if sh < 0:
                    continue
                f = (OLD_MIN_SEC + sh) * 16
                L.append("  %s : --fond=%d (%d tranches de haut, contre 254 aujourd'hui)." % (label, f, NEW_MAX_SEC - (OLD_MIN_SEC + sh) + 1))
    L.append("")

    by_cls = collections.Counter(c["cls"] for c in all_chunks)
    L.append("CHUNKS avec --fond=%d : %d au total" % (NEW_MIN_Y, len(all_chunks)))
    for cls in ("carte", "hors-map", "mixte", "vide") + CONFLICTS:
        L.append("  %-14s %8d   (dont %d pas finis de générer)" % (cls, by_cls[cls], by_cls[cls] - status[(cls, True)]))
    L.append("  (mixte : carte et hors-map dans la même colonne, séparés : la carte reste, le hors-map monte dessous)")
    L.append("  yPos lus : %s" % dict(ypos))
    L.append("")

    moved_cells = set((c["x"], c["z"]) for c in all_chunks if c["cls"] in MOVED)
    low = [c for c in all_chunks if c["ext"][1] is not None and c["cls"] in MOVED]
    if low:
        floors = collections.Counter(c["floor"] for c in low if c["nlo"] == 1)
        floor = floors.most_common(1)[0][0] if floors else None
        L.append("HORS-MAP : %d colonnes remontées de %d blocs." % (len(low), SHIFT))
        if floor:
            L.append("  Sol plat le plus courant : y %d à %d, blocs %s, dans %d chunks. Après remontée : y %d à %d." % (
                floor[0] * 16, floor[0] * 16 + 15, ", ".join(floor[1]), floors[floor], floor[0] * 16 + SHIFT, floor[0] * 16 + 15 + SHIFT))
        built = {(c["x"], c["z"]): c["ext"][1] for c in low if c["nlo"] > 1 or (floor and c["floor"] != floor)}
        lines, nz = _zone_lines(built, 60)
        L.append("  %d colonnes portent autre chose que le sol plat, en %d zone(s) :" % (len(built), nz))
        L += lines
        bes = collections.Counter(b[0] for c in low for b in c.get("be", []))
        if bes:
            L.append("  Blocs à entité qui montent avec : %s" % dict(bes))
        for b in [b for c in low for b in c.get("be", []) if b[4]][:30]:
            L.append("  ! Bloc de commande en %d %d %d : %s" % (b[1], b[2], b[3], b[4][:120]))
    mixed = [c for c in all_chunks if c["cls"] == "mixte"]
    if mixed:
        gaps = [(c["ext"][2] - (c["ext"][1] + SHIFT_SEC)) * 16 for c in mixed]
        L.append("  Colonnes mixtes : %d ; vide entre le hors-map remonté et la carte au-dessus : %d à %d blocs." % (len(mixed), min(gaps), max(gaps)))
    L.append("")

    be_out = [b for c in all_chunks for b in c.get("be_out", [])]
    if be_out:
        L.append("BLOCS À ENTITÉ AU-DESSUS DE %d (seraient retirés) : %d" % (NEW_MAX_Y, len(be_out)))
        for b in be_out[:30]:
            L.append("  %s en %d %d %d" % b[:4])
        L.append("")

    conf = [c for c in all_chunks if c["cls"] in CONFLICTS]
    L.append("CONFLITS avec --fond=%d (la conversion refusera de tourner tant qu'il en reste) : %d" % (NEW_MIN_Y, len(conf)))
    kinds = collections.Counter(c["cls"] for c in conf)
    if conf:
        L.append("  %s" % dict(kinds))
        for kind in CONFLICTS:
            cells = {(c["x"], c["z"]): (c["ext"][1] if c["ext"][1] is not None else c["ext"][3]) for c in conf if c["cls"] == kind}
            if cells:
                L.append("  %s, par zone :" % kind)
                for zone in _zones(list(cells))[:40]:
                    xs = [p[0] for p in zone]
                    zs = [p[1] for p in zone]
                    exts = [next(c["ext"] for c in conf if (c["x"], c["z"]) == p) for p in zone[:200]]
                    lo = [e[1] for e in exts if e[1] is not None]
                    hi = [e[2] for e in exts if e[2] is not None]
                    L.append("    x %d à %d, z %d à %d : %d chunk(s) ; hors-map jusqu'à y %s, carte dès y %s" % (
                        min(xs) * 16, max(xs) * 16 + 15, min(zs) * 16, max(zs) * 16 + 15, len(zone),
                        (max(lo) * 16 + 15) if lo else "-", (min(hi) * 16) if hi else "-"))
    L.append("")

    ent_moved = collections.Counter(e[3] for e in entities if e[2] in MOVED and e[4] is not None and e[4] < CUT_Y)
    ent_out = [e for e in entities if e[4] is not None and not (e[2] in MOVED and e[4] < CUT_Y) and not (NEW_MIN_Y - 64 <= e[4] <= NEW_MAX_Y + 64)]
    L.append("ENTITÉS : %d en tout ; %d remontées avec le hors-map %s" % (len(entities), sum(ent_moved.values()), dict(ent_moved) if ent_moved else ""))
    if ent_out:
        L.append("  %d entité(s) hors plage ailleurs (tomberaient dans le vide) :" % len(ent_out))
        for e in ent_out[:30]:
            L.append("    %s dans le chunk %d %d, y %.1f" % (e[3], e[0], e[1], e[4]))
    L.append("")

    players, perr = _players(world, moved_cells)
    L.append("JOUEURS DÉCONNECTÉS sous y %d ou hors de la plage : %s" % (CUT_Y, perr or len(players)))
    for p in players:
        L.append("  %s %s %s : %s" % p)
    L.append("")

    if notes:
        L.append("DIVERS : %s" % dict(notes))
    L.append("ERREURS DE LECTURE : %d" % len(errors))
    for e in errors[:50]:
        L.append("  " + e)
    L.append("")
    L.append("Mesuré en %.0f s." % (time.time() - t0))
    text = "\n".join(L)
    log("")
    log(text)
    with open("rapport-mesure.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    log("\nRapport écrit dans %s" % os.path.abspath("rapport-mesure.txt"))
    return 0 if not errors else 1


# ==================== CONVERSION ====================

def cmd_convertir(world, out, procs):
    dim = os.path.join(world, DIM)
    if os.path.exists(out):
        log("ARRÊT : %s existe déjà. Le script n'écrit que dans un dossier neuf." % out)
        return 1
    out_dim = os.path.join(out, "overworld")
    # Tout ce qui n'est pas région, entités ou points d'intérêt est recopié tel quel (data, paper-world.yml...).
    shutil.copytree(dim, out_dim, ignore=lambda d, names: [n for n in names if d == dim and n in FOLDERS])
    for f in FOLDERS:
        os.makedirs(os.path.join(out_dim, f), exist_ok=True)
    regions = sorted(f for f in os.listdir(os.path.join(dim, "region")) if region_coords(f))
    # Une région d'entités ou de points d'intérêt sans région de blocs est recopiée telle quelle.
    for f in ("entities", "poi"):
        src = os.path.join(dim, f)
        if os.path.isdir(src):
            for name in os.listdir(src):
                if name not in regions:
                    shutil.copy2(os.path.join(src, name), os.path.join(out_dim, f, name))
    log("Conversion de %d régions de %s vers %s (%d processus), fond y %d, hors-map remonté de %d blocs." % (
        len(regions), dim, out_dim, procs, NEW_MIN_Y, SHIFT))
    t0 = time.time()
    notes, problems, moved = collections.Counter(), [], set()
    try:
        with multiprocessing.Pool(procs, initializer=set_fond, initargs=(NEW_MIN_Y,)) as pool:
            for k, res in enumerate(pool.imap_unordered(convert_region, [(dim, out_dim, r) for r in regions]), 1):
                notes.update(res["notes"])
                problems += res["problems"]
                moved.update(res["moved"])
                if k % 50 == 0 or k == len(regions):
                    log("  %d / %d régions (%.0f s)" % (k, len(regions), time.time() - t0))
    except Exception as ex:  # noqa: BLE001 - tout arrêt est dit en clair
        log("")
        log("ARRÊT DE LA CONVERSION : %s" % ex)
        log("Le monde d'origine n'a pas été touché. Le dossier %s est incomplet : à supprimer à la main." % out)
        return 3

    # Les joueurs déconnectés sous la carte ou hors de la plage : copies corrigées dans <sortie>/joueurs.
    pd = os.path.join(world, "players", "data")
    fixed = 0
    if os.path.isdir(pd):
        spawn = None
        try:
            _, lvl = read_gzip_file(os.path.join(world, "level.dat"))
            sp = lvl[b"Data"][1].get(b"spawn")
            if sp:
                spawn = ints_of(sp[1][b"pos"][1])
        except Exception:  # noqa: BLE001
            spawn = None
        for f in sorted(os.listdir(pd)):
            if not f.endswith(".dat"):
                continue
            name, root = read_gzip_file(os.path.join(pd, f))
            dimn = root.get(b"Dimension", (0, b"minecraft:overworld"))[1]
            pos = root.get(b"Pos")
            if dimn != b"minecraft:overworld" or not pos:
                continue
            x, y, z = pos[1][1]
            if y >= CUT_Y and y_ok(int(y // 1)):
                continue
            if y < CUT_Y and (int(x // 16), int(z // 16)) in moved and y_ok(int((y + SHIFT) // 1)):
                shift_entity(root, SHIFT)
                what = "remonté de %d avec le hors-map" % SHIFT
            elif spawn:
                pos[1][1][0], pos[1][1][1], pos[1][1][2] = spawn[0] + 0.5, float(spawn[1]), spawn[2] + 0.5
                what = "posé au spawn %d %d %d" % tuple(spawn)
            else:
                problems.append("joueur %s hors plage et spawn introuvable" % f)
                continue
            os.makedirs(os.path.join(out, "joueurs"), exist_ok=True)
            write_gzip_file(os.path.join(out, "joueurs", f), name, root)
            fixed += 1
            log("  joueur %s : %s" % (f, what))

    L = ["", "CONVERSION TERMINÉE en %.0f s (fond y %d)" % (time.time() - t0, NEW_MIN_Y)]
    n = sum(v for k, v in notes.items() if k.startswith("chunk:"))
    if n and notes["octets_avant"]:
        L.append("  Par chunk, en moyenne : %.1f Ko à lire et %.0f tranches avant ; %.1f Ko et %.0f tranches après (%d %% du poids)." % (
            notes["octets_avant"] / n / 1024, notes["tranches_avant"] / n, notes["octets_apres"] / n / 1024, notes["tranches_apres"] / n,
            round(100 * notes["octets_apres"] / notes["octets_avant"])))
    for k in sorted(notes):
        L.append("  %s : %d" % (k, notes[k]))
    L.append("  joueurs corrigés : %d%s" % (fixed, " (copies dans %s, à poser dans players/data)" % os.path.join(out, "joueurs") if fixed else ""))
    L.append("VÉRIFICATION : %s" % ("aucun écart" if not problems else "%d ÉCART(S), NE PAS UTILISER CE RÉSULTAT" % len(problems)))
    for p in problems[:100]:
        L.append("  " + p)
    text = "\n".join(L)
    log(text)
    with open(os.path.join(out, "rapport-conversion.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return 0 if not problems else 2


# ==================== DATAPACK ====================

def cmd_datapack(src, dst):
    if os.path.exists(dst):
        log("ARRÊT : %s existe déjà." % dst)
        return 1
    target = "data/minecraft/dimension_type/overworld.json"
    with zipfile.ZipFile(src) as zin:
        text = zin.read(target).decode("utf-8")
        new = re.sub(r'("min_y"\s*:\s*)-?\d+', r"\g<1>%d" % NEW_MIN_Y, text, count=1)
        new = re.sub(r'("height"\s*:\s*)\d+', r"\g<1>%d" % NEW_HEIGHT, new, count=1)
        data = json.loads(new)
        if data["min_y"] != NEW_MIN_Y or data["height"] != NEW_HEIGHT or data.get("logical_height", 0) > NEW_HEIGHT:
            log("ARRÊT : le fichier modifié ne donne pas la hauteur attendue : %s" % {k: data.get(k) for k in ("min_y", "height", "logical_height")})
            return 1
        with zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                raw = new.encode("utf-8") if info.filename == target else zin.read(info.filename)
                zout.writestr(info, raw, compress_type=info.compress_type)
    with zipfile.ZipFile(dst) as z:
        check = json.loads(z.read(target))
        others = len(z.infolist())
    log("Datapack écrit : %s (%d fichiers). overworld : min_y %d, height %d (jusqu'à y %d), logical_height %d." % (
        dst, others, check["min_y"], check["height"], check["min_y"] + check["height"] - 1, check["logical_height"]))
    return 0


# ==================== ENTRÉE ====================

def main(argv):
    procs = max(1, min(4, os.cpu_count() or 1))
    fond = None
    marge = 64
    args = []
    for a in argv[1:]:
        if a.startswith("--processus="):
            procs = max(1, int(a.split("=", 1)[1]))
        elif a.startswith("--fond="):
            fond = int(a.split("=", 1)[1])
        elif a.startswith("--marge="):
            marge = max(0, int(a.split("=", 1)[1]))
        else:
            args.append(a)
    if args and args[0] in ("convertir", "datapack") and fond is None:
        log("ARRÊT : --fond=Y est obligatoire pour %s (le même pour la conversion et le datapack)." % args[0])
        return 1
    try:
        set_fond(CUT_Y if fond is None else fond)
    except ValueError as ex:
        log("ARRÊT : %s" % ex)
        return 1
    if len(args) >= 2 and args[0] == "mesure":
        return cmd_mesure(args[1], procs, marge)
    if len(args) >= 3 and args[0] == "convertir":
        return cmd_convertir(args[1], args[2], procs)
    if len(args) >= 3 and args[0] == "datapack":
        return cmd_datapack(args[1], args[2])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
