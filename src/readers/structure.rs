//! Three-way merge of declaration extents. Matching keys identify units; gaps keep raw bytes.
use super::{outline, Node};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Read, Write};

#[derive(Clone, Copy)]
struct View<'a> {
    bytes: &'a [u8],
    node: &'a Node,
}

impl<'a> View<'a> {
    fn text(self) -> &'a [u8] {
        &self.bytes[self.node.start..self.node.end]
    }
    fn child(self, node: &'a Node) -> Self {
        Self {
            bytes: self.bytes,
            node,
        }
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Piece<'a> {
    Line(&'a [u8]),
    Unit(&'a [u8]),
}

impl<'a> Piece<'a> {
    fn is_unit(&self) -> bool {
        matches!(self, Self::Unit(_))
    }
    fn text(self) -> &'a [u8] {
        match self {
            Self::Unit(b) | Self::Line(b) => b,
        }
    }
}

fn choose<'a, T: ?Sized + Eq>(base: &'a T, ours: &'a T, theirs: &'a T) -> Option<&'a T> {
    if ours == theirs || base == theirs {
        Some(ours)
    } else if ours == base {
        Some(theirs)
    } else {
        None
    }
}

struct Edit<'a, T> {
    start: usize,
    end: usize,
    replacement: &'a [T],
}

fn edit<'a, T: Eq>(base: &[T], side: &'a [T]) -> Edit<'a, T> {
    let start = base.iter().zip(side).take_while(|(a, b)| a == b).count();
    let tail = base[start..]
        .iter()
        .rev()
        .zip(side[start..].iter().rev())
        .take_while(|(a, b)| a == b)
        .count();
    Edit {
        start,
        end: base.len() - tail,
        replacement: &side[start..side.len() - tail],
    }
}

fn beside<'a>(
    base: &[Piece<'a>],
    run: &Edit<'_, Piece<'a>>,
    insertion: &Edit<'_, Piece<'a>>,
) -> Option<Vec<Piece<'a>>> {
    let p = run.replacement;
    let q = insertion.replacement;
    if insertion.start != insertion.end
        || run.start == run.end
        || p.is_empty()
        || !q.iter().any(Piece::is_unit)
    {
        return None;
    }
    let (first, second) =
        if insertion.start == run.start && base[run.start].is_unit() && p[0].is_unit() {
            (q, p)
        } else if insertion.start == run.end && base[run.end - 1].is_unit() && p.last()?.is_unit() {
            (p, q)
        } else {
            return None;
        };
    Some([&base[..run.start], first, second, &base[run.end..]].concat())
}

fn merge_slot<'a>(
    base: &[Piece<'a>],
    ours: &[Piece<'a>],
    theirs: &[Piece<'a>],
    shared: bool,
) -> Option<Vec<Piece<'a>>> {
    if let Some(same) = choose(base, ours, theirs) {
        return Some(same.to_vec());
    }
    let a = edit(base, ours);
    let b = edit(base, theirs);
    if a.start == a.end && b.start == b.end && a.start == b.start {
        let (p, q) = (a.replacement, b.replacement);
        if !p.iter().any(Piece::is_unit) || !q.iter().any(Piece::is_unit) {
            return None;
        }
        let (p, q) = if !shared {
            (p, q)
        } else if q.starts_with(p) || q.ends_with(p) {
            (q, &[][..])
        } else if p.starts_with(q) || p.ends_with(q) {
            (p, &[][..])
        } else {
            return None;
        };
        return Some([&base[..a.start], p, q, &base[a.start..]].concat());
    }
    if shared {
        return None;
    }
    for (first, second) in [(&a, &b), (&b, &a)] {
        if let Some(merged) = beside(base, first, second) {
            return Some(merged);
        }
        if first.end < second.start {
            return Some(
                [
                    &base[..first.start],
                    first.replacement,
                    &base[first.end..second.start],
                    second.replacement,
                    &base[second.end..],
                ]
                .concat(),
            );
        }
    }
    None
}

type Key<'a> = &'a [u16];
type Units<'a> = BTreeMap<Key<'a>, View<'a>>;

#[derive(Default)]
struct Slot<'a> {
    pieces: Vec<Piece<'a>>,
    added: BTreeSet<Key<'a>>,
}

fn lines<'a>(mut gap: &'a [u8], pieces: &mut Vec<Piece<'a>>) {
    while !gap.is_empty() {
        let end = gap
            .iter()
            .position(|b| matches!(b, b'\n' | b'\r'))
            .map_or(gap.len(), |i| {
                i + 1 + usize::from(gap[i] == b'\r' && gap.get(i + 1) == Some(&b'\n'))
            });
        pieces.push(Piece::Line(&gap[..end]));
        gap = &gap[end..];
    }
}

