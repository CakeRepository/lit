use super::*;

/// Frontier groups changes that affect the same unit. Git dependencies and changes
/// Frontier could not admit must stay together even when their test footprints are empty.
pub(super) fn groups(changes: &[Change], decision: &Value) -> Result<Vec<Vec<Change>>> {
    let mut edges: BTreeMap<u64, Vec<u64>> = changes.iter().map(|c| (c.number, vec![])).collect();
    let mut link = |a: u64, b: u64| -> Result<()> {
        edges.get_mut(&a).ok_or("Unknown component member")?.push(b);
        edges.get_mut(&b).ok_or("Unknown component member")?.push(a);
        Ok(())
    };
    for component in decision.field("components")?.array()? {
        let members = component.field("members")?.array()?;
        for pair in members.windows(2) {
            link(pair[0].number()?, pair[1].number()?)?;
        }
    }
    for (id, conflict) in decision.field("conflicts")?.object()? {
        let id = id.parse().map_err(|_| "Invalid conflict member")?;
        for other in conflict.field("held_by")?.array()? {
            link(id, other.number()?)?;
        }
    }
    for change in changes {
        for &required in &change.requires {
            link(change.number, required)?;
        }
    }
    let mut group_of = BTreeMap::new();
    let mut groups = vec![];
    for change in changes {
        if group_of.contains_key(&change.number) {
            continue;
        }
        let index = groups.len();
        groups.push(Vec::new());
        let mut pending = vec![change.number];
        group_of.insert(change.number, index);
        while let Some(id) = pending.pop() {
            for &next in &edges[&id] {
                if group_of.insert(next, index).is_none() {
                    pending.push(next);
                }
            }
        }
    }
    for change in changes {
        groups[group_of[&change.number]].push(change.clone());
    }
    Ok(groups)
}

pub(crate) fn subset(snapshot: &Value, members: &BTreeSet<u64>) -> Result<Value> {
    let mut out = snapshot.object()?.clone();
    for key in ["changes", "groups"] {
        if let Some(rows) = out.get(key) {
            let mut keep = vec![];
            for row in rows.array()? {
                if members.contains(&row.field("number")?.number()?) {
                    keep.push(row.clone());
                }
            }
            out.insert(key.into(), Value::Array(keep));
        }
    }
    // Keep every eligibility rule. An explicit sibling PR may already be closed
    // by the time this particular component lands.
    if let Some(Value::Object(selection)) = out.get_mut("selection") {
        if let Some(Value::Array(prs)) = selection.get_mut("prs") {
            prs.retain(|n| n.number().is_ok_and(|id| members.contains(&id)));
        }
    }
    Ok(Value::Object(out))
}
