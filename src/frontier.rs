//! Track which combinations of changes still have passing certificates for every affected unit.
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub type Inputs = BTreeMap<String, Option<String>>;
pub type Members = BTreeSet<u64>;
pub type Units = BTreeSet<String>;
const KEEP: usize = 16;

#[derive(Clone, Debug, PartialEq)]
pub struct Certificate {
    pub inputs: Inputs,
    pub seconds: f64,
    pub proof: bool,
}
#[derive(Clone, Debug, Default)]
pub struct Intent {
    pub title: Option<String>,
    pub intent: Option<String>,
    pub diffs: BTreeMap<String, String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Side {
    pub change: u64,
    pub title: Option<String>,
    pub intent: Option<String>,
    pub diffs: BTreeMap<String, Option<String>>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Conflict {
    pub incoming: Side,
    pub landed_first: Vec<Side>,
    pub tests: Units,
}
#[derive(Debug)]
pub enum Admission {
    Duplicate,
    Conflict(Box<Conflict>),
}
#[derive(Debug, PartialEq)]
pub enum Hold {
    Moved(u64),
    Absent(Vec<u64>),
    Unproven(Units),
}

#[derive(Default)]
pub struct Frontier {
    pub base: Inputs,
    changes: BTreeMap<u64, (Inputs, Intent)>,
    order: Vec<u64>,
    certs: BTreeMap<String, Vec<Certificate>>,
    reads: BTreeMap<String, Units>,
    readers: BTreeMap<String, Units>,
    writer: BTreeMap<String, u64>,
    landed: Vec<u64>,
    done: Members,
    version: u64,
}
impl Frontier {
    pub fn new(base: Inputs) -> Self {
        Self {
            base,
            ..Self::default()
        }
    }
    pub fn version(&self) -> u64 {
        self.version
    }
    pub fn landed(&self) -> &[u64] {
        &self.landed
    }
    pub fn units(&self) -> impl Iterator<Item = &String> {
        self.certs.keys()
    }
    pub fn keys(&self) -> impl Iterator<Item = &String> {
        self.readers.keys()
    }
    pub fn content(&self, key: &str, members: Option<&Members>) -> Option<&String> {
        if let Some(id) = self
            .writer
            .get(key)
            .filter(|id| members.is_none_or(|m| m.contains(id)))
        {
            self.changes[id].0.get(key)?.as_ref()
        } else {
            self.base.get(key)?.as_ref()
        }
    }
    pub fn admit(&mut self, id: u64, writes: Inputs, intent: Intent) -> Result<(), Admission> {
        if self.changes.contains_key(&id) || self.done.contains(&id) {
            return Err(Admission::Duplicate);
        }
        let mut clashes = BTreeMap::<u64, Vec<String>>::new();
        for key in writes.keys() {
            if let Some(other) = self.writer.get(key) {
                clashes.entry(*other).or_default().push(key.clone());
            }
        }
        if !clashes.is_empty() {
            let keys: Vec<_> = clashes.values().flatten().cloned().collect();
            let side = |id, meta: &Intent, keys: &[String]| Side {
                change: id,
                title: meta.title.clone(),
                intent: meta.intent.clone(),
                diffs: keys
                    .iter()
                    .map(|k| (k.clone(), meta.diffs.get(k).cloned()))
                    .collect(),
            };
            return Err(Admission::Conflict(Box::new(Conflict {
                incoming: side(id, &intent, &keys),
                landed_first: clashes
                    .iter()
                    .map(|(other, keys)| side(*other, &self.changes[other].1, keys))
                    .collect(),
                tests: keys
                    .iter()
                    .filter_map(|k| self.readers.get(k))
                    .flatten()
                    .cloned()
                    .collect(),
            })));
        }
        self.order.push(id);
        for k in writes.keys() {
            self.writer.insert(k.clone(), id);
        }
        self.changes.insert(id, (writes, intent));
        Ok(())
    }
    pub fn eject(&mut self, id: u64) {
        if let Some((writes, _)) = self.changes.remove(&id) {
            for k in writes.keys() {
                self.writer.remove(k);
            }
            self.order.retain(|&n| n != id);
        }
    }
    pub fn snapshot<'a>(
        &self,
        keys: impl IntoIterator<Item = &'a String>,
        seconds: f64,
        members: Option<&Members>,
    ) -> Certificate {
        Certificate {
            inputs: keys
                .into_iter()
                .map(|k| (k.clone(), self.content(k, members).cloned()))
                .collect(),
            seconds,
            proof: false,
        }
    }
    fn fits(&self, c: &Certificate, members: Option<&Members>) -> bool {
        c.inputs
            .iter()
            .all(|(k, v)| self.content(k, members) == v.as_ref())
    }
    pub fn proves(&self, unit: &str, members: Option<&Members>) -> Option<&Certificate> {
        self.certs
            .get(unit)?
            .iter()
            .rev()
            .find(|c| self.fits(c, members))
    }
    pub fn record(&mut self, unit: String, cert: Certificate) {
        let mut kept = self.certs.remove(&unit).unwrap_or_default();
        kept.push(cert);
        if kept.len() > KEEP {
            let split = kept.len() - KEEP;
            let main = Members::new();
            let keep_main = if kept[split..].iter().any(|c| self.fits(c, Some(&main))) {
                None
            } else {
                kept[..split]
                    .iter()
                    .rposition(|c| self.fits(c, Some(&main)))
            };
            let mut i = 0;
            kept.retain(|_| {
                let keep = i >= split || Some(i) == keep_main;
                i += 1;
                keep
            });
        }
        let reads: Units = kept.iter().flat_map(|c| c.inputs.keys().cloned()).collect();
        if let Some(prior) = self.reads.insert(unit.clone(), reads.clone()) {
            for k in prior.difference(&reads) {
                if let Some(set) = self.readers.get_mut(k) {
                    set.remove(&unit);
                }
            }
        }
        for k in reads {
            self.readers.entry(k).or_default().insert(unit.clone());
        }
        self.certs.insert(unit, kept);
    }
    pub fn cone(&self, id: u64) -> Units {
        self.changes
            .get(&id)
            .into_iter()
            .flat_map(|(w, _)| w.keys())
            .filter_map(|k| self.readers.get(k))
            .flatten()
            .cloned()
            .collect()
    }
    pub fn touching(&self, unit: &str) -> Members {
        self.reads
            .get(unit)
            .into_iter()
            .flatten()
            .filter_map(|k| self.writer.get(k))
            .copied()
            .collect()
    }
    /// Traverse each unit only once so a shared unit with thousands of writers stays linear.
    pub fn components(&self) -> Vec<Vec<u64>> {
        let mut assigned = BTreeMap::new();
        let mut seen = Units::new();
        let mut groups = Vec::new();
        for &start in &self.order {
            if assigned.contains_key(&start) {
                continue;
            }
            let group = groups.len();
            groups.push(Vec::new());
            assigned.insert(start, group);
            let mut queue = VecDeque::from([start]);
            while let Some(id) = queue.pop_front() {
                for u in self.cone(id) {
                    if seen.insert(u.clone()) {
                        for n in self.touching(&u) {
                            if assigned.insert(n, group).is_none() {
                                queue.push_back(n);
                            }
                        }
                    }
                }
            }
        }
        for id in &self.order {
            groups[assigned[id]].push(*id);
        }
        groups
    }
    pub fn affected(&self, members: &Members) -> Units {
        members.iter().flat_map(|&id| self.cone(id)).collect()
    }
    pub fn needs(&self, members: &Members) -> Units {
        self.affected(members)
            .into_iter()
            .filter(|u| self.proves(u, Some(members)).is_none())
            .collect()
    }
    pub fn overlaps(&self, members: &Members) -> Units {
        self.affected(members)
            .into_iter()
            .filter(|u| self.touching(u).intersection(members).nth(1).is_some())
            .collect()
    }
    pub fn landable(&self) -> Vec<Vec<u64>> {
        self.components()
            .into_iter()
            .filter(|g| self.needs(&g.iter().copied().collect()).is_empty())
            .collect()
    }
    pub fn land_if(&mut self, ids: &[u64], version: u64) -> Result<(), Hold> {
        if version != self.version {
            return Err(Hold::Moved(self.version));
        }
        let absent: Vec<_> = ids
            .iter()
            .copied()
            .filter(|id| !self.changes.contains_key(id))
            .collect();
        if !absent.is_empty() || ids.is_empty() {
            return Err(Hold::Absent(absent));
        }
        let members = ids.iter().copied().collect();
        let need = self.needs(&members);
        if !need.is_empty() {
            return Err(Hold::Unproven(need));
        }
        self.version += 1;
        for id in ids {
            if let Some((writes, _)) = self.changes.remove(id) {
                for (k, v) in writes {
                    self.writer.remove(&k);
                    if v.is_none() {
                        self.base.remove(&k);
                    } else {
                        self.base.insert(k, v);
                    }
                }
                self.landed.push(*id);
                self.done.insert(*id);
            }
        }
        self.order.retain(|n| !members.contains(n));
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn input(k: &str, v: &str) -> Inputs {
        BTreeMap::from([(k.into(), Some(v.into()))])
    }
    #[test]
    fn old_certificates_survive_trimming_and_landing_is_versioned() {
        let mut f = Frontier::new(input("code", "old"));
        let initial = f.snapshot(f.base.keys(), 0.5, None);
        f.record("unit".into(), initial.clone());
        for n in 0..30 {
            f.record(
                "unit".into(),
                Certificate {
                    inputs: input("code", &n.to_string()),
                    proof: true,
                    seconds: 1.,
                },
            );
        }
        assert_eq!(f.proves("unit", None), Some(&initial));
        f.admit(2, input("code", "new"), Intent::default()).unwrap();
        assert!(matches!(
            f.admit(3, input("code", "different"), Intent::default()),
            Err(Admission::Conflict(_))
        ));
        assert_eq!(
            f.land_if(&[2], 0),
            Err(Hold::Unproven(Units::from(["unit".into()])))
        );
        let checked = f.snapshot(f.base.keys(), 0.4, None);
        f.record("unit".into(), checked);
        assert_eq!(f.landable(), vec![vec![2]]);
        f.land_if(&[2], 0).unwrap();
        assert_eq!(f.land_if(&[2], 0), Err(Hold::Moved(1)));
        assert_eq!(f.landed(), &[2]);
    }
}
