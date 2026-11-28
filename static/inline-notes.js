// Keep UTF-16 offsets aligned with the editor while excluding literal Markdown.
export function prose(source) {
  let fence = "";
  const lines = source.split("\n").map(line => {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = "";
      return "\0".repeat(line.length);
    }
    if (marker) {
      fence = marker[1];
      return "\0".repeat(line.length);
    }
    return line;
  }).join("\n");
  return lines.replace(/(?<!`)(`+)(?!`)([^\0]*?)(?<!`)\1(?!`)/g, s => "\0".repeat(s.length));
}

export function webUrl(value) {
  try {
    if (!/^https?:\/\//i.test(value)) return null;
    return new URL(value).href;
  } catch { return null; }
}

export function inlineTokens(source) {
  const pattern = /(?<![\\!])\[((?:\\.|[^\\\]\n\0])+)\]\((https?:\/\/[^\s()\0]+)\)|(?<!\\)\[\[#([\p{L}\p{N}_-]+)\]\]|(?<![\\\p{L}\p{N}_#])#([\p{L}\p{N}_-]+)#/giu;
  const result = [];
  for (const m of prose(source).matchAll(pattern)) {
    const type = m[1] ? "external" : m[3] ? "reference" : "assignment";
    const label = m[1]?.replace(/\\([\\\[\]])/g, "$1") || m[3] || m[4];
    const href = m[2] && webUrl(m[2]);
    if (m[2] && !href) continue;
    result.push({type, label, href, from:m.index, to:m.index + m[0].length});
  }
  return result;
}

export function typedTrigger(source, end) {
  const before = prose(source).slice(0, end);
  return /(?:^|\s)(:(?:pic|link): )$/.exec(before)?.[1];
}