fn slots<'a>(view: View<'a>, base: &Units<'a>, kept: &BTreeSet<Key<'a>>) -> Vec<Slot<'a>> {
    let mut slots = Vec::new();
    let mut slot = Slot::default();
    let mut pos = view.node.start;
    for child in &view.node.children {
        lines(&view.bytes[pos..child.start], &mut slot.pieces);
        pos = child.end;
        if kept.contains(child.key.as_slice()) {
            slots.push(std::mem::take(&mut slot));
        } else {
            if !base.contains_key(child.key.as_slice()) {
                slot.added.insert(&child.key);
            }
            slot.pieces.push(Piece::Unit(view.child(child).text()));
        }
    }
    lines(&view.bytes[pos..view.node.end], &mut slot.pieces);
    slots.push(slot);
    slots
}

enum Fragment<'a> {
    Bytes(&'a [u8]),
    Nodes([View<'a>; 3]),
}

fn expand<'a>(views: [View<'a>; 3]) -> Option<Vec<Fragment<'a>>> {
    let [base, ours, theirs] = views.map(View::text);
    if let Some(text) = choose(base, ours, theirs) {
        return Some(vec![Fragment::Bytes(text)]);
    }
    let maps = views.map(|v| {
        v.node
            .children
            .iter()
            .map(|n| (n.key.as_slice(), v.child(n)))
            .collect::<Units>()
    });
    if views
        .iter()
        .zip(&maps)
        .any(|(v, m)| m.is_empty() || v.node.children.len() != m.len())
    {
        return None;
    }
    let kept: Vec<_> = views[0]
        .node
        .children
        .iter()
        .map(|n| n.key.as_slice())
        .filter(|key| maps[1].contains_key(key) && maps[2].contains_key(key))
        .collect();
    let keep_set: BTreeSet<_> = kept.iter().copied().collect();
    for side in &views[1..] {
        let order = side
            .node
            .children
            .iter()
            .map(|n| n.key.as_slice())
            .filter(|key| keep_set.contains(key));
        if !order.eq(kept.iter().copied()) {
            return None;
        }
    }
    for (key, original) in &maps[0] {
        match (maps[1].get(key), maps[2].get(key)) {
            (None, Some(other)) | (Some(other), None) if other.text() != original.text() => {
                return None
            }
            _ => (),
        }
    }
    let [base, ours, theirs] = views.map(|v| slots(v, &maps[0], &keep_set));
    let mut out = Vec::new();
    for (i, ((base, a), b)) in base.iter().zip(&ours).zip(&theirs).enumerate() {
        if a.added
            .difference(&b.added)
            .any(|key| maps[2].contains_key(key))
            || b.added
                .difference(&a.added)
                .any(|key| maps[1].contains_key(key))
        {
            return None;
        }
        let mut shared = false;
        for key in a.added.intersection(&b.added) {
            if maps[1][key].text() != maps[2][key].text() {
                return None;
            }
            shared = true;
        }
        let pieces = merge_slot(&base.pieces, &a.pieces, &b.pieces, shared)?;
        out.extend(
            pieces
                .into_iter()
                .map(|piece| Fragment::Bytes(piece.text())),
        );
        if let Some(key) = kept.get(i) {
            out.push(Fragment::Nodes([maps[0][key], maps[1][key], maps[2][key]]));
        }
    }
    Some(out)
}

fn checked_root(suffix: &str, bytes: &[u8]) -> Option<Node> {
    let root = Node {
        key: vec![],
        start: 0,
        end: bytes.len(),
        children: outline(suffix, bytes)?,
    };
    let mut pending = vec![&root];
    while let Some(parent) = pending.pop() {
        let mut pos = parent.start;
        for child in &parent.children {
            if child.start < pos || child.end <= child.start || child.end > parent.end {
                return None;
            }
            pos = child.end;
            pending.push(child);
        }
    }
    Some(root)
}

fn merge(suffix: &str, blobs: [&[u8]; 3]) -> Option<Vec<u8>> {
    let [base, ours, theirs] = blobs.map(|b| checked_root(suffix, b));
    let roots = [base?, ours?, theirs?];
    let views = std::array::from_fn(|i| View {
        bytes: blobs[i],
        node: &roots[i],
    });
    let mut work = vec![Fragment::Nodes(views)];
    let mut out = Vec::new();
    while let Some(item) = work.pop() {
        match item {
            Fragment::Bytes(b) => out.extend_from_slice(b),
            Fragment::Nodes(nodes) => work.extend(expand(nodes)?.into_iter().rev()),
        }
    }
    checked_root(suffix, &out)?;
    Some(out)
}

fn unpack(mut input: &[u8]) -> Option<[&[u8]; 3]> {
    let mut blobs: [&[u8]; 3] = [&[]; 3];
    for blob in &mut blobs {
        let (header, rest) = input.split_at_checked(8)?;
        let len = usize::try_from(u64::from_be_bytes(header.try_into().ok()?)).ok()?;
        (*blob, input) = rest.split_at_checked(len)?;
    }
    input.is_empty().then_some(blobs)
}

pub fn command(suffix: &str) -> i32 {
    let mut input = Vec::new();
    if io::stdin().read_to_end(&mut input).is_err() {
        return 2;
    }
    let Some(blobs) = unpack(&input) else {
        return 2;
    };
    match merge(suffix, blobs) {
        Some(merged) if io::stdout().lock().write_all(&merged).is_ok() => 0,
        Some(_) => 2,
        None => 1,
    }
}
