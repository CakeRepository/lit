//! ATX heading sections. Offsets include the original line endings and any BOM.
use super::Node;

fn heading(line: &str) -> Option<(usize, String)> {
    let level = line.bytes().take_while(|&b| b == b'#').count();
    if !(1..=6).contains(&level) {
        return None;
    }
    let tail = &line[level..];
    if !tail.is_empty() && !tail.starts_with([' ', '\t']) {
        return None;
    }
    let title = tail.trim_matches([' ', '\t']);
    let before_hashes = title.trim_end_matches('#');
    let title = if before_hashes.is_empty() || before_hashes.ends_with([' ', '\t']) {
        before_hashes
    } else {
        title
    };
    let mut key = format!("h{level}");
    for word in title.split_whitespace() {
        key.push(' ');
        key.push_str(word);
    }
    Some((level, key))
}

fn sections(
    headings: &[(usize, String, usize)],
    index: &mut usize,
    parent: usize,
    total: usize,
) -> Vec<Node> {
    let mut nodes = Vec::new();
    while let Some((level, key, start)) = headings.get(*index) {
        if *level <= parent {
            break;
        }
        *index += 1;
        let children = sections(headings, index, *level, total);
        let end = headings.get(*index).map_or(total, |row| row.2);
        nodes.push(Node {
            key: key.encode_utf16().collect(),
            start: *start,
            end,
            children,
        });
    }
    nodes
}

pub(super) fn outline(text: &str) -> Option<Vec<Node>> {
    let body = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut position = text.len() - body.len();
    let lines: Vec<_> = body
        .split_inclusive('\n')
        .map(|piece| {
            let start = position;
            position += piece.len();
            (start, piece.trim_end_matches(['\n', '\r']))
        })
        .collect();
    let mut first = 0;
    if lines.first().is_some_and(|(_, l)| l.trim_end() == "---") {
        if let Some(end) = lines
            .iter()
            .skip(1)
            .position(|(_, l)| matches!(l.trim_end(), "---" | "..."))
        {
            first = end + 2;
        }
    }
    let mut fence = None;
    let mut comment = false;
    let mut headings = Vec::new();
    for &(start, line) in &lines[first..] {
        let trimmed = line.trim_start_matches(' ');
        let indented = line.len() - trimmed.len() <= 3;
        let marker = trimmed.as_bytes().first().copied();
        let count = trimmed.bytes().take_while(|b| Some(*b) == marker).count();
        if let Some((open, width)) = fence {
            if indented
                && marker == open
                && count >= width
                && trimmed[count..].trim_matches([' ', '\t']).is_empty()
            {
                fence = None;
            }
            continue;
        }
        if comment {
            comment = !line.contains("-->");
            continue;
        }
        if indented
            && matches!(marker, Some(b'`' | b'~'))
            && count >= 3
            && (marker != Some(b'`') || !trimmed[count..].contains('`'))
        {
            fence = Some((marker, count));
            continue;
        }
        if line
            .rsplit_once("<!--")
            .is_some_and(|(_, rest)| !rest.contains("-->"))
        {
            comment = true;
            continue;
        }
        if indented {
            if let Some((level, key)) = heading(trimmed) {
                headings.push((level, key, start));
            }
        }
    }
    if fence.is_some() || comment {
        return None;
    }
    Some(sections(&headings, &mut 0, 0, text.len()))
}
