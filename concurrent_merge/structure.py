"""Conservative three-way merge of declaration extents.

lit's readers (concurrent_merge/readers) outline a file into units, and
tree_merge merges two edits of it unit by unit. A file with no reader, or one
its reader is unsure of, keeps Git's line merge, and a real overlap goes back
to the change that caused it.
"""

from . import readers


def choose(base, ours, theirs):
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    return None


def edit(base, side):
    """The one run of base pieces that side replaced, as (start, end, replacement)."""
    limit = min(len(base), len(side))
    head = 0
    while head < limit and base[head] == side[head]:
        head += 1
    tail = 0
    while tail < limit - head and base[-1 - tail] == side[-1 - tail]:
        tail += 1
    return head, len(base) - tail, side[head : len(side) - tail]


def slot_merge(base, ours, theirs, shared):
    """Merge the stretch between two kept units, or None.

    Each stretch is a list of pieces: whole lines of gap text, and ("unit",
    text) for each unit a side added or the base had. Each side's change is
    one replaced run of base pieces. Runs that are apart (not touching) both
    apply, as in Git's line merge. Insertions at the same point both apply,
    ours first, only when each adds a unit. A unit both sides added comes out
    once, so then one side's insertion must start or end with the other's.
    """
    same = choose(base, ours, theirs)
    if same is not None:
        return same
    (a1, b1, r1), (a2, b2, r2) = edit(base, ours), edit(base, theirs)
    units = lambda run: any(type(piece) is tuple for piece in run)
    if a1 == b1 == a2 == b2:
        if not (units(r1) and units(r2)):
            return None
        if shared:
            n1, n2 = len(r1), len(r2)
            if n1 <= n2 and r1 in (r2[:n1], r2[n2 - n1 :]):
                return base[:a1] + r2 + base[a1:]
            if n2 <= n1 and r2 in (r1[:n2], r1[n1 - n2 :]):
                return base[:a1] + r1 + base[a1:]
            return None
        return base[:a1] + r1 + r2 + base[a1:]
    if shared:
        return None
    if b1 < a2:
        return base[:a1] + r1 + base[b1:a2] + r2 + base[b2:]
    if b2 < a1:
        return base[:a2] + r2 + base[b2:a1] + r1 + base[b1:]
    return None


def tree_merge(parts):
    """Merge three (raw, node) outlines of one file, or None.

    Units present on all three sides are kept and matched by key; a kept unit
    only one side changed takes that side's text, one both changed the same
    way is taken once, and one both changed differently is merged one level
    down, failing if it has no children. The text between kept units (gaps,
    and units a side added or deleted) merges as one stretch per side, so a
    side's additions land where it put them, ours before theirs at the same
    spot. A unit one side deletes must be untouched on the other, a unit both
    add must be identical and come out once, base units must keep their order,
    and keys must be unique at every level merged.
    """
    same = choose(*(raw[node[1]:node[2]] for raw, node in parts))
    if same is not None:
        return same
    sides = []
    for raw, node in parts:
        if not node[3]:
            return None
        units = {}
        for child in node[3]:
            if child[0] in units:
                return None
            units[child[0]] = child
        sides.append((raw, node, units))
    base, ours, theirs = (units for _, _, units in sides)
    kept = [k for k in base if k in ours and k in theirs]
    for units in (ours, theirs):
        if [k for k in units if k in kept] != kept:
            return None

    def text(side, key):
        raw, _, units = sides[side]
        return raw[units[key][1] : units[key][2]]

    for key in base:
        for gone, other in ((1, 2), (2, 1)):
            if key not in sides[gone][2] and key in sides[other][2]:
                if text(other, key) != text(0, key):
                    return None

    def slots(side):
        """Each side's pieces between kept units, and the keys it added there."""
        raw, node, units = sides[side]
        found, pieces, position, added = [], [], node[1], set()
        for key, child in units.items():
            pieces.extend(raw[position : child[1]].splitlines(keepends=True))
            position = child[2]
            if key in kept:
                found.append((pieces, added))
                pieces, added = [], set()
                continue
            if key not in base:
                added.add(key)
            pieces.append(("unit", raw[child[1] : child[2]]))
        pieces.extend(raw[position : node[2]].splitlines(keepends=True))
        found.append((pieces, added))
        return found

    base_slots, our_slots, their_slots = slots(0), slots(1), slots(2)
    for i, ((_, mine), (_, yours)) in enumerate(zip(our_slots, their_slots)):
        for key in mine - yours:
            if key in theirs:
                return None
        for key in yours - mine:
            if key in ours:
                return None
    output = []
    for i, slot in enumerate(zip(base_slots, our_slots, their_slots)):
        (gap0, _), (gap1, mine), (gap2, yours) = slot
        shared = mine & yours
        if any(text(1, k) != text(2, k) for k in shared):
            return None
        merged = slot_merge(gap0, gap1, gap2, shared)
        if merged is None:
            return None
        output.extend(p[1] if type(p) is tuple else p for p in merged)
        if i < len(kept):
            key = kept[i]
            body = tree_merge([(raw, units[key]) for raw, _, units in sides])
            if body is None:
                return None
            output.append(body)
    return b"".join(output)


def outline_merge(path, blobs):
    trees = [readers.outline(path, blob) for blob in blobs]
    if None in trees:
        return None
    merged = tree_merge(trees)
    if merged is None or readers.outline(path, merged) is None:
        return None
    return merged


def has_driver(path):
    return readers.reader_for(path) is not None


def resolve_file(path, blobs):
    """(merged content, method) or (None, None) for Git's line merge and the author."""
    reader = readers.reader_for(path)
    if reader is not None:
        merged = outline_merge(path, blobs)
        if merged is not None:
            return merged, reader.NAME + "-outline"
    return None, None
