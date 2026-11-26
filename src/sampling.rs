//! Reproduce Python's commit-seeded audit sequence so independent shards choose the same units.
use ring::digest::{digest, SHA512};
pub struct Sampler {
    state: [u32; 624],
    index: usize,
}
impl Sampler {
    pub fn from_commit(commit: &str) -> Self {
        let mut bytes = commit.as_bytes().to_vec();
        bytes.extend_from_slice(digest(&SHA512, commit.as_bytes()).as_ref());
        bytes.reverse();
        let keys = bytes
            .chunks(4)
            .map(|chunk| {
                let mut b = [0; 4];
                b[..chunk.len()].copy_from_slice(chunk);
                u32::from_le_bytes(b)
            })
            .collect::<Vec<_>>();
        let mut s = [0u32; 624];
        s[0] = 19650218;
        for i in 1..624 {
            s[i] = 1812433253u32
                .wrapping_mul(s[i - 1] ^ (s[i - 1] >> 30))
                .wrapping_add(i as u32);
        }
        let (mut i, mut j) = (1usize, 0usize);
        for _ in 0..624.max(keys.len()) {
            s[i] = (s[i] ^ ((s[i - 1] ^ (s[i - 1] >> 30)).wrapping_mul(1664525)))
                .wrapping_add(keys[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i == 624 {
                s[0] = s[623];
                i = 1;
            }
            if j == keys.len() {
                j = 0;
            }
        }
        for _ in 1..624 {
            s[i] = (s[i] ^ ((s[i - 1] ^ (s[i - 1] >> 30)).wrapping_mul(1566083941)))
                .wrapping_sub(i as u32);
            i += 1;
            if i == 624 {
                s[0] = s[623];
                i = 1;
            }
        }
        s[0] = 0x80000000;
        Self {
            state: s,
            index: 624,
        }
    }
    fn word(&mut self) -> u32 {
        if self.index == 624 {
            for i in 0..624 {
                let y = (self.state[i] & 0x80000000) | (self.state[(i + 1) % 624] & 0x7fffffff);
                self.state[i] = self.state[(i + 397) % 624]
                    ^ (y >> 1)
                    ^ if y & 1 == 1 { 0x9908b0df } else { 0 };
            }
            self.index = 0;
        }
        let mut y = self.state[self.index];
        self.index += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c5680;
        y ^= (y << 15) & 0xefc60000;
        y ^ (y >> 18)
    }
    pub fn sample(&mut self) -> f64 {
        let a = u64::from(self.word() >> 5);
        let b = u64::from(self.word() >> 6);
        ((a << 26) + b) as f64 / (1u64 << 53) as f64
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn independent_shards_draw_the_reference_sequence() {
        let mut s = Sampler::from_commit(&"a".repeat(40));
        assert_eq!(
            [s.sample(), s.sample(), s.sample()],
            [
                0.28019035976573703,
                0.023165234979514948,
                0.3774740774162142
            ]
        );
    }
}
